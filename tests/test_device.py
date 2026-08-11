from __future__ import annotations

import pytest
from conftest import FakeClient, fixture, make_device

from obicfg.device import Device, count_redacted, diff_snapshots
from obicfg.errors import (
    GuardError,
    ResolutionError,
    TransportError,
    VerificationError,
)
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
    with pytest.raises(ResolutionError, match="no page named"):
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


class TestDiscoveryEdges:
    def test_an_empty_menu_is_reported_as_a_wrong_target(self):
        client = FakeClient()
        client.menu = "<html>a login page, not a menu</html>"
        device = Device(client, Guard(enabled=False))
        with pytest.raises(Exception, match="not an OBi admin interface"):
            device.page_names()

    def test_the_label_of_an_unlisted_page_is_its_name(self, device):
        assert device.label("DM_S_") == "DM_S_"

    def test_a_page_absent_from_the_menu_is_still_fetched(self):
        # DM_S_ and callhistory.xml are served on request but appear in no
        # menu entry, so "not listed" must not mean "does not exist".
        client = FakeClient()
        client.pages["DM_S_"] = fixture("EXAMPLE_SVC_.xml")
        device = Device(client, Guard(enabled=False))
        assert "DM_S_" not in device.page_names()
        assert device.page("DM_S_").get("Enable") is not None

    def test_identity_needs_a_page_that_reports_the_model(self):
        client = FakeClient(pages={"X": fixture("EXAMPLE_SVC_.xml")})
        client.menu = (
            """<a href="#" onclick="e('X.xml')" class="cmenu">X</a>"""
        )
        device = Device(client, Guard(enabled=False))
        with pytest.raises(ResolutionError, match="no page on this device reports"):
            device.identity()

    def test_the_query_transport_rejects_a_value_at_planning_time(self):
        from obicfg.client import Transport
        from obicfg.errors import ValidationError

        device = make_device()
        device.client.transport = Transport.QUERY
        # Caught in plan(), before anything is sent -- a dry run must fail too.
        with pytest.raises(ValidationError, match="space"):
            device.plan([("sp2.CallerIDName", "two words")])

    def test_wait_for_a_reboot_that_never_completes(self, monkeypatch):
        device = make_device()
        monkeypatch.setattr(__import__("time"), "sleep", lambda _s: None)
        original_fetch = device.client.fetch

        def always_down(path):
            if path == "DI_S_.xml":
                raise TransportError("still rebooting")
            return original_fetch(path)

        device.client.fetch = always_down
        assert device.reboot(wait=True, timeout=0.2) is False

    def test_wait_for_a_reboot_that_completes(self, monkeypatch):
        device = make_device()
        monkeypatch.setattr(__import__("time"), "sleep", lambda _s: None)
        assert device.reboot(wait=True) is True

    def test_a_show_prefixed_name_is_not_treated_as_a_secret(self):
        from obicfg.device import _is_secret
        from obicfg.model import Parameter

        def param(name):
            return Parameter(
                page="P", obj="O", name=name, hash="0", widget="input",
                default="", current="x",
            )

        assert _is_secret(param("AuthPassword")) is True
        # A checkbox called ShowAccessPointPassword is not a password.
        assert _is_secret(param("ShowAccessPointPassword")) is False


