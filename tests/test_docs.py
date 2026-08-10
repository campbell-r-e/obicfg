"""The documentation is checked by the test suite, not by hope.

A command reference drifts the moment someone adds a flag and forgets the
docs. These tests make that a build failure instead of a discovery six months
later by someone following instructions that no longer describe the tool.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from obicfg.cli import EXIT_GUARD, EXIT_OK, EXIT_USAGE, EXIT_VERIFY, build_parser
from obicfg.naming import ALIASES
from obicfg.profile import load

REPO = Path(__file__).resolve().parents[1]
COMMANDS = (REPO / "docs" / "COMMANDS.md").read_text(encoding="utf-8")
README = (REPO / "README.md").read_text(encoding="utf-8")
SKILL = (REPO / "skills" / "obicfg" / "SKILL.md").read_text(encoding="utf-8")
RECIPES = (REPO / "skills" / "obicfg" / "references" / "recipes.md").read_text(
    encoding="utf-8"
)


def _subcommands():
    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise AssertionError("the parser has no subcommands")


@pytest.mark.parametrize("name", sorted(_subcommands()))
def test_every_command_has_a_reference_section(name):
    assert f"## `{name}`" in COMMANDS


@pytest.mark.parametrize(
    "name,flag",
    [
        (name, option)
        for name, sub in sorted(_subcommands().items())
        for action in sub._actions
        for option in action.option_strings
        if option.startswith("--") and option != "--help"
    ],
)
def test_every_flag_is_documented(name, flag):
    assert flag in COMMANDS, f"{name} {flag} is not in docs/COMMANDS.md"


def test_the_documented_exit_codes_are_the_real_ones():
    for code in (EXIT_OK, EXIT_USAGE, EXIT_VERIFY, EXIT_GUARD):
        assert f"| `{code}` |" in COMMANDS
    # And the skill's table, which agents branch on.
    for code in (0, 1, 2, 3, 4):
        assert f"| {code} |" in SKILL


@pytest.mark.parametrize(
    "profile", sorted((REPO / "examples").glob("*.toml")), ids=lambda p: p.name
)
def test_every_example_profile_parses(profile):
    if profile.name == "config.toml":
        pytest.skip("connection settings, not a profile")
    loaded = load(profile)
    assert loaded.settings, f"{profile.name} sets nothing"
    for path in loaded.settings:
        assert "." in path


@pytest.mark.parametrize(
    "alias", sorted(a for a in ALIASES if not a.startswith(("bt", "wizard")))
)
def test_documented_aliases_exist_in_the_alias_table(alias):
    # recipes.md is where people look them up; an alias missing from it is
    # effectively undiscoverable.
    assert alias in RECIPES or ALIASES[alias] in RECIPES


@pytest.mark.parametrize(
    "link",
    [
        "docs/COMMANDS.md",
        "skills/obicfg/references/recipes.md",
        "examples/pbx-trunks.toml",
        "examples/p2p-direct.toml",
        "examples/config.toml",
        "LICENSE",
    ],
)
def test_readme_links_resolve(link):
    assert link in README
    assert (REPO / link).exists()


def test_skill_links_resolve():
    assert (REPO / "skills" / "obicfg" / "references" / "recipes.md").exists()
    assert (REPO / "docs" / "COMMANDS.md").exists()


def test_the_json_support_table_in_the_skill_is_accurate():
    supported, unsupported = set(), set()
    for name, sub in _subcommands().items():
        has_json = any("--json" in a.option_strings for a in sub._actions)
        (supported if has_json else unsupported).add(name)
    # The skill tells agents which commands they can parse; being wrong here
    # sends them looking for JSON that will never arrive.
    table = SKILL.split("| `--json` supported | no `--json` |")[1].split("\n")[2]
    for name in supported:
        assert f"`{name}`" in table.split("|")[1]
    for name in unsupported:
        assert f"`{name}`" in table.split("|")[2]
