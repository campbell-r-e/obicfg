from __future__ import annotations

import pytest

from obicfg.naming import ALIASES, aliases_for, resolve_page, split_path

KNOWN = ["VS_1_VP_1_L_2_", "VS_1_VP_2_SIP_", "VS_1_X_FXS_1_", "DI_S_"]


def test_alias_expands_to_the_page_name():
    assert split_path("sp2.X_InboundCallRoute") == (
        "VS_1_VP_1_L_2_",
        "X_InboundCallRoute",
    )


def test_longer_aliases_win():
    # "itsp.b.sip" must beat the "itsp.b" prefix.
    assert split_path("itsp.b.sip.ProxyServer") == ("VS_1_VP_2_SIP_", "ProxyServer")
    assert split_path("itsp.b.X_AccessList") == ("VS_1_VP_2_", "X_AccessList")


def test_raw_page_names_pass_through():
    assert split_path("VS_1_VP_1_L_2_.X_InboundCallRoute") == (
        "VS_1_VP_1_L_2_",
        "X_InboundCallRoute",
    )


def test_aliases_are_case_insensitive():
    assert split_path("SP2.Enable")[0] == "VS_1_VP_1_L_2_"


def test_a_path_without_a_parameter_is_rejected():
    # A UsageError, so the CLI exits 2 and prints a message rather than
    # letting a bare ValueError escape as a traceback.
    from obicfg.errors import UsageError
    from obicfg.naming import PathError

    with pytest.raises(PathError, match="page.*parameter"):
        split_path("sp2")
    assert issubclass(PathError, UsageError)


def test_resolve_page_handles_alias_exact_and_sloppy_names():
    assert resolve_page("sp2", KNOWN) == "VS_1_VP_1_L_2_"
    assert resolve_page("vs_1_vp_2_sip_", KNOWN) == "VS_1_VP_2_SIP_"
    # The trailing underscore is easy to forget and carries no meaning.
    assert resolve_page("VS_1_VP_2_SIP", KNOWN) == "VS_1_VP_2_SIP_"
    assert resolve_page("nonsense", KNOWN) is None


def test_an_alias_resolves_even_when_the_menu_does_not_list_the_page():
    # The menu is not a complete index -- DM_S_ and callhistory.xml are served
    # without appearing in it. Requiring a menu entry made `show provisioning`
    # fail while `get provisioning.ConfigURL` worked. A page that genuinely
    # does not exist fails at the fetch, with the device saying so.
    assert resolve_page("bt1", KNOWN) == "VS_1_X_BT_1_"
    assert resolve_page("provisioning", KNOWN) == "DM_S_"


def test_aliases_for_reports_every_name_pointing_at_a_page():
    assert aliases_for("VS_1_X_FXS_1_") == ["fxs", "phone"]


def test_the_sp_numbering_trap_is_encoded_correctly():
    # SP2 lives at VP_1_L_2_, not VP_2_. The VP index is the ITSP profile.
    assert ALIASES["sp2"] == "VS_1_VP_1_L_2_"
    assert ALIASES["itsp.b"] == "VS_1_VP_2_"
