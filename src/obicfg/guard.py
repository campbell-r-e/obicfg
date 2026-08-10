"""Write protection for settings that cannot be put back.

Most of an OBi is freely editable: get it wrong, notice, set it again.  A few
things are not like that.

These devices were largely configured *by* the OBiTALK provisioning cloud,
which Polycom retired in November 2024.  Whatever that cloud pushed onto a
unit -- most famously a Google Voice binding, but equally an ITSP account a
provider set up for a customer -- now exists in exactly one place: the flash
on that one box.  There is no longer a server to hand it back.  Overwrite it
and it is gone for good, along with the phone number attached to it.

Rather than hardcoding "profile A is sacred" -- which is only true on some
units -- this module *detects* provisioned settings.  Provisioning macros
survive in the stored configuration verbatim, so a parameter still holding
``${DSN}_1`` or ``$MAC`` is, by direct evidence, a value the device did not
get from a human and cannot regenerate.  Any page carrying such a value has
its credential fields protected as a group, because the password sitting next
to a macro-valued username came from the same vanished source.

Everything here is advisory and switchable: ``--unsafe`` turns it off
entirely, and the config file can add or remove rules.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Iterable

from .errors import GuardError

OBITALK_GONE = (
    "the OBiTALK provisioning cloud shut down in Nov 2024, so anything it "
    "issued cannot be re-issued once overwritten"
)

#: A provisioning macro left behind in a stored value, e.g. ${DSN}_1, $MAC.
MACRO = re.compile(r"\$\{[A-Za-z0-9_]+\}|\$[A-Z][A-Z0-9_]{2,}")

#: Fields that together constitute "who this device logs in as". If one of
#: them on a page is provisioned, all of them on that page are suspect.
CREDENTIAL_FIELDS = frozenset(
    {
        "authusername",
        "authpassword",
        "uri",
        "x_servprovprofile",
        "proxyserver",
        "outboundproxy",
        "registrarserver",
        "userid",
        "password",
    }
)


@dataclass(frozen=True)
class Rule:
    pattern: str
    reason: str

    def matches(self, path: str) -> bool:
        return fnmatch.fnmatch(path.lower(), self.pattern.lower())


#: Static rules that hold on every unit, regardless of how it was set up.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        "DM_S_.*",
        "auto-provisioning can rewrite the entire configuration at the next "
        f"check-in, and {OBITALK_GONE}",
    ),
    Rule(
        "SetupWizard.*",
        "the Setup Wizard rewrites service configuration in bulk rather than "
        "one setting at a time",
    ),
)


class Guard:
    """Decides whether a given parameter may be written.

    Checks are cheap and local; nothing here talks to the device.  The caller
    supplies the parameter and, where it can, the other parameters on the same
    page, which is what makes provisioning detection possible.
    """

    def __init__(
        self,
        rules: tuple[Rule, ...] = DEFAULT_RULES,
        *,
        enabled: bool = True,
        detect_provisioned: bool = True,
    ) -> None:
        self.rules = rules
        self.enabled = enabled
        self.detect_provisioned = detect_provisioned

    @classmethod
    def from_config(cls, config: dict, *, enabled: bool = True) -> "Guard":
        """Build from a ``[guard]`` table.

            [guard]
            extra = ["VS_1_X_PBX_.*"]     # protect these too
            allow = ["DM_S_.*"]           # drop a built-in rule
            detect_provisioned = true     # default
        """
        rules = list(DEFAULT_RULES)
        for pattern in config.get("allow", []):
            rules = [r for r in rules if r.pattern.lower() != str(pattern).lower()]
        for pattern in config.get("extra", []):
            rules.append(Rule(str(pattern), "protected by local configuration"))
        return cls(
            tuple(rules),
            enabled=enabled,
            detect_provisioned=bool(config.get("detect_provisioned", True)),
        )

    # -- static rules -----------------------------------------------------

    def static_reason(self, path: str) -> str | None:
        for rule in self.rules:
            if rule.matches(path):
                return rule.reason
        return None

    # -- provisioning detection -------------------------------------------

    @staticmethod
    def _is_provisioned(parameter) -> bool:
        """Does this parameter hold a macro that a provisioning server put there?

        The "not at factory default" half is essential.  The firmware ships
        macros of its own -- every ITSP profile's ``X_UserAgentName`` defaults
        to ``OBIHAI/${DM}-${FWV}``, which is just templating for the SIP
        User-Agent header and is present on a factory-fresh unit.  Treating
        those as provisioning evidence would lock the tool out of every SIP
        page on every device.  A macro that is *not* the default was written
        by something, and the only thing that writes macros is a provisioning
        server.
        """
        return not parameter.is_default and bool(MACRO.search(parameter.value))

    def provisioned_reason(self, parameter, siblings: Iterable = ()) -> str | None:
        """Why this parameter looks provisioned, or None if it does not.

        ``parameter`` and ``siblings`` are :class:`~obicfg.model.Parameter`
        objects; siblings are the other parameters on the same page.
        """
        if not self.detect_provisioned:
            return None
        if self._is_provisioned(parameter):
            return (
                f"its current value {parameter.value!r} is a provisioning macro "
                f"and not the factory default, so it was pushed by a "
                f"provisioning server rather than typed; {OBITALK_GONE}"
            )
        if parameter.name.lower() not in CREDENTIAL_FIELDS:
            return None
        for sibling in siblings:
            # Only other *credentials* count. A macro somewhere unrelated on
            # the page says nothing about who owns the login.
            if sibling.name == parameter.name:
                continue
            if sibling.name.lower() not in CREDENTIAL_FIELDS:
                continue
            if self._is_provisioned(sibling):
                return (
                    f"{sibling.path} on this page holds the provisioning macro "
                    f"{sibling.value!r}, so this page's credentials came from a "
                    f"provisioning server; {OBITALK_GONE}"
                )
        return None

    # -- the check --------------------------------------------------------

    def check(self, path: str, parameter=None, siblings: Iterable = ()) -> None:
        """Raise :class:`GuardError` if this write is protected."""
        if not self.enabled:
            return
        reason = self.static_reason(path)
        if reason is None and parameter is not None:
            reason = self.provisioned_reason(parameter, siblings)
        if reason is not None:
            raise GuardError(
                f"refusing to write {path}: {reason}.\n"
                "  Back up first (obicfg dump ./before), and re-run with "
                "--unsafe if you are certain."
            )
