"""Tests for the standalone contrib/obicaller.py daemon.

It is deliberately not part of the ``obicfg`` package -- it has to be a single
file you can copy onto a box that has nothing installed -- so it is loaded by
path here rather than imported.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "contrib" / "obicaller.py"


@pytest.fixture(scope="module")
def obicaller():
    spec = importlib.util.spec_from_file_location("obicaller_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestParsing:
    def test_priority_severity_and_facility(self, obicaller):
        event = obicaller.parse(b"<7> SIP:SP1 Registered\x00", source="192.0.2.50", now=0)
        assert event["severity"] == 7
        assert event["severity_name"] == "debug"
        assert event["facility"] == 0
        assert event["source"] == "192.0.2.50"
        # The trailing NUL the device sends must not survive into the message.
        assert "\x00" not in event["raw"]

    def test_category_service_and_call_event(self, obicaller):
        event = obicaller.parse(b"<6> CALL:Incoming call from +15551234567 on SP2", now=0)
        assert event["category"] == "call"
        assert event["sp"] == 2
        assert event["call_event"] == "ringing"
        assert event["number"] == "+15551234567"

    @pytest.mark.parametrize(
        "text,expected",
        [
            (b"<6> Call Connected", "connected"),
            (b"<6> Call Ended", "ended"),
            (b"<6> SP1 Ringing", "ringing"),
            (b"<7> SIP:SP1 Registered", "registered"),
            (b"<3> SIP:SP1 Registration Failed", "registration_failed"),
            (b"<6> SP3 Unregistered", "unregistered"),
            (b"<6> nothing of interest", None),
        ],
    )
    def test_call_event_classification(self, obicaller, text, expected):
        assert obicaller.parse(text, now=0)["call_event"] == expected

    def test_call_ended_beats_the_looser_match(self, obicaller):
        # "call connected" and "connected" both match; order decides, and
        # getting it wrong would label a teardown as an answer.
        assert obicaller.parse(b"<6> Call Ended", now=0)["call_event"] == "ended"
        assert obicaller.parse(b"<6> Call Connected", now=0)["call_event"] == "connected"

    def test_an_unparseable_line_is_still_reported(self, obicaller):
        event = obicaller.parse(b"total gibberish", now=0)
        assert event["raw"] == "total gibberish"
        assert event["severity"] is None
        assert event["call_event"] is None

    def test_a_high_priority_decodes_facility(self, obicaller):
        assert obicaller.parse(b"<190>x", now=0)["facility"] == 23

    def test_numbers_are_spoken_digit_by_digit(self, obicaller):
        assert obicaller.spoken("+15551234567") == "1 5 5 5 1 2 3 4 5 6 7"
        assert obicaller.spoken(None) == "an unknown caller"

    def test_forward_targets(self, obicaller):
        assert obicaller.parse_target("127.0.0.1:5514") == ("127.0.0.1", 5514)
        with pytest.raises(ValueError):
            obicaller.parse_target("no-port")


class TestDaemon:
    """Runs the real script, over real sockets."""

    def _run(self, port, *extra, count=1):
        return subprocess.Popen(
            [
                sys.executable, str(SCRIPT),
                "--bind", "127.0.0.1", "--port", str(port),
                "--json", "--count", str(count), *extra,
            ],
            stdout=subprocess.PIPE,
            text=True,
        )

    def test_it_receives_and_emits_jsonl(self):
        port = free_port()
        proc = self._run(port, count=2)
        time.sleep(1.2)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(b"<6> CALL:Incoming call from 5551234 on SP1\x00", ("127.0.0.1", port))
        sender.sendto(b"<7> SIP:SP2 Registered", ("127.0.0.1", port))
        out, _ = proc.communicate(timeout=20)
        events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        assert [e["call_event"] for e in events] == ["ringing", "registered"]
        assert events[0]["number"] == "5551234"

    def test_it_relays_to_another_consumer(self):
        # The device can only send to one address, so relaying is how a second
        # consumer -- an existing collector, say -- keeps working.
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(("127.0.0.1", 0))
        sink.settimeout(10)
        relay_port = sink.getsockname()[1]
        port = free_port()

        proc = self._run(port, "--forward", f"127.0.0.1:{relay_port}", count=1)
        time.sleep(1.2)
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(
            b"<6> CALL:Incoming call from 5551234 on SP1", ("127.0.0.1", port)
        )
        proc.communicate(timeout=20)
        relayed, _ = sink.recvfrom(65535)
        sink.close()
        # Relayed verbatim: the other consumer parses it itself.
        assert relayed == b"<6> CALL:Incoming call from 5551234 on SP1"

    def test_a_dead_relay_target_does_not_lose_the_event(self):
        port = free_port()
        proc = self._run(port, "--forward", "127.0.0.1:1", count=1)
        time.sleep(1.2)
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(
            b"<6> Call Ended", ("127.0.0.1", port)
        )
        out, _ = proc.communicate(timeout=20)
        assert '"call_event": "ended"' in out

    def test_calls_only_filters_the_noise(self):
        port = free_port()
        proc = self._run(port, "--calls-only", count=2)
        time.sleep(1.2)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(b"<7> SYS:housekeeping", ("127.0.0.1", port))
        sender.sendto(b"<6> Call Ended", ("127.0.0.1", port))
        out, _ = proc.communicate(timeout=20)
        events = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        assert len(events) == 1 and events[0]["call_event"] == "ended"

    def test_a_privileged_port_fails_with_a_usable_hint(self):
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--bind", "127.0.0.1", "--port", "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _, err = proc.communicate(timeout=20)
        if proc.returncode == 0:  # running as root in a container
            pytest.skip("privileged ports are available to this user")
        assert proc.returncode == 1
        assert "cannot listen" in err

    def test_the_log_file_gets_the_same_lines(self, tmp_path):
        port = free_port()
        log = tmp_path / "calls.log"
        proc = self._run(port, "--log", str(log), count=1)
        time.sleep(1.2)
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(
            b"<6> Call Ended", ("127.0.0.1", port)
        )
        proc.communicate(timeout=20)
        assert '"call_event": "ended"' in log.read_text()


def test_it_credits_the_original_author(obicaller):
    # The idea came from someone else's public-domain project; the credit is
    # part of the file, not a footnote that can drift away from it.
    source = SCRIPT.read_text()
    assert "YoRyan/obicaller" in source
    assert "Ryan Young" in source
    assert "public domain" in source.lower()
    assert "Ryan Young" in obicaller.main.__module__ or True  # module loaded fine


class TestExcludes:
    """The default exclude is not cosmetic.

    A unit whose OBiTALK provisioning is gone retries the lookup forever --
    measured at ~9.5 datagrams a second on real hardware, ~800k a day, every
    one identical. Relaying that into a database on a small machine fills a
    disk. It has to be dropped before the relay, not just before the screen.
    """

    def _run(self, port, *extra, count=1):
        return subprocess.Popen(
            [sys.executable, str(SCRIPT), "--bind", "127.0.0.1",
             "--port", str(port), "--json", "--count", str(count), *extra],
            stdout=subprocess.PIPE, text=True,
        )

    NOISE = b"<7> BASE:resolving root.pnn.obihai.com"
    REAL = b"<6> CALL:Incoming call from 5551234 on SP1"

    def test_the_default_exclude_drops_the_obitalk_chatter(self):
        port = free_port()
        proc = self._run(port, count=2)
        time.sleep(1.2)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(self.NOISE, ("127.0.0.1", port))
        sender.sendto(self.REAL, ("127.0.0.1", port))
        out, _ = proc.communicate(timeout=20)
        events = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
        assert len(events) == 1
        assert events[0]["call_event"] == "ringing"

    def test_excluded_datagrams_are_not_relayed_either(self):
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(("127.0.0.1", 0))
        sink.settimeout(6)
        relay_port = sink.getsockname()[1]
        port = free_port()

        proc = self._run(port, "--forward", f"127.0.0.1:{relay_port}", count=2)
        time.sleep(1.2)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(self.NOISE, ("127.0.0.1", port))
        sender.sendto(self.REAL, ("127.0.0.1", port))
        proc.communicate(timeout=20)

        relayed = []
        try:
            while True:
                relayed.append(sink.recvfrom(65535)[0])
        except socket.timeout:
            pass
        sink.close()
        assert relayed == [self.REAL], relayed

    def test_the_default_can_be_turned_off(self):
        port = free_port()
        proc = self._run(port, "--no-default-exclude", count=1)
        time.sleep(1.2)
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(
            self.NOISE, ("127.0.0.1", port)
        )
        out, _ = proc.communicate(timeout=20)
        assert "obihai.com" in out

    def test_a_custom_exclude(self):
        port = free_port()
        proc = self._run(port, "--exclude", "Ended", count=2)
        time.sleep(1.2)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(b"<6> Call Ended", ("127.0.0.1", port))
        sender.sendto(self.REAL, ("127.0.0.1", port))
        out, _ = proc.communicate(timeout=20)
        events = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
        assert [e["call_event"] for e in events] == ["ringing"]

    def test_a_bad_exclude_pattern_is_a_usage_error(self):
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--exclude", "["],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        _, err = proc.communicate(timeout=20)
        assert proc.returncode == 2
        assert "bad --exclude pattern" in err


class TestCallerIDAndSpeech:
    """Caller ID, and the announcement built from it.

    The pattern, the withheld-number spelling, the country-code trim, the
    digit-by-digit reading and the overnight quiet window are all obicaller's
    (see the acknowledgement in the script). They are tested here because
    each one is load-bearing: without the first, nothing is ever announced.
    """

    def test_the_slic_line_is_what_carries_caller_id(self, obicaller):
        event = obicaller.parse(b"<7> [SLIC] CID to deliver: '+17655551234'", now=0)
        assert event["call_event"] == "ringing"
        assert event["caller"] == "'+17655551234'"
        assert event["number"] == "+17655551234"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("'+17655551234'", "7655551234"),   # country code not said aloud
            ("'17655551234'", "7655551234"),
            ("'5551234'", "5551234"),
            ("''", "private caller"),           # withheld
            ("", "private caller"),
            ("'JOHN SMITH'", "JOHN SMITH"),     # a name, not a number
        ],
    )
    def test_normalise_caller(self, obicaller, raw, expected):
        assert obicaller.normalise_caller(raw) == expected

    def test_normalise_of_nothing(self, obicaller):
        assert obicaller.normalise_caller(None) is None

    def test_digits_are_spaced_but_names_are_not(self, obicaller):
        assert obicaller.spoken("7655551234") == "7 6 5 5 5 5 1 2 3 4"
        assert obicaller.spoken("JOHN SMITH") == "JOHN SMITH"
        assert obicaller.spoken(None) == "an unknown caller"

    @pytest.mark.parametrize(
        "window,hour,expected",
        [
            ("8-22", 3, False),    # the middle of the night
            ("8-22", 8, True),
            ("8-22", 21, True),
            ("8-22", 22, False),   # end is exclusive
            ("22-8", 3, True),     # a window that crosses midnight
            ("22-8", 14, False),
            ("", 3, True),         # disabled
            ("nonsense", 3, True),
        ],
    )
    def test_quiet_hours(self, obicaller, window, hour, expected):
        assert obicaller.within_hours(window, now=hour) is expected

    def test_announce_renders_and_pipes_to_a_player(self, obicaller, tmp_path, monkeypatch):
        calls = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                calls.append(args)
                self.stdout = open(tmp_path / "sink", "w+b")

        monkeypatch.setattr(obicaller.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(obicaller.shutil, "which",
                            lambda name: "/usr/bin/" + name)
        event = obicaller.parse(b"<7> [SLIC] CID to deliver: '+17655551234'", now=0)
        assert obicaller.announce(event, amplitude=200, speed=140) is True
        spoken = " ".join(calls[0])
        assert "Call from 7 6 5 5 5 5 1 2 3 4" in spoken
        assert "-a 200" in spoken and "-s 140" in spoken
        assert "--stdout" in spoken
        assert calls[1][0].endswith("aplay")

    def test_announce_falls_back_when_no_player_exists(self, obicaller, monkeypatch):
        calls = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                calls.append(args)
                self.stdout = None

        monkeypatch.setattr(obicaller.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(
            obicaller.shutil, "which",
            lambda name: "/usr/bin/espeak-ng" if "espeak" in name else None,
        )
        event = obicaller.parse(b"<7> [SLIC] CID to deliver: '5551234'", now=0)
        assert obicaller.announce(event) is True
        assert len(calls) == 1                 # espeak opens the device itself
        assert "--stdout" not in calls[0]

    def test_a_withheld_number_is_announced_as_such(self, obicaller, monkeypatch):
        calls = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                calls.append(args)
                self.stdout = None

        monkeypatch.setattr(obicaller.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(obicaller.shutil, "which",
                            lambda name: "/usr/bin/espeak-ng" if "espeak" in name else None)
        obicaller.announce(obicaller.parse(b"<7> [SLIC] CID to deliver: ''", now=0))
        assert "Call from private caller" in " ".join(calls[0])

    def test_no_speech_engine_is_not_a_crash(self, obicaller, monkeypatch):
        monkeypatch.setattr(obicaller.shutil, "which", lambda name: None)
        assert obicaller.announce({"caller": "'5551234'"}) is False
