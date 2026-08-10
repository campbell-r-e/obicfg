from __future__ import annotations

import pytest
from conftest import FakeClient

from obicfg.device import Device, count_redacted, diff_snapshots
from obicfg.errors import GuardError, ResolutionError, VerificationError
from obicfg.guard import Guard


@pytest.fixture
def device():
    return Device(FakeClient(), Guard(enabled=False))


def test_pages_come_from_the_devices_own_menu(device):
    assert device.page_names() == ["EXAMPLE_SYS_", "EXAMPLE_SVC_"]
    assert device.label("EXAMPLE_SVC_") == "Example Service"


def test_status_pages_are_excluded_from_config_pages():
    client = FakeClient()
    client.menu = client.menu.replace("EXAMPLE_SYS_", "DI_S_")
    client.pages["DI_S_"] = client.pages.pop("EXAMPLE_SYS_")
    device = Device(client, Guard(enabled=False))
    assert "DI_S_" in device.page_names()
    assert "DI_S_" not in device.config_pages()


def test_parameter_resolution_and_errors(device):
    assert device.parameter("EXAMPLE_SVC_.Enable").hash == "aaaa0001"
    with pytest.raises(ResolutionError, match="no page matches"):
        device.parameter("NOPE_.Enable")
    with pytest.raises(ResolutionError, match="Did you mean: ProxyServerPort"):
        device.parameter("EXAMPLE_SVC_.ProxyServerPor")


def test_search_finds_by_name_or_description(device):
    paths = [p.path for p in device.search("InboundCallRoute")]
    assert paths == ["EXAMPLE_SVC_.X_InboundCallRoute"]
    # Descriptions are searched too, so you can find a setting by what it does.
    assert any(p.name == "CallerIDName" for p in device.search("Caller name"))


def test_plan_marks_unchanged_settings_as_noops(device):
    changes = device.plan([("EXAMPLE_SVC_.Enable", False)])
    assert changes[0].noop is True
    changes = device.plan([("EXAMPLE_SVC_.Enable", True)])
    assert (changes[0].old, changes[0].new, changes[0].noop) == ("false", "true", False)


def test_apply_writes_verifies_and_batches_by_page(device):
    changes = device.plan(
        [
            ("EXAMPLE_SVC_.Enable", True),
            ("EXAMPLE_SVC_.ProxyServerPort", 5061),
            ("EXAMPLE_SVC_.ProxyServerTransport", "tls"),
        ]
    )
    device.apply(changes)
    assert all(c.applied and c.verified for c in changes)
    # One page, therefore one request -- not three round trips.
    assert len(device.client.writes) == 1
    assert device.parameter("EXAMPLE_SVC_.ProxyServerTransport", refresh=True).value == "TLS"


def test_noops_are_never_sent(device):
    changes = device.plan([("EXAMPLE_SVC_.Enable", False)])
    device.apply(changes)
    assert device.client.writes == []


def test_a_silently_dropped_write_is_caught():
    # The failure mode this tool exists for: HTTP 200, nothing changed.
    device = Device(FakeClient(deaf_hashes=frozenset({"aaaa0002"})), Guard(enabled=False))
    changes = device.plan([("EXAMPLE_SVC_.ProxyServerPort", 5061)])
    device.apply(changes)
    assert changes[0].applied is True
    assert changes[0].verified is False
    assert changes[0].observed == "5060"
    assert device.verify_failures(changes) == changes
    with pytest.raises(VerificationError, match="did not apply"):
        device.assert_applied(changes)


def test_reset_to_default_uses_the_sentinel_and_confirms(device):
    parameter = device.parameter("EXAMPLE_SVC_.Enable")
    assert parameter.is_default is False
    changes = device.reset_to_default(["EXAMPLE_SVC_.Enable"])
    assert device.client.writes[-1] == [("aaaa0001", "default")]
    assert changes[0].verified is True
    assert device.parameter("EXAMPLE_SVC_.Enable", refresh=True).value == "true"


def test_reset_of_an_already_default_parameter_is_a_noop(device):
    changes = device.reset_to_default(["EXAMPLE_SVC_.ProxyServerPort"])
    assert changes[0].noop is True
    assert device.client.writes == []


def test_guard_blocks_provisioned_credentials_by_default():
    device = Device(FakeClient(), Guard())
    with pytest.raises(GuardError, match="provisioning macro"):
        device.plan([("EXAMPLE_SVC_.AuthUserName", "someone")])
    # The password next to it is protected by association.
    with pytest.raises(GuardError, match="AuthUserName"):
        device.plan([("EXAMPLE_SVC_.AuthPassword", "hunter2")])
    # Ordinary settings on the same page stay editable.
    assert device.plan([("EXAMPLE_SVC_.CallerIDName", "Front desk")])


def test_identity_prefers_current_over_default(device):
    identity = device.identity()
    # ModelName's factory default is OBi100 on every model in the family.
    assert identity["ModelName"] == "OBi200"
    assert identity["SoftwareVersion"].startswith("3.2.2")


def test_snapshot_and_diff(device):
    before = device.snapshot()
    device.apply(device.plan([("EXAMPLE_SVC_.CallerIDName", "Lobby")]))
    after = device.snapshot()
    assert diff_snapshots(before, after) == [
        ("EXAMPLE_SVC_.CallerIDName", "", "Lobby")
    ]
    assert diff_snapshots(before, before) == []


def test_snapshot_can_redact_secrets(device):
    snapshot = device.snapshot(redact=True)
    entries = snapshot["pages"]["EXAMPLE_SVC_"]["parameters"]
    assert entries["AuthPassword"]["value"] == "<redacted>"
    assert entries["CallerIDName"]["value"] == ""


def test_reboot_clears_caches(device):
    device.page("EXAMPLE_SVC_")
    device.reboot()
    assert device.client.reboots == 1
    assert device._pages == {}


def test_snapshot_omits_readonly_readouts_by_default(device):
    # RegistrationState is read-only; a diff must not report it as a change.
    entries = device.snapshot()["pages"]["EXAMPLE_SVC_"]["parameters"]
    assert "RegistrationState" not in entries
    assert "Enable" in entries
    assert "RegistrationState" in device.snapshot(writable_only=False)[
        "pages"
    ]["EXAMPLE_SVC_"]["parameters"]


def test_diff_skips_redacted_values_instead_of_crying_wolf(device):
    redacted = device.snapshot(redact=True)
    plain = device.snapshot()
    assert diff_snapshots(redacted, plain) == []
    assert count_redacted(redacted) == 1
    assert count_redacted(plain) == 0


def test_setup_wizard_is_excluded_as_a_duplicate_view():
    client = FakeClient()
    client.menu = client.menu.replace("EXAMPLE_SVC_.xml", "SetupWizard.xml", 1)
    client.pages["SetupWizard"] = client.pages["EXAMPLE_SVC_"]
    device = Device(client, Guard(enabled=False))
    assert "SetupWizard" in device.page_names()
    assert "SetupWizard" not in device.config_pages()
