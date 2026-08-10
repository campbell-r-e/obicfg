"""Declarative configuration profiles.

A profile is a TOML file describing the state a device should be in, not the
steps to get there.  Applying one is idempotent: settings already at the
requested value are skipped, so re-running after a partial failure -- or after
someone poked the web UI -- converges instead of thrashing.

    # asterisk-trunks.toml
    name = "Asterisk SIP trunks"

    [require]
    model = "OBi200"          # refuse to run against anything else

    [settings]
    "sp2.Enable"                 = true
    "sp2.X_InboundCallRoute"     = "sp1"
    "sp2.X_UserAgentPort"        = 5061

    [reset]
    parameters = ["sp4.CallerIDName"]

    [after]
    reboot = false
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import _toml
from .errors import ObiError


class ProfileError(ObiError):
    """A profile file was malformed or does not apply to this device."""


@dataclass
class Profile:
    name: str = "unnamed"
    description: str = ""
    require: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)
    reset: list = field(default_factory=list)
    reboot: bool = False
    source: Path | None = None

    @property
    def assignments(self) -> list:
        return list(self.settings.items())


def load(path: str | Path) -> Profile:
    """Read and validate a profile file."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileError(f"cannot read {path}: {exc}") from None
    try:
        data = _toml.loads(text)
    except _toml.TomlError as exc:
        raise ProfileError(f"{path}: {exc}") from None

    unknown = set(data) - {
        "name",
        "description",
        "require",
        "settings",
        "reset",
        "after",
    }
    if unknown:
        raise ProfileError(
            f"{path}: unknown top-level key(s): {', '.join(sorted(unknown))}"
        )

    settings = _toml.flatten(data.get("settings", {}))
    for key, value in settings.items():
        if isinstance(value, (list, dict)):
            raise ProfileError(
                f"{path}: setting {key!r} must be a string, number or boolean"
            )
        if "." not in key:
            raise ProfileError(
                f"{path}: setting {key!r} is not a parameter path -- expected "
                "<page>.<parameter>, e.g. \"sp2.X_InboundCallRoute\""
            )

    reset_table = data.get("reset", {})
    reset = reset_table.get("parameters", []) if isinstance(reset_table, dict) else []
    if not isinstance(reset, list) or any(not isinstance(p, str) for p in reset):
        raise ProfileError(f"{path}: [reset] parameters must be a list of strings")

    after = data.get("after", {})
    return Profile(
        name=str(data.get("name", path.stem)),
        description=str(data.get("description", "")),
        require=data.get("require", {}) or {},
        settings=settings,
        reset=reset,
        reboot=bool(after.get("reboot", False)) if isinstance(after, dict) else False,
        source=path,
    )


def check_requirements(profile: Profile, identity: dict) -> None:
    """Enforce the profile's ``[require]`` table against the live device.

    Firmware and model are matched as prefixes, so ``firmware = "3.2"``
    accepts ``3.2.2 (Build: 8680EX)``.  This exists so a profile written for
    an OBi200 cannot be applied to some other unit by a slip of the ``--host``
    flag.
    """
    checks = {
        "model": "ModelName",
        "firmware": "SoftwareVersion",
        "hardware": "HardwareVersion",
        "mac": "MACAddress",
        "serial": "SerialNumber",
    }
    for key, field_name in checks.items():
        expected = profile.require.get(key)
        if expected is None:
            continue
        actual = identity.get(field_name, "")
        if not actual:
            raise ProfileError(
                f"profile requires {key} = {expected!r} but the device does not "
                f"report {field_name}"
            )
        if not str(actual).lower().startswith(str(expected).lower()):
            raise ProfileError(
                f"profile requires {key} starting with {expected!r}, but this "
                f"device reports {actual!r}"
            )
