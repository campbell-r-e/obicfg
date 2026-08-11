from __future__ import annotations

import pytest
from conftest import FakeClient, fixture

from obicfg import telemetry
from obicfg.device import Device
from obicfg.guard import Guard
from obicfg.model import parse_objects


def test_parse_objects_keeps_repeated_blocks_distinct():
    # The quality page repeats <object name="RTP Statistics"> once per service.
    # Grouping by name would fold SP2's counters onto SP1's.
    objects = parse_objects(fixture("EXAMPLE_QUALITY_.xml"))
    names = [name for name, _ in objects]
    assert names.count("RTP Statistics") == 2
    first = next(values for name, values in objects if name == "RTP Statistics")
    assert first["PacketsSent"] == "136092"


class TestUptime:
    def test_days_hours_and_reboot_count(self):
        assert telemetry.parse_uptime(" 2 Days 17:07:14    (4)") == (234434, 4)

    def test_without_days_or_reboots(self):
        assert telemetry.parse_uptime("01:02:03") == (3723, None)

    def test_unparseable(self):
        assert telemetry.parse_uptime("") == (None, None)
        assert telemetry.parse_uptime("no idea") == (None, None)


class TestStatus:
    @pytest.fixture
    def status(self):
        return telemetry.parse_status(fixture("EXAMPLE_STATUS_.xml"))

    def test_product_information(self, status):
        assert status.model == "OBi200"
        assert status.firmware == "3.2.2 (Build: 8680EX)"
        assert status.serial == "0000TESTSERIAL"
        assert status.wan_ip == "192.0.2.50"
        assert status.uptime_s == 234434
        assert status.boot_code == 4

    def test_services_are_indexed_by_their_object_name(self, status):
        assert [s.sp for s in status.services] == [1, 2, 3]
        assert status.services[0].status == "Connected"
        assert status.services[1].status == "Registration Not Required"

    def test_active_calls_are_parsed_from_the_call_state_string(self, status):
        assert status.services[0].active_calls == 1
        assert status.services[1].active_calls == 0
        assert status.active_calls == 1

    def test_registration_not_required_counts_as_healthy(self, status):
        # It is what an IP-authenticated trunk reports; flagging it as broken
        # would make every PBX trunk look permanently down.
        assert status.services[0].healthy is True
        assert status.services[1].healthy is True
        assert status.services[2].healthy is False  # "Service Not Configured"

    def test_obitalk_status_is_captured_when_present(self, status):
        assert status.obitalk_status == "Backing Off (29)"


class TestPort:
    def test_values_are_pulled_out_of_their_units(self):
        port = telemetry.parse_port(fixture("EXAMPLE_PORT_.xml"))
        assert port.state == "Off Hook"
        assert port.off_hook is True
        assert port.loop_current_ma == 23.0
        assert port.vbat_v == 56.0
        assert port.tipring_v == 6.5
        assert port.last_caller == "+15551234567"


class TestQuality:
    @pytest.fixture
    def quality(self):
        return telemetry.parse_quality(fixture("EXAMPLE_QUALITY_.xml"))

    def test_one_entry_per_block_numbered_in_order(self, quality):
        assert [q.sp for q in quality] == [1, 2]
        assert quality[0].packets_sent == 136092
        assert quality[0].underruns == 3

    def test_missing_counters_default_to_zero(self, quality):
        assert quality[1].bytes_sent == 0

    def test_loss_is_measured_against_what_should_have_arrived(self, quality):
        # 100 lost out of 9900 received + 100 lost = 1%. Measuring against
        # packets *sent* would be meaningless -- we cannot lose our own stream.
        assert quality[0].loss_percent == pytest.approx(1.0)

    def test_loss_is_unknown_rather_than_zero_when_nothing_flowed(self, quality):
        assert quality[1].packets_received == 8
        idle = telemetry.LineQuality(sp=3)
        assert idle.loss_percent is None


class TestCalls:
    @pytest.fixture
    def calls(self):
        return telemetry.parse_calls(fixture("callhistory.xml"))

    def test_direction_is_taken_from_the_originating_terminal(self, calls):
        assert calls[0].direction == "inbound"   # From SP1 -> the handset
        assert calls[1].direction == "outbound"  # From PH  -> out a trunk

    def test_peer_and_path(self, calls):
        assert calls[0].peer == "+15551234567"
        assert (calls[0].from_terminal, calls[0].to_terminal) == ("SP1", "PH")

    def test_the_fixtures_outbound_call_reports_the_far_end(self, calls):
        # calls[1] is From PH(1001) -> To SP2(+15559876543).
        assert calls[1].direction == "outbound"
        assert calls[1].peer == "+15559876543"

    def test_ring_and_talk_time_come_from_the_event_gaps(self, calls):
        assert calls[0].answered is True
        assert calls[0].ring_s == 10
        assert calls[0].talk_s == 18

    def test_a_call_spanning_midnight_is_not_negative(self, calls):
        # Connected 23:59:40, ended 00:00:10 the next day.
        assert calls[1].talk_s == 30

    def test_an_unanswered_call_records_ring_time_only(self, calls):
        assert calls[2].answered is False
        assert calls[2].talk_s is None
        assert calls[2].ring_s == 25

    def test_limit(self):
        assert len(telemetry.parse_calls(fixture("callhistory.xml"), limit=2)) == 2


