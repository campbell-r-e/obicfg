from __future__ import annotations

import pytest

from obicfg import _toml
from obicfg.profile import ProfileError, check_requirements, load

SAMPLE = """
# a comment
name = "Example"
description = 'literal \\n stays literal'

[require]
model = "OBi200"

[settings]
"sp2.Enable" = true
"sp2.ProxyServerPort" = 5061
"sp2.X_InboundCallRoute" = "{(<**1:>(Msp1)):sp1}"
"phone.CallerIDName" = "Front desk #2"

[reset]
parameters = ["sp4.CallerIDName", "sp4.Enable"]

[after]
reboot = true
"""


class TestToml:
    def test_fallback_matches_the_stdlib_parser(self):
        # Whichever parser the runtime uses, the fallback must agree with it.
        assert _toml._fallback_loads(SAMPLE) == _toml.loads(SAMPLE)

    def test_values_and_types(self):
        data = _toml._fallback_loads(SAMPLE)
        assert data["settings"]["sp2.Enable"] is True
        assert data["settings"]["sp2.ProxyServerPort"] == 5061
        assert data["settings"]["sp2.X_InboundCallRoute"] == "{(<**1:>(Msp1)):sp1}"
        assert data["reset"]["parameters"] == ["sp4.CallerIDName", "sp4.Enable"]

    def test_hash_inside_a_string_is_not_a_comment(self):
        data = _toml._fallback_loads('[settings]\n"a.b" = "x #1"  # real comment\n')
        assert data["settings"]["a.b"] == "x #1"

    def test_literal_strings_do_not_process_escapes(self):
        assert _toml._fallback_loads(r"a = 'c:\n'")["a"] == "c:\\n"

    def test_basic_strings_do_process_escapes(self):
        assert _toml._fallback_loads(r'a = "line\nbreak"')["a"] == "line\nbreak"

    def test_bare_dotted_keys_nest_then_flatten_the_same(self):
        nested = _toml._fallback_loads("[settings]\nsp2.Enable = true\n")
        quoted = _toml._fallback_loads('[settings]\n"sp2.Enable" = true\n')
        assert _toml.flatten(nested["settings"]) == _toml.flatten(quoted["settings"])

    def test_garbage_raises_rather_than_guessing(self):
        with pytest.raises(_toml.TomlError):
            _toml._fallback_loads("this is not toml")


class TestProfile:
    def _write(self, tmp_path, text, name="p.toml"):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_load(self, tmp_path):
        profile = load(self._write(tmp_path, SAMPLE))
        assert profile.name == "Example"
        assert profile.reboot is True
        assert profile.require == {"model": "OBi200"}
        assert dict(profile.assignments)["sp2.Enable"] is True
        assert profile.reset == ["sp4.CallerIDName", "sp4.Enable"]

    def test_settings_must_be_parameter_paths(self, tmp_path):
        path = self._write(tmp_path, '[settings]\nEnable = true\n')
        with pytest.raises(ProfileError, match="not a parameter path"):
            load(path)

    def test_unknown_top_level_keys_are_rejected(self, tmp_path):
        path = self._write(tmp_path, 'setings = 1\n')
        with pytest.raises(ProfileError, match="unknown top-level"):
            load(path)

    def test_missing_file(self, tmp_path):
        with pytest.raises(ProfileError, match="cannot read"):
            load(tmp_path / "absent.toml")

    def test_requirements_match_as_prefixes(self, tmp_path):
        profile = load(self._write(tmp_path, SAMPLE))
        check_requirements(profile, {"ModelName": "OBi200"})
        profile.require = {"firmware": "3.2"}
        check_requirements(profile, {"SoftwareVersion": "3.2.2 (Build: 8680EX)"})

    def test_requirements_stop_a_profile_hitting_the_wrong_box(self, tmp_path):
        profile = load(self._write(tmp_path, SAMPLE))
        with pytest.raises(ProfileError, match="device reports 'OBi110'"):
            check_requirements(profile, {"ModelName": "OBi110"})
