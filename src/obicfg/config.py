"""Where connection settings come from, and in what order.

Precedence, highest first: command-line flags, environment variables, the
config file, then built-in defaults.  The config file lives at
``$XDG_CONFIG_HOME/obicfg/config.toml`` (``~/.config/obicfg/config.toml`` when
XDG_CONFIG_HOME is unset), which is the right place on Linux, the BSDs and
macOS alike.

    [device]
    host = "192.0.2.50"
    username = "admin"
    password = "hunter2"
    transport = "paramlist"

    [guard]
    extra = ["VS_1_X_PBX_.*"]   # protect more
    allow = ["DM_S_.*"]         # drop a built-in rule
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from . import _toml
from .errors import ObiError

ENV_PREFIX = "OBI_"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "obicfg"


def config_path() -> Path:
    override = os.environ.get("OBICFG_CONFIG")
    return Path(override) if override else config_dir() / "config.toml"


def load_config(path: Path | None = None) -> dict:
    """Load the config file, or return an empty dict if there is not one."""
    path = path or config_path()
    if not path.exists():
        return {}
    try:
        data = _toml.loads(path.read_text(encoding="utf-8"))
    except (_toml.TomlError, OSError) as exc:
        raise ObiError(f"{path}: {exc}") from None
    _warn_if_exposed(path, data)
    return data


def _warn_if_exposed(path: Path, data: dict) -> None:
    """Nag if a file holding a password is readable by anyone else."""
    if not data.get("device", {}).get("password"):
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        print(
            f"obicfg: warning: {path} contains a password and is readable by "
            f"others; consider `chmod 600 {path}`",
            file=sys.stderr,
        )


def env(name: str) -> str | None:
    return os.environ.get(ENV_PREFIX + name.upper())


def resolve(
    key: str,
    cli_value: object = None,
    config: dict | None = None,
    default: object = None,
    *,
    section: str = "device",
) -> object:
    """Pick a setting from CLI, environment, config file, then default."""
    if cli_value is not None:
        return cli_value
    from_env = env(key)
    if from_env is not None:
        return from_env
    if config:
        value = config.get(section, {}).get(key)
        if value is not None:
            return value
    return default


def read_password(
    cli_password: str | None,
    password_file: str | None,
    config: dict,
) -> str:
    """Resolve the admin password without needing it on the command line.

    ``--password-file`` and ``OBI_PASSWORD_FILE`` exist because a password in
    argv is visible in ``ps`` output to every user on the box.
    """
    if cli_password:
        return cli_password
    path = password_file or env("password_file")
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ObiError(f"cannot read password file {path}: {exc}") from None
    from_env = env("password")
    if from_env is not None:
        return from_env
    return str(config.get("device", {}).get("password", "admin"))
