"""Human-friendly names for OBi200 pages.

The device's own page names are accurate and unreadable: ``VS_1_VP_1_L_2_``
is the SP2 Service page and ``VS_1_VP_2_SIP_`` is ITSP Profile B's SIP
section.  Worse, the numbering is a trap -- the *n* in ``VP_n`` is the ITSP
profile (A-D) while the *n* in ``L_n`` is the service (SP1-SP4), so
``VS_1_VP_1_L_2_`` is SP2, not "profile 1, line 2" in any sense you would
guess.

Aliases here are a convenience layer only.  Raw page names always work, and
``obicfg search`` finds a parameter without knowing either name, so nothing
in the tool depends on this table being complete.
"""

from __future__ import annotations

#: alias -> page name (no .xml).  Keys are matched case-insensitively.
ALIASES: dict[str, str] = {
    # Voice services -- the trunks/lines you actually dial through.
    "sp1": "VS_1_VP_1_L_1_",
    "sp2": "VS_1_VP_1_L_2_",
    "sp3": "VS_1_VP_1_L_3_",
    "sp4": "VS_1_VP_1_L_4_",
    "sp1.stats": "VS_1_VP_1_L_1_Stats",
    # ITSP profiles -- the SIP/RTP/tone settings a service points at.
    "itsp.a": "VS_1_VP_1_",
    "itsp.b": "VS_1_VP_2_",
    "itsp.c": "VS_1_VP_3_",
    "itsp.d": "VS_1_VP_4_",
    "itsp.a.sip": "VS_1_VP_1_SIP_",
    "itsp.b.sip": "VS_1_VP_2_SIP_",
    "itsp.c.sip": "VS_1_VP_3_SIP_",
    "itsp.d.sip": "VS_1_VP_4_SIP_",
    "itsp.a.rtp": "VS_1_VP_1_RTP_",
    "itsp.b.rtp": "VS_1_VP_2_RTP_",
    "itsp.c.rtp": "VS_1_VP_3_RTP_",
    "itsp.d.rtp": "VS_1_VP_4_RTP_",
    "tone.a": "VS_1_VP_1_T_",
    "tone.b": "VS_1_VP_2_T_",
    "ring.a": "VS_1_VP_1_L_1_R_",
    "ring.b": "VS_1_VP_1_L_2_R_",
    "codec.a": "VS_1_CODEC_1_",
    "codec.b": "VS_1_CODEC_2_",
    # Ports and features.
    "phone": "VS_1_X_FXS_1_",
    "fxs": "VS_1_X_FXS_1_",
    "phone.stats": "PI_FXS_1_Stats",
    "gateways": "VS_1_X_GW_",
    "gw": "VS_1_X_GW_",
    "aa": "VS_1_X_AA_1_",
    "autoattendant": "VS_1_X_AA_1_",
    "obitalk": "VS_1_X_P2P_1_",
    "p2p": "VS_1_X_P2P_1_",
    "pbx": "VS_1_X_PBX_",
    "obiplus": "VS_1_X_PBX_",
    "star.a": "VS_1_X_STAR_1_",
    "star.b": "VS_1_X_STAR_2_",
    "bt1": "VS_1_X_BT_1_",
    "bt2": "VS_1_X_BT_2_",
    "pagegroups": "VS_1_X_PageGroups",
    "speeddial": "SPEEDDIAL_",
    "digitmaps": "DIGITMAPS_",
    # System.
    "wan": "DI_NS_",
    "status": "DI_S_",
    "system": "DI_S_",
    "admin": "DM_MISC_",
    "provisioning": "DM_S_",
    "wifi": "USB_WIFI_",
    "wizard": "SetupWizard",
}

#: Longest alias first, so "itsp.b.sip" wins over "itsp.b".
_SORTED = sorted(ALIASES, key=len, reverse=True)


def split_path(path: str) -> tuple[str, str]:
    """Split ``sp2.X_InboundCallRoute`` into ``("VS_1_VP_1_L_2_", "X_...")``.

    Unrecognised prefixes are passed through untouched, so raw page names and
    pages this table has never heard of both keep working.
    """
    lowered = path.lower()
    for alias in _SORTED:
        if lowered.startswith(alias + "."):
            return ALIASES[alias], path[len(alias) + 1 :]
    page, _, name = path.rpartition(".")
    if not page:
        raise ValueError(
            f"{path!r} is not a parameter path -- expected <page>.<parameter>, "
            "e.g. sp2.X_InboundCallRoute or VS_1_VP_2_SIP_.ProxyServer"
        )
    return page, name


def resolve_page(name: str, known: list[str]) -> str | None:
    """Resolve a page alias or (possibly sloppy) page name against ``known``."""
    if name.lower() in ALIASES:
        candidate = ALIASES[name.lower()]
        if candidate in known:
            return candidate
    for page in known:
        if page.lower() == name.lower():
            return page
    # The trailing underscore on most page names is easy to forget.
    for page in known:
        if page.lower().rstrip("_") == name.lower().rstrip("_"):
            return page
    return None


def aliases_for(page: str) -> list[str]:
    """Every alias pointing at ``page``, for display."""
    return sorted(a for a, target in ALIASES.items() if target == page)
