"""Where connection settings come from, and the order they win in."""

from __future__ import annotations

import stat

import pytest

from obicfg import config as config_mod
from obicfg.errors import ObiError


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "OBI_HOST", "OBI_USERNAME", "OBI_PASSWORD", "OBI_PASSWORD_FILE",
        "OBI_TRANSPORT", "OBI_TIMEOUT", "OBICFG_CONFIG", "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)


class TestPaths:
    def test_xdg_config_home_is_honoured(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert config_mod.config_dir() == tmp_path / "obicfg"

    def test_default_is_dot_config_under_home(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config_mod.Path, "home", classmethod(lambda cls: tmp_path))
        assert config_mod.config_dir() == tmp_path / ".config" / "obicfg"

    def test_an_explicit_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OBICFG_CONFIG", str(tmp_path / "custom.toml"))
        assert config_mod.config_path() == tmp_path / "custom.toml"


class TestLoad:
    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert config_mod.load_config(tmp_path / "absent.toml") == {}

    def test_a_valid_file_is_parsed(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[device]\nhost = "192.0.2.9"\n')
        assert config_mod.load_config(path)["device"]["host"] == "192.0.2.9"

    def test_a_broken_file_names_itself(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("this is not toml")
        with pytest.raises(ObiError, match="config.toml"):
            config_mod.load_config(path)

    def test_a_world_readable_password_file_warns(self, tmp_path, capsys):
        path = tmp_path / "config.toml"
        path.write_text('[device]\npassword = "hunter2"\n')
        path.chmod(0o644)
        config_mod.load_config(path)
        assert "readable by others" in capsys.readouterr().err

    def test_tight_permissions_do_not_warn(self, tmp_path, capsys):
        path = tmp_path / "config.toml"
        path.write_text('[device]\npassword = "hunter2"\n')
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        config_mod.load_config(path)
        assert capsys.readouterr().err == ""

    def test_no_password_means_no_warning_regardless_of_mode(self, tmp_path, capsys):
        path = tmp_path / "config.toml"
        path.write_text('[device]\nhost = "192.0.2.9"\n')
        path.chmod(0o644)
        config_mod.load_config(path)
        assert capsys.readouterr().err == ""

    def test_an_unstattable_file_does_not_crash(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "config.toml"
        path.write_text('[device]\npassword = "x"\n')

        real_stat = config_mod.Path.stat
        seen = {"n": 0}

        def flaky(self, *args, **kwargs):
            # Path.exists() stats too, so let the first call through and fail
            # the permission check specifically.
            if self == path:
                seen["n"] += 1
                if seen["n"] > 1:
                    raise OSError("gone")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(config_mod.Path, "stat", flaky)
        config_mod.load_config(path)
        assert capsys.readouterr().err == ""


class TestResolve:
    def test_precedence_is_cli_then_env_then_config_then_default(self, monkeypatch):
        config = {"device": {"host": "from-config"}}
        assert config_mod.resolve("host", "from-cli", config) == "from-cli"

        monkeypatch.setenv("OBI_HOST", "from-env")
        assert config_mod.resolve("host", None, config) == "from-env"

        monkeypatch.delenv("OBI_HOST")
        assert config_mod.resolve("host", None, config) == "from-config"
        assert config_mod.resolve("host", None, {}, "fallback") == "fallback"

    def test_a_key_absent_from_the_config_section(self):
        assert config_mod.resolve("host", None, {"device": {}}, "d") == "d"

    def test_env_lookup_helper(self, monkeypatch):
        assert config_mod.env("host") is None
        monkeypatch.setenv("OBI_HOST", "x")
        assert config_mod.env("host") == "x"


class TestPassword:
    def test_the_flag_wins(self):
        assert config_mod.read_password("flag", None, {}) == "flag"

    def test_a_password_file_is_read_and_stripped(self, tmp_path):
        path = tmp_path / "pw"
        path.write_text("  s3cret\n")
        assert config_mod.read_password(None, str(path), {}) == "s3cret"

    def test_the_password_file_can_come_from_the_environment(self, tmp_path, monkeypatch):
        path = tmp_path / "pw"
        path.write_text("from-env-file")
        monkeypatch.setenv("OBI_PASSWORD_FILE", str(path))
        assert config_mod.read_password(None, None, {}) == "from-env-file"

    def test_an_unreadable_password_file_is_an_error(self, tmp_path):
        with pytest.raises(ObiError, match="cannot read password file"):
            config_mod.read_password(None, str(tmp_path / "absent"), {})

    def test_the_environment_variable(self, monkeypatch):
        monkeypatch.setenv("OBI_PASSWORD", "from-env")
        assert config_mod.read_password(None, None, {}) == "from-env"

    def test_the_config_file(self):
        assert config_mod.read_password(None, None, {"device": {"password": "cfg"}}) == "cfg"

    def test_the_factory_default_is_the_last_resort(self):
        assert config_mod.read_password(None, None, {}) == "admin"