class TestDuplicateNames:
    """A page may repeat a parameter name across <object> blocks."""

    def _device(self):
        client = FakeClient(pages={"EXAMPLE_SYS_": fixture("EXAMPLE_SYS_.xml")})
        client.menu = (
            """<a href="#" onclick="e('EXAMPLE_SYS_.xml')" class="cmenu">Sys</a>"""
        )
        return Device(client, Guard(enabled=False))

    def test_a_snapshot_qualifies_every_member_of_a_duplicated_set(self):
        # Keying on the bare name kept only the last, so a backup silently
        # lost rows. Qualifying only the later ones would leave the first
        # holding a bare key that means something different from its siblings.
        entries = self._device().snapshot(
            include_status=True, writable_only=False
        )["pages"]["EXAMPLE_SYS_"]["parameters"]
        assert entries["WAN Status.MACAddress"]["value"] == "000000000001"
        assert entries["Product Information.MACAddress"]["value"] == "000000000002"
        assert "MACAddress" not in entries
        # Names that are not duplicated stay unqualified.
        assert "ModelName" in entries

    def test_an_ambiguous_name_is_refused_rather_than_guessed(self):
        # A codec profile has five BitRates, one per codec, and only one is
        # writable. Answering with the first match points a write at the wrong
        # codec and says nothing about it.
        device = self._device()
        with pytest.raises(ResolutionError, match="ambiguous") as excinfo:
            device.parameter("EXAMPLE_SYS_.MACAddress")
        message = str(excinfo.value)
        assert "WAN Status.MACAddress" in message
        assert "Product Information.MACAddress" in message
        assert "read-only" in message

    def test_the_qualified_form_resolves_exactly(self):
        device = self._device()
        assert (
            device.parameter("EXAMPLE_SYS_.Product Information.MACAddress").hash
            == "bbbb0003"
        )
        assert (
            device.parameter("EXAMPLE_SYS_.WAN Status.MACAddress").hash == "bbbb0001"
        )

    def test_a_genuinely_missing_page_still_reports_itself(self):
        device = self._device()
        with pytest.raises(ResolutionError, match="no page named"):
            device.parameter("NOPE_.Thing.Sub")


class TestHashAddressing:
    """`<page>#<hash>` is the only form that always resolves.

    The RTP statistics page carries four objects all named "Reset
    Statistics", so Object.Name cannot separate them either.
    """

    def _device(self):
        client = FakeClient(pages={"EXAMPLE_SYS_": fixture("EXAMPLE_SYS_.xml")})
        client.menu = (
            """<a href="#" onclick="e('EXAMPLE_SYS_.xml')" class="cmenu">Sys</a>"""
        )
        return Device(client, Guard(enabled=False))

    def test_a_hash_resolves_exactly(self):
        device = self._device()
        assert device.parameter("EXAMPLE_SYS_#bbbb0001").obj == "WAN Status"
        assert device.parameter("EXAMPLE_SYS_#bbbb0003").obj == "Product Information"

    def test_whitespace_around_the_hash_is_tolerated(self):
        assert self._device().parameter("EXAMPLE_SYS_# bbbb0003 ").hash == "bbbb0003"

    def test_an_unknown_hash_says_how_to_list_them(self):
        with pytest.raises(ResolutionError, match="show EXAMPLE_SYS_ --json"):
            self._device().parameter("EXAMPLE_SYS_#nosuchhash")

    def test_the_ambiguity_message_offers_the_hash_form(self):
        with pytest.raises(ResolutionError) as excinfo:
            self._device().parameter("EXAMPLE_SYS_.MACAddress")
        message = str(excinfo.value)
        assert "EXAMPLE_SYS_#bbbb0001" in message
        assert "EXAMPLE_SYS_#bbbb0003" in message
        assert "#hash form always works" in message


def test_the_guard_still_fires_on_a_hash_addressed_write():
    # The hash form bypasses split_path, so the early static check has to
    # understand it too -- otherwise #hash is a way around the protection.
    client = FakeClient(pages={"SetupWizard": fixture("EXAMPLE_SVC_.xml")})
    client.menu = (
        """<a href="#" onclick="e('SetupWizard.xml')" class="cmenu">Wizard</a>"""
    )
    device = Device(client, Guard())
    with pytest.raises(GuardError, match="Setup Wizard"):
        device.plan([("SetupWizard#aaaa0007", "x")])


def test_a_hash_addressed_write_on_an_unprotected_page_proceeds():
    # The other half of the guard-and-hash interaction: the early check must
    # let an ordinary page through rather than refusing every #hash path.
    device = make_device()
    device.guard = Guard()
    parameter = device.parameter("VS_1_VP_1_L_2_#a0020007")
    changes = device.plan([(f"VS_1_VP_1_L_2_#{parameter.hash}", "Lobby")])
    assert changes[0].parameter.name == "CallerIDName"
    assert changes[0].new == "Lobby"
