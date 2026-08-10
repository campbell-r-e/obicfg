from __future__ import annotations

import pytest

from obicfg.errors import GuardError
from obicfg.guard import MACRO, DEFAULT_RULES, Guard, Rule
from obicfg.model import Parameter


def param(
    name: str,
    value: str,
    page: str = "VS_1_VP_1_L_1_",
    *,
    default: str = "",
) -> Parameter:
    """A parameter holding `value`. It counts as changed unless value == default."""
    return Parameter(
        page=page,
        obj="Service",
        name=name,
        hash="0",
        widget="input",
        default=default,
        current=None if value == default else value,
    )


def test_macro_pattern_recognises_provisioned_values():
    assert MACRO.search("${DSN}_1")
    assert MACRO.search("sip:$MAC@example.net")
    assert not MACRO.search("1234567890")
    assert not MACRO.search("$1")  # a dialplan back-reference, not a macro


def test_static_rules_block_auto_provisioning_and_the_wizard():
    guard = Guard()
    with pytest.raises(GuardError, match="rewrite the entire configuration"):
        guard.check("DM_S_.ConfigURL")
    with pytest.raises(GuardError, match="Setup Wizard"):
        guard.check("SetupWizard.Step1")


def test_a_macro_valued_parameter_is_protected():
    guard = Guard()
    with pytest.raises(GuardError, match="provisioning macro"):
        guard.check("x.AuthUserName", param("AuthUserName", "${DSN}_1"))


def test_a_macro_that_is_the_factory_default_is_not_provisioning():
    # Every ITSP profile ships X_UserAgentName = "OBIHAI/${DM}-${FWV}". That is
    # firmware templating on a factory-fresh unit, not a provisioned secret --
    # treating it as one would lock the tool out of every SIP page.
    guard = Guard()
    firmware_macro = param(
        "X_UserAgentName",
        "OBIHAI/${DM}-${FWV}",
        default="OBIHAI/${DM}-${FWV}",
    )
    guard.check("x.X_UserAgentName", firmware_macro)
    guard.check("x.ProxyServer", param("ProxyServer", "192.0.2.10"), [firmware_macro])


def test_credentials_beside_a_macro_are_protected_by_association():
    guard = Guard()
    siblings = [param("AuthUserName", "${DSN}_1"), param("AuthPassword", "secret")]
    with pytest.raises(GuardError, match="AuthUserName"):
        guard.check("x.AuthPassword", param("AuthPassword", "secret"), siblings)


def test_non_credential_fields_beside_a_macro_stay_editable():
    guard = Guard()
    siblings = [param("AuthUserName", "${DSN}_1")]
    guard.check("x.X_InboundCallRoute", param("X_InboundCallRoute", "ph"), siblings)


def test_a_hand_configured_service_is_not_protected():
    guard = Guard()
    siblings = [param("AuthUserName", "1001"), param("AuthPassword", "typed-by-hand")]
    guard.check("x.AuthPassword", param("AuthPassword", "typed-by-hand"), siblings)


def test_unsafe_disables_everything():
    guard = Guard(enabled=False)
    guard.check("DM_S_.ConfigURL")
    guard.check("x.AuthUserName", param("AuthUserName", "${DSN}_1"))


def test_detection_can_be_turned_off_without_dropping_static_rules():
    guard = Guard(detect_provisioned=False)
    guard.check("x.AuthUserName", param("AuthUserName", "${DSN}_1"))
    with pytest.raises(GuardError):
        guard.check("DM_S_.ConfigURL")


class TestFromConfig:
    def test_extra_patterns_are_added(self):
        guard = Guard.from_config({"extra": ["VS_1_X_PBX_.*"]})
        with pytest.raises(GuardError, match="local configuration"):
            guard.check("VS_1_X_PBX_.Enable")

    def test_allow_drops_a_builtin_rule(self):
        guard = Guard.from_config({"allow": ["DM_S_.*"]})
        guard.check("DM_S_.ConfigURL")
        assert len(guard.rules) == len(DEFAULT_RULES) - 1

    def test_patterns_are_case_insensitive(self):
        assert Rule("dm_s_.*", "x").matches("DM_S_.ConfigURL")
