"""End-to-end tests of the command line, against a fake device.

Every test calls ``main(argv)`` exactly as a shell would, and asserts on the
**exit code** as well as the output. The exit codes are a documented contract
-- scripts and agents branch on them -- so a change that turns a 3 into a 1 is
a breaking change even if the message is identical.
"""

from __future__ import annotations

import json
import time

import pytest
from conftest import FakeClient, make_device

from obicfg import cli
from obicfg.guard import Guard


@pytest.fixture
def device(monkeypatch):
    """Point the CLI at a fake OBi and hand the same object to the test."""
    built = make_device()
    monkeypatch.setattr(cli, "_device", lambda args: built)
    return built


@pytest.fixture
def guarded(monkeypatch):
    built = make_device(guard=Guard())
    monkeypatch.setattr(cli, "_device", lambda args: built)
    return built


def run(*argv):
    return cli.main(list(argv))


def out(capsys) -> str:
    return capsys.readouterr().out


def out_json(capsys):
    return json.loads(capsys.readouterr().out)


# -- helpers ---------------------------------------------------------------


class TestHelpers:
    def test_table_of_nothing_is_empty(self):
        assert cli._table([], ["A"]) == ""

    def test_table_pads_to_the_widest_cell(self):
        text = cli._table([["a", "bb"], ["ccc", "d"]], ["H1", "H2"])
        assert text.splitlines()[0] == "H1   H2"

    def test_shorten_truncates_and_flattens(self):
        assert cli._shorten("ab\ncd") == "ab cd"
        assert cli._shorten("x" * 80, 10) == "x" * 9 + "…"

    def test_at_prefix_reads_the_value_from_a_file(self, tmp_path):
        target = tmp_path / "route.txt"
        target.write_text("{(911):sp1}\n")
        assert cli._read_value(f"@{target}") == "{(911):sp1}"
        assert cli._read_value("plain") == "plain"

    def test_confirm_yes_flag_skips_the_prompt(self):
        assert cli._confirm("go?", True) is True

    def test_confirm_refuses_to_guess_when_not_a_terminal(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
        with pytest.raises(cli.ObiError, match="--yes"):
            cli._confirm("go?", False)

    @pytest.mark.parametrize("reply,expected", [("y", True), ("no", False), ("", False)])
    def test_confirm_reads_the_answer(self, monkeypatch, reply, expected):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _: reply)
        assert cli._confirm("go?", False) is expected

    def test_confirm_treats_interruption_as_no(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

        def interrupt(_):
            raise EOFError

        monkeypatch.setattr("builtins.input", interrupt)
        assert cli._confirm("go?", False) is False

    @pytest.mark.parametrize(
        "seconds,expected", [(None, "?"), (3600, "1h 0m"), (90000, "1d 1h 0m")]
    )
    def test_uptime_formatting(self, seconds, expected):
        assert cli._format_uptime(seconds) == expected


# -- connection resolution -------------------------------------------------


class TestDeviceConstruction:
    def _args(self, **overrides):
        import argparse

        defaults = dict(
            host=None, user=None, password=None, password_file=None, port=None,
            scheme=None, timeout=None, transport=None, unsafe=False,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_missing_host_names_the_config_file(self, monkeypatch):
        monkeypatch.setattr(cli.config_mod, "load_config", dict)
        monkeypatch.delenv("OBI_HOST", raising=False)
        with pytest.raises(cli.ObiError, match="no device given"):
            cli._device(self._args())

    def test_flags_win_over_config(self, monkeypatch):
        monkeypatch.setattr(
            cli.config_mod, "load_config", lambda: {"device": {"host": "from-config"}}
        )
        monkeypatch.delenv("OBI_HOST", raising=False)
        device = cli._device(self._args(host="from-flag", port=8080))
        assert device.client.host == "from-flag"
        assert device.client.base_url == "http://from-flag:8080"

    def test_unknown_transport_is_rejected_by_name(self, monkeypatch):
        monkeypatch.setattr(cli.config_mod, "load_config", dict)
        with pytest.raises(cli.ObiError, match="unknown transport"):
            cli._device(self._args(host="x", transport="carrier-pigeon"))

    def test_unsafe_disables_the_guard(self, monkeypatch):
        monkeypatch.setattr(cli.config_mod, "load_config", dict)
        assert cli._device(self._args(host="x", unsafe=True)).guard.enabled is False
        assert cli._device(self._args(host="x")).guard.enabled is True


# -- read-only commands ----------------------------------------------------


class TestReads:
    def test_pages_text_and_json(self, device, capsys):
        assert run("pages") == 0
        assert "VS_1_VP_1_L_2_" in out(capsys)
        assert run("pages", "--json") == 0
        entries = out_json(capsys)
        assert {"page", "label", "aliases"} <= set(entries[0])
        assert "sp2" in next(e["aliases"] for e in entries if e["page"] == "VS_1_VP_1_L_2_")

    def test_pages_titles_costs_a_fetch_but_disambiguates(self, device, capsys):
        assert run("pages", "--titles") == 0
        assert "Example Service" in out(capsys)

    def test_show_text_json_and_changed_filter(self, device, capsys):
        assert run("show", "sp2") == 0
        text = out(capsys)
        assert "Example Service" in text and "ProxyServerPort" in text

        assert run("show", "sp2", "--changed", "--json") == 0
        names = [p["name"] for p in out_json(capsys)["parameters"]]
        assert "Enable" in names and "ProxyServerPort" not in names

    def test_show_reports_when_a_page_needs_a_reboot(self, device, capsys):
        device.client.pages["VS_1_VP_1_L_2_"] = device.client.pages[
            "VS_1_VP_1_L_2_"
        ].replace('reboot_req="false"', 'reboot_req="true"')
        assert run("show", "sp2") == 0
        assert "require a reboot" in out(capsys)

    def test_show_says_so_when_nothing_has_been_changed(self, device, capsys):
        import re as _re

        device.client.pages["VS_1_VP_1_L_2_"] = _re.sub(
            r' current="[^"]*"', "", device.client.pages["VS_1_VP_1_L_2_"]
        )
        assert run("show", "sp2", "--changed") == 0
        assert "factory default" in out(capsys)

    def test_search_found_and_not_found_agree_across_formats(self, device, capsys):
        assert run("search", "InboundCallRoute") == 0
        assert "X_InboundCallRoute" in out(capsys)

        assert run("search", "InboundCallRoute", "--json") == 0
        assert out_json(capsys)

        # No match is exit 1 in both forms -- callers branch on this.
        assert run("search", "zzznope") == 1
        assert "nothing matches" in out(capsys)
        assert run("search", "zzznope", "--json") == 1
        assert out_json(capsys) == []

    def test_get_one_value_prints_it_bare(self, device, capsys):
        assert run("get", "sp2.ProxyServerPort") == 0
        assert out(capsys).strip() == "5060"

    def test_get_several_values_is_labelled(self, device, capsys):
        assert run("get", "sp2.Enable", "sp3.Enable") == 0
        text = out(capsys)
        assert "VS_1_VP_1_L_2_.Enable = false" in text
        assert "VS_1_VP_1_L_3_.Enable = false" in text

    def test_get_json(self, device, capsys):
        assert run("get", "sp2.Enable", "--json") == 0
        assert out_json(capsys) == {"VS_1_VP_1_L_2_.Enable": "false"}

    def test_status_text_and_json(self, device, capsys):
        assert run("status") == 0
        text = out(capsys)
        assert "OBi200" in text and "SP1" in text

        assert run("status", "--json") == 0
        data = out_json(capsys)
        assert data["identity"]["ModelName"] == "OBi200"
        assert [s["service"] for s in data["services"]] == ["SP1", "SP2", "SP3", "SP4"]

    def test_status_tolerates_a_model_without_four_services(self, device, capsys):
        del device.client.pages["VS_1_VP_1_L_4_"]
        assert run("status", "--json") == 0
        assert len(out_json(capsys)["services"]) == 3


# -- telemetry -------------------------------------------------------------


class TestTelemetry:
    def test_text_output_covers_device_services_port_and_calls(self, device, capsys):
        assert run("telemetry") == 0
        text = out(capsys)
        assert "OBi200" in text
        assert "phone port:" in text
        assert "Off Hook" in text
        assert "SP1" in text

    def test_json_output(self, device, capsys):
        assert run("telemetry", "--json") == 0
        data = out_json(capsys)
        assert data["device"]["model"] == "OBi200"
        assert data["quality"][0]["loss_percent"] == pytest.approx(1.0)

    def test_redact_masks_numbers(self, device, capsys):
        assert run("telemetry", "--redact", "--json") == 0
        data = out_json(capsys)
        assert "1234567" not in json.dumps(data)

    def test_no_calls_skips_the_biggest_fetch(self, device, capsys):
        assert run("telemetry", "--no-calls", "--json") == 0
        assert out_json(capsys)["calls"] == []

    def test_a_missing_endpoint_is_reported_not_fatal(self, device, capsys):
        del device.client.pages["PI_FXS_1_Stats"]
        assert run("telemetry") == 0
        assert "unavailable" in out(capsys)

    def test_port_with_no_data_says_so(self, device, capsys):
        device.client.pages["PI_FXS_1_Stats"] = (
            '<?xml version="1.0"?><model><object name="Port Status"></object></model>'
        )
        assert run("telemetry", "--no-calls") == 0
        assert "phone port: no data" in out(capsys)

    def test_watch_repeats_until_interrupted(self, device, capsys, monkeypatch):
        calls = {"n": 0}

        def sleep(_seconds):
            calls["n"] += 1
            if calls["n"] > 1:
                raise KeyboardInterrupt

        monkeypatch.setattr(cli.time, "sleep", sleep)
        assert run("telemetry", "--no-calls", "--watch", "1") == 0
        assert calls["n"] == 2
        assert out(capsys).count("OBi200") == 2


# -- writes ----------------------------------------------------------------


class TestSet:
    def test_a_missing_equals_sign_is_a_usage_error(self, device, capsys):
        assert run("set", "sp2.Enable") == 2

    def test_dry_run_sends_nothing(self, device, capsys):
        assert run("set", "sp2.Enable=true", "--dry-run") == 0
        assert "dry run" in out(capsys)
        assert device.client.writes == []

    def test_dry_run_json_shape(self, device, capsys):
        assert run("set", "sp2.Enable=true", "--dry-run", "--json") == 0
        data = out_json(capsys)
        assert data["dry_run"] is True
        assert data["plan"][0]["old"] == "false"
        assert data["plan"][0]["new"] == "true"
        assert data["applied"] == []

    def test_a_value_already_in_place_is_a_noop(self, device, capsys):
        assert run("set", "sp2.Enable=false") == 0
        assert "nothing to do" in out(capsys)
        assert device.client.writes == []

    def test_noop_in_json_mode(self, device, capsys):
        assert run("set", "sp2.Enable=false", "--json") == 0
        assert out_json(capsys)["plan"][0]["noop"] is True

    def test_a_real_write_is_verified(self, device, capsys):
        assert run("set", "sp2.Enable=true") == 0
        text = out(capsys)
        assert "ok   VS_1_VP_1_L_2_.Enable = 'true'" in text
        assert device.parameter("sp2.Enable", refresh=True).value == "true"

    def test_a_real_write_in_json_reports_verified(self, device, capsys):
        assert run("set", "sp2.Enable=true", "--json") == 0
        data = out_json(capsys)
        assert data["applied"][0]["verified"] is True
        assert data["failures"] == []

    def test_several_settings_on_one_page_go_out_together(self, device, capsys):
        assert run("set", "sp2.Enable=true", "sp2.ProxyServerPort=5061") == 0
        assert len(device.client.writes) == 1

    def test_a_value_from_a_file(self, device, capsys, tmp_path):
        target = tmp_path / "v.txt"
        target.write_text("Lobby phone")
        assert run("set", f"sp2.CallerIDName=@{target}") == 0
        assert device.parameter("sp2.CallerIDName", refresh=True).value == "Lobby phone"

    def test_a_silently_dropped_write_exits_3(self, monkeypatch, capsys):
        built = make_device(deaf_hashes=frozenset({"a0020001"}))
        monkeypatch.setattr(cli, "_device", lambda args: built)
        assert run("set", "sp2.Enable=true") == 3
        text = out(capsys)
        assert "FAIL" in text
        assert "did not apply" in text

    def test_a_silently_dropped_write_in_json(self, monkeypatch, capsys):
        built = make_device(deaf_hashes=frozenset({"a0020001"}))
        monkeypatch.setattr(cli, "_device", lambda args: built)
        assert run("set", "sp2.Enable=true", "--json") == 3
        data = out_json(capsys)
        assert data["failures"][0]["wanted"] == "true"
        assert data["failures"][0]["observed"] == "false"

    def test_no_verify_reports_only_that_it_was_sent(self, monkeypatch, capsys):
        built = make_device(deaf_hashes=frozenset({"a0020001"}))
        monkeypatch.setattr(cli, "_device", lambda args: built)
        # Without the read-back the tool cannot know it failed -- which is
        # exactly why --no-verify is discouraged.
        assert run("set", "sp2.Enable=true", "--no-verify") == 0
        assert "sent VS_1_VP_1_L_2_.Enable" in out(capsys)

    def test_reboot_after_a_successful_write(self, device, capsys):
        assert run("set", "sp2.Enable=true", "--reboot") == 0
        assert device.client.reboots == 1

    def test_no_reboot_on_top_of_a_failed_write(self, monkeypatch, capsys):
        built = make_device(deaf_hashes=frozenset({"a0020001"}))
        monkeypatch.setattr(cli, "_device", lambda args: built)
        assert run("set", "sp2.Enable=true", "--reboot") == 3
        assert built.client.reboots == 0

    def test_the_guard_blocks_a_provisioned_credential(self, guarded, capsys):
        assert run("set", "sp2.AuthUserName=someone") == 4

    def test_the_guard_blocks_a_protected_page_before_resolving_it(self, guarded):
        # DM_S_ is not in the menu at all, so this must be refused on the
        # pattern alone rather than failing with "no such page".
        assert run("set", "DM_S_.ConfigURL=http://example.net") == 4
        assert run("set", "SetupWizard.AnythingAtAll=1") == 4


class TestUnset:
    def test_declining_the_prompt_changes_nothing(self, device, capsys, monkeypatch):
        monkeypatch.setattr(cli, "_confirm", lambda *a: False)
        assert run("unset", "sp2.Enable") == 0
        assert "aborted" in out(capsys)
        assert device.client.writes == []

    def test_reset_uses_the_sentinel(self, device, capsys):
        assert run("--yes", "unset", "sp2.Enable") == 0
        assert device.client.writes[-1] == [("a0020001", "default")]
        assert "ok" in out(capsys)

    def test_reset_json(self, device, capsys):
        assert run("--yes", "unset", "sp2.Enable", "--json") == 0
        assert out_json(capsys)["applied"][0]["verified"] is True

    def test_already_default_is_reported_as_such(self, device, capsys):
        assert run("--yes", "unset", "sp2.ProxyServerPort") == 0
        assert "already at its default" in out(capsys)

    def test_a_refused_reset_exits_3(self, monkeypatch, capsys):
        built = make_device(deaf_hashes=frozenset({"a0020001"}))
        monkeypatch.setattr(cli, "_device", lambda args: built)
        assert run("--yes", "unset", "sp2.Enable") == 3
        assert "FAIL" in out(capsys)


# -- backup, diff, profiles ------------------------------------------------


class TestDump:
    def test_writes_raw_pages_and_a_snapshot(self, device, capsys, tmp_path):
        target = tmp_path / "backup"
        assert run("dump", str(target)) == 0
        assert (target / "snapshot.json").exists()
        assert (target / "VS_1_VP_1_L_2_.xml").exists()
        snapshot = json.loads((target / "snapshot.json").read_text())
        assert snapshot["meta"]["identity"]["ModelName"] == "OBi200"
        assert "keep it out of public repositories" in out(capsys)

    def test_no_raw_writes_only_the_snapshot(self, device, tmp_path):
        target = tmp_path / "b2"
        assert run("dump", str(target), "--no-raw") == 0
        assert not (target / "VS_1_VP_1_L_2_.xml").exists()

    def test_redact_covers_identity_and_values(self, device, capsys, tmp_path):
        target = tmp_path / "b3"
        assert run("dump", str(target), "--redact") == 0
        snapshot = json.loads((target / "snapshot.json").read_text())
        assert snapshot["meta"]["identity"]["SerialNumber"] == "<redacted>"
        assert "keep it out" not in out(capsys)

    def test_include_status_and_all(self, device, tmp_path):
        target = tmp_path / "b4"
        assert run("dump", str(target), "--include-status", "--all", "--no-raw") == 0
        snapshot = json.loads((target / "snapshot.json").read_text())
        assert "DI_S_" in snapshot["pages"]


class TestDiff:
    def test_no_differences(self, device, capsys, tmp_path):
        target = tmp_path / "b"
        run("dump", str(target), "--no-raw")
        capsys.readouterr()
        assert run("diff", str(target)) == 0
        assert "no differences" in out(capsys)

    def test_differences_exit_1_and_are_listed(self, device, capsys, tmp_path):
        target = tmp_path / "b"
        run("dump", str(target), "--no-raw")
        run("set", "sp2.CallerIDName=Lobby")
        capsys.readouterr()
        assert run("diff", str(target)) == 1
        text = out(capsys)
        assert "VS_1_VP_1_L_2_.CallerIDName" in text
        assert "1 difference(s)" in text

    def test_json_form(self, device, capsys, tmp_path):
        target = tmp_path / "b"
        run("dump", str(target), "--no-raw")
        run("set", "sp2.CallerIDName=Lobby")
        capsys.readouterr()
        assert run("diff", str(target), "--json") == 1
        data = out_json(capsys)
        assert data["differences"][0]["after"] == "Lobby"
        assert data["redacted_skipped"] == 0

    def test_two_snapshots_against_each_other(self, device, capsys, tmp_path):
        first, second = tmp_path / "a", tmp_path / "b"
        run("dump", str(first), "--no-raw")
        run("set", "sp2.CallerIDName=Lobby")
        run("dump", str(second), "--no-raw")
        capsys.readouterr()
        assert run("diff", str(first), "--against", str(second)) == 1
        assert "Lobby" in out(capsys)

    def test_redacted_values_are_skipped_with_a_note(self, device, capsys, tmp_path):
        target = tmp_path / "b"
        run("dump", str(target), "--no-raw", "--redact")
        capsys.readouterr()
        assert run("diff", str(target)) == 0
        assert "redacted value(s) cannot be compared" in out(capsys)

    def test_a_missing_snapshot_is_an_error(self, device, tmp_path):
        assert run("diff", str(tmp_path / "nope")) == 1

    def test_a_corrupt_snapshot_is_an_error(self, device, tmp_path):
        bad = tmp_path / "snapshot.json"
        bad.write_text("{not json")
        assert run("diff", str(bad)) == 1


PROFILE = """
name = "Test profile"
description = "Used by the tests."

[settings]
"sp2.CallerIDName" = "Lobby"
"""


class TestApply:
    def _profile(self, tmp_path, text=PROFILE, name="p.toml"):
        path = tmp_path / name
        path.write_text(text)
        return str(path)

    def test_dry_run_shows_the_plan_and_writes_nothing(self, device, capsys, tmp_path):
        assert run("apply", self._profile(tmp_path), "--dry-run") == 0
        text = out(capsys)
        assert "Test profile" in text and "dry run" in text
        assert device.client.writes == []

    def test_applying_writes_and_verifies(self, device, capsys, tmp_path):
        assert run("--yes", "apply", self._profile(tmp_path)) == 0
        assert "ok" in out(capsys)
        assert device.parameter("sp2.CallerIDName", refresh=True).value == "Lobby"

    def test_applying_twice_is_a_noop(self, device, capsys, tmp_path):
        path = self._profile(tmp_path)
        run("--yes", "apply", path)
        capsys.readouterr()
        assert run("--yes", "apply", path) == 0
        assert "already matches this profile" in out(capsys)

    def test_declining_the_prompt_writes_nothing(self, device, capsys, tmp_path, monkeypatch):
        monkeypatch.setattr(cli, "_confirm", lambda *a: False)
        assert run("apply", self._profile(tmp_path)) == 0
        assert "aborted" in out(capsys)
        assert device.client.writes == []

    def test_require_stops_the_wrong_device(self, device, tmp_path):
        text = PROFILE + '\n[require]\nmodel = "OBi110"\n'
        assert run("apply", self._profile(tmp_path, text), "--dry-run") == 1

    def test_require_matching_is_allowed(self, device, tmp_path):
        text = PROFILE + '\n[require]\nmodel = "OBi2"\nfirmware = "3.2"\n'
        assert run("apply", self._profile(tmp_path, text), "--dry-run") == 0

    def test_reset_entries_are_planned_and_applied(self, device, capsys, tmp_path):
        text = PROFILE + '\n[reset]\nparameters = ["sp2.Enable"]\n'
        assert run("--yes", "apply", self._profile(tmp_path, text)) == 0
        assert device.parameter("sp2.Enable", refresh=True).is_default

    def test_a_failed_write_exits_3(self, monkeypatch, capsys, tmp_path):
        built = make_device(deaf_hashes=frozenset({"a0020007"}))
        monkeypatch.setattr(cli, "_device", lambda args: built)
        assert run("--yes", "apply", self._profile(tmp_path)) == 3

    def test_reboot_from_the_profile_and_the_override(self, device, tmp_path):
        text = PROFILE + "\n[after]\nreboot = true\n"
        path = self._profile(tmp_path, text)
        assert run("--yes", "apply", path, "--no-reboot") == 0
        assert device.client.reboots == 0

        device.client.pages["VS_1_VP_1_L_2_"] = make_device().client.pages[
            "VS_1_VP_1_L_2_"
        ]
        device._pages.clear()
        assert run("--yes", "apply", path) == 0
        assert device.client.reboots == 1

    def test_reboot_that_never_comes_back_is_reported(self, device, capsys, tmp_path, monkeypatch):
        monkeypatch.setattr(cli.Device, "reboot", lambda self, **kw: False)
        text = PROFILE + "\n[after]\nreboot = true\n"
        assert run("--yes", "apply", self._profile(tmp_path, text), "--reboot") == 0
        assert "check manually" in out(capsys)


# -- device control --------------------------------------------------------


class TestReboot:
    def test_declining_does_nothing(self, device, capsys, monkeypatch):
        monkeypatch.setattr(cli, "_confirm", lambda *a: False)
        assert run("reboot") == 0
        assert device.client.reboots == 0

    def test_requesting_a_reboot(self, device, capsys):
        assert run("--yes", "reboot") == 0
        assert device.client.reboots == 1
        assert "reboot requested" in out(capsys)

    def test_waiting_for_it_to_return(self, device, capsys, monkeypatch):
        monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
        assert run("--yes", "reboot", "--wait") == 0
        assert "back up" in out(capsys)

    def test_a_device_that_stays_down_exits_1(self, device, capsys, monkeypatch):
        monkeypatch.setattr(cli.Device, "reboot", lambda self, **kw: False)
        assert run("--yes", "reboot", "--wait") == 1


class TestProbe:
    def test_a_read_only_scratch_parameter_is_refused(self, device):
        assert run("--yes", "probe", "--scratch", "sp2.RegistrationState") == 1

    def test_declining_leaves_the_device_alone(self, device, capsys, monkeypatch):
        monkeypatch.setattr(cli, "_confirm", lambda *a: False)
        assert run("probe", "--scratch", "sp2.CallerIDName") == 0
        assert device.client.writes == []

    def test_all_three_encodings_and_the_original_restored(self, device, capsys):
        assert run("--yes", "probe", "--scratch", "sp2.CallerIDName") == 0
        text = out(capsys)
        assert text.count("ok  ") == 3
        # CallerIDName started at its factory default, so restoring means reset.
        assert device.parameter("sp2.CallerIDName", refresh=True).is_default

    def test_a_non_default_scratch_value_is_put_back_exactly(self, device, capsys):
        run("set", "sp2.CallerIDName=Before")
        capsys.readouterr()
        assert run("--yes", "probe", "--scratch", "sp2.CallerIDName", "--json") == 0
        assert device.parameter("sp2.CallerIDName", refresh=True).value == "Before"

    def test_a_device_that_drops_the_write_exits_3(self, monkeypatch, capsys):
        built = make_device(deaf_hashes=frozenset({"a0020007"}))
        monkeypatch.setattr(cli, "_device", lambda args: built)
        assert run("--yes", "probe", "--scratch", "sp2.CallerIDName") == 3

    def test_a_validation_failure_is_recorded_not_raised(self, monkeypatch, capsys):
        built = make_device()
        monkeypatch.setattr(cli, "_device", lambda args: built)
        # CallerIDName caps at 16 characters; "obicfg#test" fits, but force a
        # failure to prove the probe records rather than aborts.
        original = cli.Device.plan

        def explode(self, assignments):
            if any(" " in str(v) for _, v in assignments):
                raise cli.ObiError("nope")
            return original(self, assignments)

        monkeypatch.setattr(cli.Device, "plan", explode)
        assert run("--yes", "probe", "--scratch", "sp2.CallerIDName") == 3
        assert "FAIL" in out(capsys)


# -- top level -------------------------------------------------------------


class TestMain:
    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exit_info:
            run("--version")
        assert exit_info.value.code == 0

    def test_no_subcommand_is_a_usage_error(self, capsys):
        with pytest.raises(SystemExit) as exit_info:
            run()
        assert exit_info.value.code == 2

    def test_transport_errors_exit_1(self, monkeypatch, capsys):
        def boom(args):
            raise cli.ObiError("cannot reach anything")

        monkeypatch.setattr(cli, "_device", boom)
        assert run("status") == 1
        assert "cannot reach anything" in capsys.readouterr().err

    def test_interruption_exits_1_quietly(self, monkeypatch, capsys):
        def boom(args):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "_device", boom)
        assert run("status") == 1
        assert "interrupted" in capsys.readouterr().err

    def test_verification_errors_exit_3(self, monkeypatch, capsys):
        def boom(args):
            raise cli.VerificationError("did not apply")

        monkeypatch.setattr(cli, "_device", boom)
        assert run("status") == 3

    def test_module_entry_point_runs(self):
        import runpy

        with pytest.raises(SystemExit):
            runpy.run_module("obicfg", run_name="__main__")


class TestAuditFixes:
    """Regressions for defects found by an audit of the skill that drives this.

    Each of these was a real hole: a destructive command with no preview, a
    backup that could eat the previous backup, and a "sharing-safe" flag that
    left a device fingerprint in the output.
    """

    def test_unset_has_a_dry_run_and_sends_nothing(self, device, capsys):
        assert run("unset", "sp2.Enable", "--dry-run") == 0
        text = out(capsys)
        assert "dry run" in text
        assert "'false' -> 'true'" in text  # what the reset would restore
        assert device.client.writes == []

    def test_unset_dry_run_json(self, device, capsys):
        assert run("unset", "sp2.Enable", "--dry-run", "--json") == 0
        data = out_json(capsys)
        assert data["dry_run"] is True
        assert data["plan"][0]["new"] == "true"
        assert data["applied"] == []

    def test_unset_shows_the_plan_before_asking(self, device, capsys, monkeypatch):
        monkeypatch.setattr(cli, "_confirm", lambda *a: False)
        assert run("unset", "sp2.Enable") == 0
        assert "plan:" in out(capsys)

    def test_unset_dry_run_respects_the_guard(self, guarded):
        assert run("unset", "sp2.AuthUserName", "--dry-run") == 4

    def test_dump_refuses_to_overwrite_a_restore_point(self, device, capsys, tmp_path):
        target = tmp_path / "backup"
        assert run("dump", str(target), "--no-raw") == 0
        assert run("dump", str(target), "--no-raw") == 1
        assert "refusing to overwrite" in capsys.readouterr().err

    def test_dump_force_overwrites_deliberately(self, device, tmp_path):
        target = tmp_path / "backup"
        run("dump", str(target), "--no-raw")
        assert run("dump", str(target), "--no-raw", "--force") == 0

    def test_telemetry_redact_covers_the_device_fingerprint(self, device, capsys):
        assert run("telemetry", "--redact", "--json") == 0
        data = out_json(capsys)
        assert data["device"]["serial"] == "<redacted>"
        assert data["device"]["mac"] == "<redacted>"
        # The model and firmware stay: they are not identifying.
        assert data["device"]["model"] == "OBi200"

    def test_telemetry_without_redact_keeps_the_real_values(self, device, capsys):
        assert run("telemetry", "--json") == 0
        assert out_json(capsys)["device"]["serial"] == "0000TESTSERIAL"

    def test_the_non_interactive_error_does_not_just_say_pass_yes(self, monkeypatch):
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
        with pytest.raises(cli.ObiError, match="Get the human's approval"):
            cli._confirm("go?", False)


def test_show_json_exposes_the_declared_limits(device, capsys):
    # After an exit 3 these are what a caller checks the value against, so
    # they have to be in the machine-readable output, not only the docs.
    assert run("show", "sp2", "--json") == 0
    fields = {p["name"]: p for p in out_json(capsys)["parameters"]}
    assert fields["ProxyServerPort"]["type"] == "unsignedInt"
    assert (fields["ProxyServerPort"]["min"], fields["ProxyServerPort"]["max"]) == (0, 65535)
    assert fields["CallerIDName"]["max_length"] == 16
    assert fields["ProxyServerTransport"]["options"] == ["UDP", "TCP", "TLS"]


class TestSecondAuditFixes:
    """Regressions for defects found by an agent following the skill for real.

    The first of these is the important one: a dry run used to report
    `verified: true` with an empty `failures` list, which is exactly the
    success test the skill tells an agent to apply. A preview satisfied it.
    """

    def test_a_dry_run_never_claims_to_have_verified_anything(self, device, capsys):
        assert run("set", "sp2.Enable=true", "--dry-run", "--json") == 0
        data = out_json(capsys)
        assert data["dry_run"] is True
        assert data["verified"] is False
        assert data["applied"] == []

    def test_a_real_write_does_claim_it(self, device, capsys):
        assert run("set", "sp2.Enable=true", "--json") == 0
        data = out_json(capsys)
        assert (data["dry_run"], data["verified"]) == (False, True)
        assert data["applied"][0]["verified"] is True

    def test_an_all_noop_run_is_not_labelled_a_dry_run(self, device, capsys):
        # Nothing was sent, but not because a preview was asked for.
        assert run("set", "sp2.Enable=false", "--json") == 0
        assert out_json(capsys)["dry_run"] is False

    def test_apply_json_is_actually_json(self, device, capsys, tmp_path):
        path = tmp_path / "p.toml"
        path.write_text('name = "T"\n[settings]\n"sp2.CallerIDName" = "Lobby"\n')
        assert run("apply", str(path), "--dry-run", "--json") == 0
        data = out_json(capsys)
        assert data["profile"] == "T"
        assert data["dry_run"] is True
        assert data["plan"][0]["new"] == "Lobby"

        assert run("--yes", "apply", str(path), "--json") == 0
        data = out_json(capsys)
        assert data["applied"][0]["verified"] is True

    def test_apply_json_when_the_device_already_matches(self, device, capsys, tmp_path):
        path = tmp_path / "p.toml"
        path.write_text('name = "T"\n[settings]\n"sp2.Enable" = false\n')
        assert run("apply", str(path), "--json") == 0
        assert out_json(capsys)["plan"][0]["noop"] is True

    def test_apply_json_includes_planned_resets(self, device, capsys, tmp_path):
        path = tmp_path / "p.toml"
        path.write_text('name = "T"\n[reset]\nparameters = ["sp2.Enable"]\n')
        assert run("apply", str(path), "--dry-run", "--json") == 0
        assert out_json(capsys)["plan"][0]["path"] == "VS_1_VP_1_L_2_.Enable"

    def test_apply_text_shows_what_a_reset_would_restore(self, device, capsys, tmp_path):
        path = tmp_path / "p.toml"
        path.write_text('name = "T"\n[reset]\nparameters = ["sp2.Enable"]\n')
        assert run("apply", str(path), "--dry-run") == 0
        assert "factory default 'true'" in out(capsys)

    @pytest.mark.parametrize(
        "argv,kind,code",
        [
            (["get", "sp2.NoSuchParameter", "--json"], "not_found", 1),
            (["set", "sp2.ProxyServerPort=99999", "--json"], "invalid_value", 1),
            (["set", "notapair", "--json"], "usage", 2),
        ],
    )
    def test_errors_are_parseable_when_json_was_requested(
        self, device, capsys, argv, kind, code
    ):
        # Otherwise json.loads(stdout) raises on empty input for every error.
        assert run(*argv) == code
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["kind"] == kind
        assert payload["exit"] == code
        assert payload["error"]
        assert "obicfg:" in captured.err  # humans still get the prose

    def test_the_guard_error_is_parseable_too(self, guarded, capsys):
        assert run("set", "sp2.AuthUserName=x", "--json") == 4
        assert json.loads(capsys.readouterr().out)["kind"] == "guard"

    def test_an_unreachable_device_is_parseable(self, monkeypatch, capsys):
        from obicfg.errors import TransportError

        def boom(args):
            raise TransportError("cannot reach 192.0.2.1")

        monkeypatch.setattr(cli, "_device", boom)
        assert run("status", "--json") == 1
        assert json.loads(capsys.readouterr().out)["kind"] == "unreachable"

    def test_bad_credentials_are_parseable(self, monkeypatch, capsys):
        from obicfg.errors import AuthError

        def boom(args):
            raise AuthError("rejected the credentials")

        monkeypatch.setattr(cli, "_device", boom)
        assert run("status", "--json") == 1
        assert json.loads(capsys.readouterr().out)["kind"] == "auth"

    def test_a_verification_error_is_parseable(self, monkeypatch, capsys):
        def boom(args):
            raise cli.VerificationError("did not apply")

        monkeypatch.setattr(cli, "_device", boom)
        assert run("status", "--json") == 3
        assert json.loads(capsys.readouterr().out)["kind"] == "not_applied"

    def test_errors_stay_prose_only_without_json(self, device, capsys):
        assert run("get", "sp2.NoSuchParameter") == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "obicfg:" in captured.err

    def test_status_can_redact_the_fingerprint(self, device, capsys):
        assert run("status", "--redact", "--json") == 0
        identity = out_json(capsys)["identity"]
        assert identity["SerialNumber"] == "<redacted>"
        assert identity["ModelName"] == "OBi200"

    def test_status_without_redact_shows_it(self, device, capsys):
        assert run("status", "--json") == 0
        assert out_json(capsys)["identity"]["SerialNumber"] == "0000TESTSERIAL"

    def test_skipped_call_history_is_distinguishable_from_no_calls(self, device, capsys):
        assert run("telemetry", "--no-calls", "--json") == 0
        data = out_json(capsys)
        assert data["calls"] == [] and data["calls_included"] is False

        assert run("telemetry", "--json") == 0
        data = out_json(capsys)
        assert data["calls"] and data["calls_included"] is True

    def test_calls_expose_answered_in_json(self, device, capsys):
        # Workflow F tells agents to read `answered`; it has to be there.
        assert run("telemetry", "--json") == 0
        assert out_json(capsys)["calls"][0]["answered"] is True

    def test_the_port_can_come_from_config_and_environment(self, monkeypatch):
        import argparse

        args = argparse.Namespace(
            host="x", user=None, password=None, password_file=None, port=None,
            scheme=None, timeout=None, transport=None, unsafe=False,
        )
        monkeypatch.setattr(
            cli.config_mod, "load_config", lambda: {"device": {"port": 8080}}
        )
        monkeypatch.delenv("OBI_PORT", raising=False)
        assert cli._device(args).client.port == 8080
        monkeypatch.setenv("OBI_PORT", "9090")
        assert cli._device(args).client.port == 9090

    def test_a_nonsense_port_is_rejected_clearly(self, monkeypatch):
        import argparse

        args = argparse.Namespace(
            host="x", user=None, password=None, password_file=None, port=None,
            scheme=None, timeout=None, transport=None, unsafe=False,
        )
        monkeypatch.setattr(cli.config_mod, "load_config", dict)
        monkeypatch.setenv("OBI_PORT", "not-a-port")
        with pytest.raises(cli.ObiError, match="port must be a number"):
            cli._device(args)

    def test_an_empty_port_setting_means_the_default(self, monkeypatch):
        import argparse

        args = argparse.Namespace(
            host="x", user=None, password=None, password_file=None, port=None,
            scheme=None, timeout=None, transport=None, unsafe=False,
        )
        monkeypatch.setattr(cli.config_mod, "load_config", dict)
        monkeypatch.setenv("OBI_PORT", "")
        assert cli._device(args).client.port is None


class TestThirdAuditFixes:
    """Regressions for an adversarial code review.

    Two of these were the tool failing at its own stated purpose: a `dump
    --redact` that wrote plaintext passwords beside the redacted snapshot,
    and a `probe` that announced "restored" without checking.
    """

    def test_redacted_dump_does_not_write_unredactable_raw_xml(
        self, device, capsys, tmp_path
    ):
        target = tmp_path / "b"
        assert run("dump", str(target), "--redact") == 0
        assert (target / "snapshot.json").exists()
        assert not list(target.glob("*.xml"))
        text = out(capsys)
        assert "raw per-page XML" in text
        # The secret must not be anywhere in the directory.
        for path in target.iterdir():
            assert "s3cret" not in path.read_text()

    def test_an_unredacted_dump_still_warns_about_the_raw_xml(
        self, device, capsys, tmp_path
    ):
        assert run("dump", str(tmp_path / "b"), ) == 0
        assert "keep it out of public repositories" in out(capsys)

    def test_redact_with_no_raw_is_quiet_about_xml(self, device, capsys, tmp_path):
        assert run("dump", str(tmp_path / "b"), "--redact", "--no-raw") == 0
        assert "raw per-page XML" not in out(capsys)

    def test_probe_reports_and_fails_when_it_cannot_restore(
        self, monkeypatch, capsys
    ):
        built = make_device()
        monkeypatch.setattr(cli, "_device", lambda args: built)
        run("set", "sp2.CallerIDName=Before")
        capsys.readouterr()

        real_apply = cli.Device.apply
        state = {"probes": 0}

        def apply(self, changes, **kwargs):
            state["probes"] += 1
            if state["probes"] > 3:  # the restore
                self.client.deaf_hashes = frozenset({"a0020007"})
            return real_apply(self, changes, **kwargs)

        monkeypatch.setattr(cli.Device, "apply", apply)
        assert run("--yes", "probe", "--scratch", "sp2.CallerIDName") == 3
        text = out(capsys)
        assert "could NOT be restored" in text
        assert "restored VS_1_VP_1_L_2_.CallerIDName to 'Before'" not in text

    def test_probe_does_not_call_an_already_correct_value_a_failure(
        self, monkeypatch, capsys
    ):
        built = make_device()
        monkeypatch.setattr(cli, "_device", lambda args: built)
        run("set", "sp2.CallerIDName=obicfgtest")
        capsys.readouterr()
        # The first probe value is already in place; that is a successful
        # round-trip, not the firmware rejecting a plain write.
        assert run("--yes", "probe", "--scratch", "sp2.CallerIDName") == 0
        assert "FAIL" not in out(capsys)

    @pytest.mark.parametrize(
        "argv,message",
        [
            (["get", "Enable"], "not a parameter path"),
            (["search", "["], "invalid regular expression"),
            (["set", "sp2.CallerIDName=@/no/such/file"], "cannot read value from"),
        ],
    )
    def test_input_errors_do_not_escape_as_tracebacks(self, device, capsys, argv, message):
        assert run(*argv) in (1, 2)
        assert message in capsys.readouterr().err

    @pytest.mark.parametrize(
        "var,value,message",
        [
            ("OBI_TIMEOUT", "soon", "timeout must be a number"),
            ("OBI_TIMEOUT", "-1", "greater than zero"),
            ("OBI_SCHEME", "ftp", "scheme must be http or https"),
        ],
    )
    def test_bad_connection_settings_are_reported_not_raised(
        self, monkeypatch, capsys, var, value, message
    ):
        monkeypatch.setattr(cli.config_mod, "load_config", dict)
        monkeypatch.setenv("OBI_HOST", "192.0.2.1")
        monkeypatch.setenv(var, value)
        assert run("status") == 1
        assert message in capsys.readouterr().err

    def test_diff_reads_the_device_at_the_snapshots_own_scope(
        self, device, capsys, tmp_path
    ):
        # A wide snapshot compared against a default read of the device used
        # to report every parameter the default read excludes as deleted.
        target = tmp_path / "wide"
        assert run("dump", str(target), "--include-status", "--all", "--no-raw") == 0
        capsys.readouterr()
        assert run("diff", str(target)) == 0
        assert "no differences" in out(capsys)

    def test_a_snapshot_records_its_scope(self, device, capsys, tmp_path):
        target = tmp_path / "b"
        run("dump", str(target), "--include-status", "--all", "--no-raw")
        scope = json.loads((target / "snapshot.json").read_text())["scope"]
        assert scope == {"include_status": True, "writable_only": False}


def test_probe_reports_when_the_original_value_cannot_be_written_back(
    monkeypatch, capsys
):
    """The destructive case: a real device holds values this tool would reject.

    A provisioned or web-UI-set value can exceed the page's declared limit,
    because it never went through this tool's validation. Probing such a
    parameter used to leave it holding a test string, with no message and
    exit 1 from an unhandled error.
    """
    built = make_device()
    # 20 characters, where the fixture declares maxLength 16.
    built.client.pages["VS_1_VP_1_L_2_"] = built.client.pages[
        "VS_1_VP_1_L_2_"
    ].replace(
        '<value hash="a0020007" type="input" default="" >',
        '<value hash="a0020007" type="input" default="" current="Front Desk Extension" >',
    )
    monkeypatch.setattr(cli, "_device", lambda args: built)
    assert run("--yes", "probe", "--scratch", "sp2.CallerIDName") == 3
    text = out(capsys)
    assert "restore FAILED" in text
    assert "could NOT be restored" in text
    assert "'Front Desk Extension'" in text  # tells you what to put back


class TestListen:
    """The syslog listener, driven through the CLI over real UDP sockets.

    The datagrams are sent *before* the command runs. A UDP socket buffers
    what arrives while nobody is reading, so this is deterministic -- unlike
    sending from a thread and hoping the listener has bound by then, where a
    datagram that arrives too early is dropped without trace and the test
    either flakes or waits forever.
    """

    def _prepared(self, monkeypatch, *payloads, timeout=1.0):
        """Bind a socket, queue the payloads on it, and hand it to the CLI."""
        import socket as _socket

        from obicfg import syslog as syslog_mod

        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(timeout)
        port = sock.getsockname()[1]

        sender = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        for payload in payloads:
            sender.sendto(payload, ("127.0.0.1", port))
        sender.close()
        time.sleep(0.1)  # let the kernel deliver into the receive buffer

        listener = syslog_mod.Listener(bind="127.0.0.1", port=port, sock=sock)
        monkeypatch.setattr(syslog_mod, "open_listener", lambda *a, **k: listener)
        return listener

    def _listen(self, *extra):
        # --seconds is a backstop: a missing datagram must fail, not hang.
        return run("listen", "--bind", "127.0.0.1", "--seconds", "15", *extra)

    def test_it_prints_events_as_they_arrive(self, capsys, monkeypatch):
        self._prepared(monkeypatch, b"<6> CALL:Incoming call from +15551234567 on SP2\x00")
        assert self._listen("--count", "1") == 0
        text = out(capsys)
        assert "listening on 127.0.0.1" in text
        assert "SP2" in text and "[ringing]" in text

    def test_json_output(self, capsys, monkeypatch):
        self._prepared(monkeypatch, b"<7> SIP:SP1 Registered")
        assert self._listen("--count", "1", "--json") == 0
        event = json.loads(out(capsys).strip())
        assert event["call_event"] == "registered"
        assert event["sp"] == 1
        assert event["severity_name"] == "debug"
        assert event["source"] == "127.0.0.1"

    def test_calls_only_drops_the_noise(self, capsys, monkeypatch):
        self._prepared(monkeypatch, b"<7> SYS:noise", b"<6> Call Ended")
        assert self._listen("--count", "2", "--calls-only", "--json") == 0
        events = [json.loads(x) for x in out(capsys).splitlines() if x.startswith("{")]
        assert [e["call_event"] for e in events] == ["ended"]

    def test_filter_keeps_only_matching_lines(self, capsys, monkeypatch):
        self._prepared(monkeypatch, b"<7> SYS:noise", b"<6> Call Ended")
        assert self._listen("--count", "2", "--filter", "noise", "--json") == 0
        events = [json.loads(x) for x in out(capsys).splitlines() if x.startswith("{")]
        assert len(events) == 1 and "noise" in events[0]["raw"]

    def test_it_stops_on_a_deadline_when_nothing_arrives(self, capsys, monkeypatch):
        self._prepared(monkeypatch)
        assert run("listen", "--bind", "127.0.0.1", "--seconds", "0.1") == 0

    def test_a_closed_socket_ends_the_stream(self, capsys, monkeypatch):
        listener = self._prepared(monkeypatch, b"<6> Call Ended")
        listener.sock.close()
        assert self._listen("--json") == 0

    def test_ctrl_c_is_a_clean_exit(self, capsys, monkeypatch):
        from obicfg import syslog as syslog_mod

        self._prepared(monkeypatch)

        def interrupt(*a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(syslog_mod, "events", interrupt)
        assert self._listen() == 0

    def test_a_privileged_port_says_how_to_avoid_it(self, capsys, monkeypatch):
        from obicfg import syslog as syslog_mod

        def refuse(*a, **k):
            raise OSError("Permission denied")

        monkeypatch.setattr(syslog_mod, "open_listener", refuse)
        assert run("listen", "--listen-port", "514") == 1
        assert "--listen-port 5514" in capsys.readouterr().err

    def test_a_high_port_failure_omits_the_root_hint(self, capsys, monkeypatch):
        from obicfg import syslog as syslog_mod

        def refuse(*a, **k):
            raise OSError("Address already in use")

        monkeypatch.setattr(syslog_mod, "open_listener", refuse)
        assert run("listen", "--listen-port", "5515") == 1
        err = capsys.readouterr().err
        assert "already in use" in err and "need root" not in err

    def test_setup_prints_commands_and_runs_nothing(self, capsys, monkeypatch):
        monkeypatch.setattr(
            cli.config_mod, "load_config", lambda: {"device": {"host": "192.0.2.50"}}
        )
        monkeypatch.setattr(cli.syslog_mod, "local_address_for", lambda h: "192.0.2.71")
        assert run("listen", "--setup") == 0
        text = out(capsys)
        assert "obicfg set admin.Server=192.0.2.71" in text
        assert "admin.Port=5514" in text
        assert "Not run for you" in text

    def test_setup_without_a_configured_host(self, capsys, monkeypatch):
        monkeypatch.setattr(cli.config_mod, "load_config", dict)
        monkeypatch.delenv("OBI_HOST", raising=False)
        assert run("listen", "--setup") == 0
        assert "<your address>" in out(capsys)
