#!/usr/bin/env python3
"""obicaller — live caller ID and call events from an OBi ATA, over syslog.

An OBi will push a plain UDP syslog stream, and that stream carries call
setup, answer, teardown and registration changes *as they happen*. Everything
else these devices offer is polling: call history only appears once a call is
over, and a registration that flaps between two polls is invisible. This is
the only real-time interface the hardware has.

    ACKNOWLEDGEMENT
    ---------------
    The idea is not mine. It comes from **obicaller** by Ryan Young,
    https://github.com/YoRyan/obicaller — a public-domain talking caller-ID
    daemon for the OBi200 that announces incoming calls through espeak. That
    project is archived (November 2022) and is a shell script; this is a
    Python re-take on it.

    Four specifics come straight from that script, and it would be dishonest
    to present them as findings of mine:

      * the line that actually carries caller ID is
        ``<7> [SLIC] CID to deliver: ...`` — not something you would guess,
        and without it nothing is ever announced;
      * a withheld number arrives as two apostrophes, and reads better as
        "private caller";
      * a leading country code is not worth saying out loud;
      * announcements are worth suppressing overnight. The original speaks
        only between 08:00 and 22:00, which is the sort of detail that only
        turns up after living with a thing.

    Reading the digits out individually is its idea too, and correct: espeak
    given a bare number says "five million five hundred fifty-one
    thousand...". The name is kept in tribute.

Standard library only, Python 3.6+, so it runs on whatever is already on the
box that can hear the device — in the case it was written for, an Alpine
netbook running an Asterisk PBX.

Typical use
-----------

    # watch calls as they happen
    obicaller.py --port 5515

    # announce the caller aloud, and relay everything to a collector that is
    # already listening on 5514 so both consumers get the stream
    obicaller.py --port 5515 --say --forward 127.0.0.1:5514

    # run a command on each call event; the event is in the environment
    obicaller.py --port 5515 --exec '/usr/local/bin/notify-call'

Point the device at it under System Management -> Device Admin -> Syslog:
set Server to this host, Port to the port below, and Level to 7. With obicfg:

    obicfg set admin.Server=<this host> admin.Port=5515 admin.Level=7

This script never talks to the device. It only listens.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

__version__ = "1.0.0"

SEVERITIES = (
    "emergency", "alert", "critical", "error",
    "warning", "notice", "info", "debug",
)

_PRI = re.compile(r"^<(\d+)>\s*(.*)$", re.S)
# CATEGORY:message. Requiring the colon stops the first word of an
# ordinary sentence being promoted to a category.
_CATEGORY = re.compile(r"^([A-Za-z0-9_]+)\s*:")
_SP = re.compile(r"\bSP([1-4])\b")
# The line that actually carries caller ID. Credit: obicaller.
_CID = re.compile(r"\[SLIC\]\s*CID to deliver:\s*(.*)", re.IGNORECASE)
# A dialled or presented number: at least three of digits/*/# , optionally +.
_NUMBER = re.compile(r"\+?[0-9*#]{3,}")

#: Checked in order, so anything containing a shorter needle comes first:
#: "Unregistered" contains "registered", and calling a service drop a
#: successful registration is exactly backwards.
#:
#: There is deliberately no bare "connected" needle. The device emits
#: "TC:ssl connected" for the TLS handshake to its provider several times an
#: hour, which was being recorded as a call being answered -- and then kept
#: for a year by the retention rule for call events. A call reports "Call
#: Connected".
CALL_EVENTS = (
    ("call connected", "connected"),
    ("call ended", "ended"),
    ("incoming call", "ringing"),
    ("registration failed", "registration_failed"),
    ("unregistered", "unregistered"),
    ("registered", "registered"),
    ("ringing", "ringing"),
    ("hangup", "ended"),
    ("hang up", "ended"),
)

#: Events worth interrupting a human for.
ANNOUNCE = ("ringing",)

#: Dropped unless --no-default-exclude. An OBi whose OBiTALK provisioning is
#: gone retries the lookup forever: measured at ~9.5 datagrams a second, or
#: some 800,000 a day, all identical and all useless. Relaying that into a
#: database on a small machine is how you fill a disk by Tuesday.
DEFAULT_EXCLUDES = (r"resolving root\.pnn\.obihai\.com",)


def parse(data, source="", now=None):
    """Parse one datagram into a dict.

    Unrecognised lines still come back, with `raw` intact. A feed that drops
    what it cannot classify is worst exactly when something unusual is
    happening, which is when you are reading it.
    """
    text = data.decode("utf-8", "replace").replace("\x00", "").strip()
    event = {
        "at": time.time() if now is None else now,
        "source": source,
        "raw": text,
        "message": text,
        "severity": None,
        "severity_name": None,
        "facility": None,
        "category": "system",
        "sp": None,
        "call_event": None,
        "caller": None,
        "number": None,
    }

    priority = _PRI.match(text)
    if priority:
        value = int(priority.group(1))
        event["severity"] = value & 0x07
        event["facility"] = value >> 3
        event["severity_name"] = SEVERITIES[value & 0x07]
        event["message"] = priority.group(2).strip()

    category = _CATEGORY.match(event["message"])
    if category:
        event["category"] = category.group(1).lower()

    service = _SP.search(event["message"])
    if service:
        event["sp"] = int(service.group(1))

    # Caller ID is checked first: it is a ringing event and it carries the
    # caller, and none of the generic needles below match its wording.
    caller = _CID.search(event["message"])
    if caller:
        event["call_event"] = "ringing"
        event["caller"] = caller.group(1).strip()
    else:
        lowered = event["message"].lower()
        for needle, name in CALL_EVENTS:
            if needle in lowered:
                event["call_event"] = name
                break

    # Only where the line is actually about a call. Otherwise any digits
    # anywhere become a "phone number": CallHistoryXmlSize=123633/205824
    # yielded 123633, and PARAM Cache Write Back(256 bytes) yielded 256.
    if event["caller"] or event["call_event"]:
        number = _NUMBER.search(event["caller"] or event["message"])
        if number:
            event["number"] = number.group(0)
    return event


def normalise_caller(caller):
    """Turn a raw CID payload into something worth saying.

    A withheld number arrives as two apostrophes; a North American number
    arrives with a country code nobody says out loud. Both: credit obicaller.
    """
    if caller is None:
        return None
    text = caller.strip().strip(chr(34)).strip()
    if not text.strip(chr(39)):
        return "private caller"
    digits = re.sub(r"[^0-9+]", "", text)
    if digits.startswith("+1") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    digits = digits.lstrip("+")
    return digits or text.strip(chr(39))


def spoken(number):
    """Digits read one at a time, which is the only way they are intelligible.

    Credit: obicaller. A bare number makes espeak say "five million five
    hundred fifty-one thousand two hundred thirty-four", which is useless to
    someone listening for a phone number.
    """
    if not number:
        return "an unknown caller"
    text = number.replace("+", "")
    if not text.isdigit():
        return text
    return " ".join(text)


def within_hours(window, now=None):
    """Is the current hour inside START-END (24h, end exclusive)?

    Credit: obicaller, which speaks only between 08:00 and 22:00. Nobody
    wants the house told about a robocall at three in the morning.
    """
    if not window:
        return True
    start, _, end = window.partition("-")
    try:
        start, end = int(start), int(end)
    except ValueError:
        return True
    hour = time.localtime().tm_hour if now is None else now
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end          # a window across midnight


def announce(event, voice="en-us", amplitude=200, speed=140, player="aplay"):
    """Say the caller aloud, if a speech engine is installed.

    Rendered to a WAV on stdout and piped to a player rather than letting
    espeak open the sound device itself. espeak's own output path is the
    first thing to break on a machine with an unusual ALSA setup, and this
    way the player decides where the audio goes. Credit: obicaller does the
    same, for what I assume was the same reason.
    """
    engine = shutil.which("espeak-ng") or shutil.which("espeak")
    if not engine:
        return False
    caller = normalise_caller(event.get("caller"))
    if caller is None:
        caller = event.get("number")
    text = "Call from %s" % (
        caller if caller == "private caller" else spoken(caller)
    )
    speak = [engine, "-v", voice, "-a", str(amplitude), "-s", str(speed), text]
    play = shutil.which(player) if player else None
    try:
        if play:
            speaker = subprocess.Popen(
                speak + ["--stdout"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            subprocess.Popen(
                [play, "-q"],
                stdin=speaker.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            speaker.stdout.close()
        else:
            subprocess.Popen(
                speak, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        return True
    except OSError:
        return False


def run_hook(command, event):
    """Run a command per event, passing it through the environment.

    Fire and forget on purpose: a slow or wedged hook must not stall the
    listener and lose the next datagram.
    """
    environment = dict(os.environ)
    for key, value in event.items():
        environment["OBI_" + key.upper()] = "" if value is None else str(value)
    try:
        subprocess.Popen(
            command,
            shell=True,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        print("hook failed: %s" % exc, file=sys.stderr)


def parse_target(text):
    host, _, port = text.rpartition(":")
    if not host:
        raise ValueError("expected host:port, got %r" % text)
    return (host, int(port))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="obicaller",
        description=__doc__.split("ACKNOWLEDGEMENT")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Idea credit: obicaller by Ryan Young "
               "(https://github.com/YoRyan/obicaller), public domain.",
    )
    parser.add_argument("--version", action="version", version="obicaller " + __version__)
    parser.add_argument("--bind", default="0.0.0.0", help="address to listen on")
    parser.add_argument("--port", type=int, default=5515,
                        help="UDP port to listen on (default 5515; 514 needs root)")
    parser.add_argument("--json", action="store_true",
                        help="one JSON object per line, for piping")
    parser.add_argument("--calls-only", action="store_true",
                        help="only lines that mark a point in a call's life")
    parser.add_argument("--say", action="store_true",
                        help="announce incoming callers aloud via espeak")
    parser.add_argument("--voice", default="en-us", help="espeak voice for --say")
    parser.add_argument("--amplitude", type=int, default=200, metavar="0-200",
                        help="espeak loudness for --say (default 200, its maximum)")
    parser.add_argument("--speed", type=int, default=140, metavar="WPM",
                        help="speaking rate for --say (default 140; espeak's own "
                             "175 is too fast for a number read aloud)")
    parser.add_argument("--player", default="aplay",
                        help="command that plays a WAV on stdin (default aplay); "
                             "pass an empty string to let espeak open the device")
    parser.add_argument("--say-between", default="8-22", metavar="START-END",
                        help="only speak between these hours, 24h, end exclusive "
                             "(default 8-22). Empty string to speak at any hour)")
    parser.add_argument("--forward", action="append", default=[], metavar="HOST:PORT",
                        help="relay every datagram here too; repeatable. Use this "
                             "when something else already wants the stream -- the "
                             "device can only send to one address")
    parser.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                        help="drop datagrams matching this, before logging AND "
                             "before forwarding; repeatable")
    parser.add_argument("--no-default-exclude", action="store_true",
                        help="keep the OBiTALK retry chatter that is dropped by "
                             "default (see DEFAULT_EXCLUDES)")
    parser.add_argument("--exec", dest="hook", metavar="CMD",
                        help="run CMD per event, with OBI_* set in its environment")
    parser.add_argument("--log", metavar="FILE", help="append output to FILE as well")
    parser.add_argument("--count", type=int, help="stop after N datagrams")
    args = parser.parse_args(argv)

    try:
        targets = [parse_target(t) for t in args.forward]
    except ValueError as exc:
        parser.error(str(exc))

    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.settimeout(1.0)
    try:
        listener.bind((args.bind, args.port))
    except OSError as exc:
        hint = ""
        if args.port < 1024:
            hint = " (ports below 1024 need root; try --port 5515)"
        print("cannot listen on %s:%d: %s%s" % (args.bind, args.port, exc, hint),
              file=sys.stderr)
        return 1

    patterns = list(args.exclude)
    if not args.no_default_exclude:
        patterns.extend(DEFAULT_EXCLUDES)
    try:
        excludes = [re.compile(p, re.IGNORECASE) for p in patterns]
    except re.error as exc:
        parser.error("bad --exclude pattern: %s" % exc)

    relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if targets else None
    handle = open(args.log, "a", buffering=1) if args.log else None

    if not args.json:
        print("obicaller %s listening on %s:%d" % (__version__, args.bind, args.port))
        if targets:
            print("relaying to " + ", ".join("%s:%d" % t for t in targets))

    seen = 0
    try:
        while args.count is None or seen < args.count:
            try:
                data, addr = listener.recvfrom(65535)
            except socket.timeout:
                continue
            seen += 1

            # Excluded before anything else, including the relay: the point is
            # to keep the noise off the network and out of the other
            # consumer's storage, not merely off our own screen.
            text = data.decode("utf-8", "replace")
            if any(pattern.search(text) for pattern in excludes):
                continue

            # Relay first, and never let a relay failure cost us the event:
            # the other consumer being down is not our problem to escalate.
            for target in targets:
                try:
                    relay.sendto(data, target)
                except OSError:
                    pass

            event = parse(data, source=addr[0])
            if args.calls_only and not event["call_event"]:
                continue

            if args.json:
                line = json.dumps(event, sort_keys=True)
            else:
                stamp = time.strftime("%H:%M:%S", time.localtime(event["at"]))
                where = "SP%d" % event["sp"] if event["sp"] else event["category"]
                marker = "[%s] " % event["call_event"] if event["call_event"] else ""
                line = "%s %-8s %s%s" % (stamp, where, marker, event["message"])
            print(line)
            sys.stdout.flush()
            if handle:
                handle.write(line + "\n")

            if (args.say and event["call_event"] in ANNOUNCE
                    and within_hours(args.say_between)):
                announce(event, args.voice, args.amplitude, args.speed,
                         args.player)
            if args.hook and event["call_event"]:
                run_hook(args.hook, event)
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()
        if relay:
            relay.close()
        if handle:
            handle.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
