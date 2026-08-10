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


class TestTomlEdgeCases:
    """The fallback parser's error paths.

    It is deliberately strict: anything it cannot parse raises rather than
    guessing, so a file that loads here loads identically under tomllib.
    """

    def test_loads_wraps_the_stdlib_parser_error(self):
        with pytest.raises(_toml.TomlError):
            _toml.loads("= no key")

    def test_empty_value(self):
        # Reachable through an array with a hole in it; the key = value form
        # cannot produce one, because the regex requires something after "=".
        with pytest.raises(_toml.TomlError, match="missing value"):
            _toml._fallback_loads('a = ["x", , "y"]')
        with pytest.raises(_toml.TomlError, match="cannot parse"):
            _toml._fallback_loads("a = ")

    def test_unterminated_string(self):
        with pytest.raises(_toml.TomlError, match="unterminated"):
            _toml._fallback_loads('a = "no end')

    def test_trailing_backslash(self):
        with pytest.raises(_toml.TomlError, match="trailing backslash"):
            _toml._fallback_loads('a = "ends with \\"')

    def test_unknown_escape(self):
        with pytest.raises(_toml.TomlError, match="unknown escape"):
            _toml._fallback_loads(r'a = "\q"')

    def test_unicode_escapes(self):
        assert _toml._fallback_loads(r'a = "A\U0001F600"')["a"] == "A\U0001F600"

    def test_truncated_unicode_escape(self):
        with pytest.raises(_toml.TomlError, match="truncated"):
            _toml._fallback_loads(r'a = "\u00"')

    def test_a_value_that_is_neither_number_nor_string(self):
        with pytest.raises(_toml.TomlError, match="cannot parse value"):
            _toml._fallback_loads("a = notaliteral")

    def test_floats(self):
        assert _toml._fallback_loads("a = 1.5")["a"] == 1.5

    def test_arrays(self):
        assert _toml._fallback_loads("a = []")["a"] == []
        assert _toml._fallback_loads('a = ["x", "y"]')["a"] == ["x", "y"]
        assert _toml._fallback_loads('a = ["has, comma"]')["a"] == ["has, comma"]

    def test_multiline_arrays_are_refused_with_a_way_out(self):
        with pytest.raises(_toml.TomlError, match="multi-line arrays"):
            _toml._fallback_loads('a = [\n"x",\n]')

    def test_unterminated_string_inside_an_array(self):
        with pytest.raises(_toml.TomlError, match="unterminated string in array"):
            _toml._fallback_loads('a = ["x]')

    def test_a_key_cannot_be_both_a_value_and_a_table(self):
        with pytest.raises(_toml.TomlError, match="both a value and a table"):
            _toml._fallback_loads("a = 1\na.b = 2")

    def test_quoted_table_and_key_names(self):
        data = _toml._fallback_loads('["a b"]\n"c d" = 1\n')
        assert data["a b"]["c d"] == 1

    def test_comments_and_blank_lines_are_skipped(self):
        assert _toml._fallback_loads("# just a comment\n\n  \n") == {}

    def test_flatten_of_a_flat_table_is_unchanged(self):
        assert _toml.flatten({"a": 1}) == {"a": 1}


class TestProfileEdgeCases:
    def _write(self, tmp_path, text):
        path = tmp_path / "p.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_malformed_profile_names_the_file(self, tmp_path):
        with pytest.raises(ProfileError, match="p.toml"):
            load(self._write(tmp_path, "not toml at all"))

    def test_a_setting_cannot_be_a_list(self, tmp_path):
        path = self._write(tmp_path, '[settings]\n"a.b" = ["x"]\n')
        with pytest.raises(ProfileError, match="string, number or boolean"):
            load(path)

    def test_reset_must_be_a_list_of_strings(self, tmp_path):
        path = self._write(tmp_path, "[reset]\nparameters = 5\n")
        with pytest.raises(ProfileError, match="list of strings"):
            load(path)

    def test_reset_that_is_not_a_table_is_ignored(self, tmp_path):
        assert load(self._write(tmp_path, 'reset = "nope"\n')).reset == []

    def test_after_that_is_not_a_table_means_no_reboot(self, tmp_path):
        assert load(self._write(tmp_path, 'after = "nope"\n')).reboot is False

    def test_the_name_defaults_to_the_filename(self, tmp_path):
        assert load(self._write(tmp_path, "[settings]\n")).name == "p"

    def test_a_requirement_the_device_cannot_report(self, tmp_path):
        profile = load(self._write(tmp_path, '[require]\nserial = "X"\n'))
        with pytest.raises(ProfileError, match="does not report SerialNumber"):
            check_requirements(profile, {})


def test_loads_uses_the_bundled_parser_when_tomllib_is_absent(monkeypatch):
    # The path taken on Python older than 3.11 without `tomli` installed.
    monkeypatch.setattr(_toml, "_tomllib", None)
    assert _toml.loads('[settings]\n"a.b" = 1\n') == {"settings": {"a.b": 1}}
