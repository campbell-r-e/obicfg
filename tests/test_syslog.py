"""The syslog listener: parsing, sockets, and the setup advice."""

from __future__ import annotations

import socket

import pytest

from obicfg import syslog


class TestParsing:
    def test_priority_splits_into_facility_and_severity(self):
        event = syslog.parse_line(b"<7> SIP:SP1 Registered\x00", source="192.0.2.50")
        assert (event.severity, event.facility) == (7, 0)
        assert event.severity_name == "debug"
        assert event.source == "192.0.2.50"
        assert "\x00" not in event.raw

    def test_a_high_priority_value(self):
        assert syslog.parse_line(b"<190>x").facility == 23

    def test_category_service_and_number(self):
        event = syslog.parse_line(b"<6> CALL:Incoming call from +15551234567 on SP2")
        assert event.category == "call"
        assert event.sp == 2
        assert event.number == "+15551234567"
        assert event.call_event == "ringing"

    @pytest.mark.parametrize(
        "text,expected",
        [
            (b"<6> Call Connected", "connected"),
            # Observed on real hardware: the TLS handshake to the
            # provider, several times an hour. Not a call.
            (b"<7> TC:ssl connected", None),
            (b"<7> Trying to connect ssl", None),
            (b"<6> Call Ended", "ended"),
            (b"<6> SP1 Ringing", "ringing"),
            (b"<7> SIP:SP1 Registered", "registered"),
            (b"<3> Registration failed", "registration_failed"),
            (b"<6> SP3 Unregistered", "unregistered"),
            (b"<6> nothing interesting", None),
        ],
    )
    def test_call_events(self, text, expected):
        assert syslog.parse_line(text).call_event == expected

    def test_an_unparseable_line_is_still_an_event(self):
        event = syslog.parse_line(b"total gibberish")
        assert event.raw == "total gibberish"
        assert event.severity is None
        assert event.category == "system"

    def test_the_formatted_line_carries_the_useful_bits(self):
        event = syslog.parse_line(b"<6> CALL:Incoming call on SP2", received_at=0)
        line = event.format()
        assert "SP2" in line and "[ringing]" in line and "Incoming call" in line

    def test_a_line_with_no_service_falls_back_to_the_category(self):
        assert "sys" in syslog.parse_line(b"<7> SYS:tick", received_at=0).format()

    def test_to_dict_is_json_ready(self):
        import json

        json.dumps(syslog.parse_line(b"<6> Call Ended").to_dict())


class TestListener:
    def test_open_and_report_its_address(self):
        listener = syslog.open_listener("127.0.0.1", 0)
        try:
            assert listener.address[0] == "127.0.0.1"
            assert listener.address[1] > 0
        finally:
            listener.sock.close()

    def test_an_unbound_listener_reports_its_requested_address(self):
        assert syslog.Listener(bind="127.0.0.1", port=5514).address == ("127.0.0.1", 5514)

    def test_events_are_yielded_as_they_arrive(self):
        listener = syslog.open_listener("127.0.0.1", 0, timeout=0.2)
        try:
            port = listener.address[1]
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sender.sendto(b"<6> Call Ended", ("127.0.0.1", port))
            sender.close()
            events = list(syslog.events(listener, count=1))
            assert [e.call_event for e in events] == ["ended"]
        finally:
            listener.sock.close()

    def test_a_deadline_ends_a_quiet_stream(self):
        import time

        listener = syslog.open_listener("127.0.0.1", 0, timeout=0.05)
        try:
            assert list(syslog.events(listener, deadline=time.time() + 0.1)) == []
        finally:
            listener.sock.close()

    def test_a_closed_socket_ends_the_stream_rather_than_raising(self):
        listener = syslog.open_listener("127.0.0.1", 0, timeout=0.05)
        listener.sock.close()
        assert list(syslog.events(listener)) == []


class TestSetupAdvice:
    def test_it_reports_the_address_the_device_would_see(self):
        # Connecting a UDP socket sends nothing; it asks the routing table
        # which source address would be used. Any routable host will do.
        address = syslog.local_address_for("192.0.2.1")
        assert address.count(".") == 3 or address == "<this host's address>"

    def test_an_unroutable_target_degrades_to_a_placeholder(self, monkeypatch):
        def refuse(self, address):
            raise OSError("no route to host")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        assert syslog.local_address_for("192.0.2.1") == "<this host's address>"

    def test_the_commands_are_returned_not_run(self):
        commands = syslog.setup_commands("192.0.2.71", 5514, level=7)
        assert commands == [
            "obicfg set admin.Server=192.0.2.71",
            "obicfg set admin.Port=5514",
            "obicfg set admin.Level=7",
        ]
        # They are strings for a human to read and run, deliberately: enabling
        # the feed changes device behaviour and exposes call metadata.
        assert all(isinstance(c, str) for c in commands)


class TestCallerID:
    """The line that actually carries caller ID.

    Credit for the pattern: obicaller. Without it none of the generic call
    needles match, so a ringing phone produced no event at all.
    """

    def test_a_caller_id_line_is_a_ringing_event(self):
        event = syslog.parse_line(b"<7> [SLIC] CID to deliver: '+17655551234'")
        assert event.call_event == "ringing"
        assert event.caller == "'+17655551234'"
        assert event.number == "+17655551234"

    def test_a_withheld_number(self):
        event = syslog.parse_line(b"<7> [SLIC] CID to deliver: ''")
        assert event.call_event == "ringing"
        assert event.caller == "''"
        assert event.number is None

    def test_a_name_rather_than_a_number(self):
        event = syslog.parse_line(b"<7> [SLIC] CID to deliver: 'JOHN SMITH'")
        assert event.call_event == "ringing"
        assert event.caller == "'JOHN SMITH'"

    def test_the_number_is_taken_from_the_caller_not_the_rest_of_the_line(self):
        # "[SLIC]" and any trailing counters must not be mistaken for a number.
        event = syslog.parse_line(b"<7> [SLIC] CID to deliver: '5551234'")
        assert event.number == "5551234"

    def test_lines_without_caller_id_still_classify_normally(self):
        assert syslog.parse_line(b"<6> Call Ended").call_event == "ended"

    def test_to_dict_includes_the_caller(self):
        assert "caller" in syslog.parse_line(b"<6> Call Ended").to_dict()


class TestNumberExtraction:
    """`number` must mean a phone number, not "some digits on the line"."""

    @pytest.mark.parametrize(
        "line",
        [
            b"<7> CallHistoryXmlSize=123633/205824",
            b"<7> PARAM Cache Write Back(256 bytes)",
            b"<7> BASE:resolving root.pnn.obihai.com",
        ],
    )
    def test_housekeeping_lines_carry_no_number(self, line):
        # These were yielding 123633 and 256 as "phone numbers", which then
        # went into a database column named `number`.
        event = syslog.parse_line(line)
        assert event.call_event is None
        assert event.number is None

    def test_a_caller_id_line_still_yields_its_number(self):
        assert syslog.parse_line(
            b"<7> [SLIC] CID to deliver: '+17655551234'"
        ).number == "+17655551234"

    def test_a_call_line_still_yields_its_number(self):
        assert syslog.parse_line(
            b"<6> CALL:Incoming call from 5551234 on SP1"
        ).number == "5551234"