class TestRedaction:
    def test_all_but_the_last_two_digits_are_masked(self):
        assert telemetry.redact_number("+15551234567") == "+xxxxxxxxx67"
        assert telemetry.redact_number("1001") == "xx01"

    def test_short_numbers_and_blanks_are_left_alone(self):
        assert telemetry.redact_number("11") == "11"
        assert telemetry.redact_number("") == ""
        assert telemetry.redact_number(None) is None

    def test_redacting_a_reading_covers_events_too(self):
        reading = telemetry.Telemetry(
            device=telemetry.DeviceStatus(),
            port=telemetry.parse_port(fixture("EXAMPLE_PORT_.xml")),
            quality=[],
            calls=telemetry.parse_calls(fixture("callhistory.xml")),
        )
        telemetry.redact(reading)
        assert reading.port.last_caller == "+xxxxxxxxx67"
        assert reading.calls[0].peer == "+xxxxxxxxx67"
        assert all("1234567" not in e.text for c in reading.calls for e in c.events)


class TestRead:
    def _device(self):
        client = FakeClient(
            pages={
                "DI_S_": fixture("EXAMPLE_STATUS_.xml"),
                "PI_FXS_1_Stats": fixture("EXAMPLE_PORT_.xml"),
                "VS_1_VP_1_L_1_Stats": fixture("EXAMPLE_QUALITY_.xml"),
                "callhistory": fixture("callhistory.xml"),
            }
        )
        return Device(client, Guard(enabled=False))

    def test_read_collects_every_endpoint(self):
        reading = telemetry.read(self._device())
        assert reading.device.model == "OBi200"
        assert reading.port.off_hook is True
        assert len(reading.quality) == 2
        assert len(reading.calls) == 3
        assert reading.errors == {}

    def test_call_history_is_fetched_even_though_it_is_not_in_the_menu(self):
        device = self._device()
        telemetry.read(device)
        assert "callhistory.xml" in device.client.fetches

    def test_a_missing_endpoint_degrades_instead_of_failing(self):
        # Not every model serves every page; partial telemetry beats none.
        device = self._device()
        del device.client.pages["callhistory"]
        reading = telemetry.read(device)
        assert reading.calls == []
        assert "callhistory.xml" in reading.errors
        assert reading.device.model == "OBi200"

    def test_calls_can_be_skipped_entirely(self):
        device = self._device()
        reading = telemetry.read(device, include_calls=False)
        assert reading.calls == []
        assert "callhistory.xml" not in device.client.fetches

    def test_to_dict_is_json_ready_and_includes_derived_loss(self):
        data = telemetry.read(self._device()).to_dict()
        assert data["device"]["model"] == "OBi200"
        assert data["quality"][0]["loss_percent"] == pytest.approx(1.0)
        import json

        json.dumps(data)  # must not raise


class TestRedactCoversIdentity:
    def test_serial_and_mac_are_masked(self):
        reading = telemetry.Telemetry(
            device=telemetry.parse_status(fixture("EXAMPLE_STATUS_.xml")),
            port=telemetry.PortStatus(),
            quality=[],
            calls=[],
        )
        assert reading.device.serial == "0000TESTSERIAL"
        telemetry.redact(reading)
        assert reading.device.serial == "<redacted>"
        assert reading.device.mac == "<redacted>"

    def test_absent_identity_fields_are_left_alone(self):
        reading = telemetry.Telemetry(
            device=telemetry.DeviceStatus(),
            port=telemetry.PortStatus(),
            quality=[],
            calls=[],
        )
        telemetry.redact(reading)
        assert reading.device.serial is None
        assert reading.device.mac is None


class TestCallTimingEdges:
    def test_a_malformed_clock_yields_no_duration(self):
        assert telemetry._seconds("nope") is None
        assert telemetry._seconds("aa:bb:cc") is None
        assert telemetry._elapsed("nope", "00:00:01") is None
        assert telemetry._elapsed("00:00:01", "nope") is None

    def test_empty_event_lines_are_skipped(self):
        calls = telemetry.parse_calls(
            '<CallHistoryFile><CallHistory date="1/1/2026" time="00:00:00">'
            "<Event time=\"00:00:00\"><e0></e0><e1>From PH(100)</e1></Event>"
            "</CallHistory></CallHistoryFile>"
        )
        assert len(calls[0].events) == 1
        assert calls[0].direction == "outbound"

    def test_an_outbound_calls_peer_is_the_number_dialled(self):
        # The old code took the peer from the "From" line unconditionally, so
        # every outbound call reported the OBi's own extension as the peer.
        # The realistic record -- a populated From *and* To -- is the one that
        # exposes it; an empty From() hides the bug entirely.
        calls = telemetry.parse_calls(
            '<CallHistoryFile><CallHistory date="1/1/2026" time="00:00:00">'
            '<Event time="00:00:00"><e0>From PH(1001)</e0>'
            "<e1>To SP1(+15550000001)</e1></Event>"
            "</CallHistory></CallHistoryFile>"
        )
        assert calls[0].direction == "outbound"
        assert calls[0].peer == "+15550000001"
        assert calls[0].from_terminal == "PH"
        assert calls[0].to_terminal == "SP1"

    def test_a_call_with_no_events_at_all(self):
        calls = telemetry.parse_calls(
            '<CallHistoryFile><CallHistory date="1/1/2026" time="00:00:00">'
            "</CallHistory></CallHistoryFile>"
        )
        assert calls[0].direction == "unknown"
        assert calls[0].ring_s is None
