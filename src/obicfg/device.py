"""The high-level device object: discover, read, write, verify.

This is where the tool's one non-negotiable rule lives: **a write is not
finished until the value has been read back**.  ``result.html`` answers HTTP
200 whether it applied a change, ignored it, or quietly truncated it, so the
status code carries no information at all.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Iterator

from .client import Client, Transport
from .errors import (
    ResolutionError,
    TransportError,
    ValidationError,
    VerificationError,
)
from .guard import Guard
from .model import Page, Parameter, parse_menu, parse_page
from .naming import resolve_page, split_path

#: Pages that report live state rather than configuration. Excluded from
#: snapshots by default because they differ on every read (uptime, call
#: counters) and would swamp a diff.
STATUS_PAGES = ("DI_S_", "PI_FXS_1_Stats", "VS_1_VP_1_L_1_Stats")

#: Pages that re-present settings which live somewhere else. The Setup Wizard
#: is a second view onto the service pages, with its own parameter names
#: ("SP1 InboundCallRoute") and its own hashes, so including it would double
#: every service setting in a snapshot and clutter every search.
DERIVED_PAGES = ("SetupWizard",)


@dataclass
class Change:
    """One pending or completed write."""

    parameter: Parameter
    old: str
    new: str
    applied: bool = False
    verified: bool = False
    observed: str | None = None

    @property
    def path(self) -> str:
        return self.parameter.path

    @property
    def noop(self) -> bool:
        return self.old == self.new


class Device:
    """One OBi200, with its pages cached for the life of the object."""

    def __init__(self, client: Client, guard: Guard | None = None) -> None:
        self.client = client
        self.guard = guard or Guard()
        self._pages: dict[str, Page] = {}
        self._menu: list[tuple[str, str]] | None = None

    # -- discovery --------------------------------------------------------

    def menu(self) -> list[tuple[str, str]]:
        """``(page, label)`` pairs, straight from the device's own menu.

        Reading the menu rather than hardcoding a page list is what lets one
        binary cope with firmware revisions that add or drop pages.
        """
        if self._menu is None:
            self._menu = parse_menu(self.client.fetch("menu.htm"))
            if not self._menu:
                raise TransportError(
                    "menu.htm contained no page links -- either the credentials "
                    "are wrong or this is not an OBi admin interface"
                )
        return self._menu

    def page_names(self) -> list[str]:
        return [f[: -len(".xml")] for f, _ in self.menu()]

    def label(self, page: str) -> str:
        for filename, text in self.menu():
            if filename[: -len(".xml")] == page:
                return text
        return page

    def page(self, name: str, *, refresh: bool = False) -> Page:
        """Fetch and parse a page, by alias or raw name.

        A name the menu does not list is still tried against the device. The
        menu is not a complete index: ``DM_S_`` (auto-provisioning) and
        ``callhistory.xml`` are both served on request while appearing in no
        menu entry, so refusing to fetch what is not listed would make some
        real pages unreachable -- including one this tool protects.
        """
        resolved = resolve_page(name, self.page_names()) or name
        if refresh or resolved not in self._pages:
            try:
                raw = self.client.fetch(f"{resolved}.xml")
            except TransportError as exc:
                raise ResolutionError(
                    f"no page named {resolved!r} on this device ({exc}) -- try "
                    "`obicfg pages`, or `obicfg search` to find a setting by name"
                ) from None
            self._pages[resolved] = parse_page(resolved, raw)
        return self._pages[resolved]

    def config_pages(self) -> list[str]:
        """Every page holding canonical configuration.

        Status and wizard pages are left out: the first is not configuration
        and the second is a duplicate of it.
        """
        skip = set(STATUS_PAGES) | set(DERIVED_PAGES)
        return [p for p in self.page_names() if p not in skip]

    def iter_pages(self, names: Iterable[str] | None = None) -> Iterator[Page]:
        for name in names if names is not None else self.config_pages():
            yield self.page(name)

    # -- reads ------------------------------------------------------------

    def parameter(self, path: str, *, refresh: bool = False) -> Parameter:
        """Resolve ``sp2.X_InboundCallRoute`` (or the raw form) to a parameter.

        ``<page>#<hash>`` addresses a parameter by its wire identity. That is
        the only form that always works: the RTP statistics page carries four
        objects *all* named "Reset Statistics", so even ``Object.Name`` cannot
        pick one out. The hash is unique by construction.
        """
        if "#" in path:
            page_name, _, wanted_hash = path.partition("#")
            page = self.page(page_name, refresh=refresh)
            parameter = page.by_hash(wanted_hash.strip())
            if parameter is None:
                raise ResolutionError(
                    f"page {page.name} has no parameter with hash "
                    f"{wanted_hash.strip()!r} -- `obicfg show {page.name} --json` "
                    "lists them"
                )
            return parameter
        page_name, param_name = split_path(path)
        try:
            page = self.page(page_name, refresh=refresh)
        except ResolutionError:
            # `EXAMPLE_.Product Information.MACAddress` splits at the last dot,
            # leaving a page name that does not exist. Retry treating the last
            # two segments as the qualified `Object.Name` form.
            head, _, tail = page_name.rpartition(".")
            if not head:
                raise
            page = self.page(head, refresh=refresh)
            param_name = f"{tail}.{param_name}"
        matches = page.candidates(param_name)
        if len(matches) > 1:
            raise ResolutionError(
                f"{param_name!r} is ambiguous on page {page.name} ({page.title}) "
                f"-- it names {len(matches)} parameters. Say which one:\n"
                + "\n".join(
                    f"  {page.name}#{m.hash}  ({m.obj}.{m.name})"
                    f"{'' if m.writable else '  [read-only]'}"
                    f"  = {m.value!r}"
                    for m in matches
                )
                + "\n  Object.Name works where the object names differ; the "
                "#hash form always works."
            )
        parameter = matches[0] if matches else None
        if parameter is None:
            near = [
                p.name
                for p in page.parameters
                if param_name.lower() in p.name.lower()
            ][:5]
            hint = f" Did you mean: {', '.join(near)}?" if near else ""
            raise ResolutionError(
                f"page {page.name} ({page.title}) has no parameter "
                f"{param_name!r}.{hint}"
            )
        return parameter

    def search(self, pattern: str, *, pages: Iterable[str] | None = None):
        """Find parameters whose name, path or description matches ``pattern``.

        This is the escape hatch that makes the alias table optional: you do
        not need to know that call routing for SP2 lives on ``VS_1_VP_1_L_2_``,
        only that the word "InboundCallRoute" exists somewhere.
        """
        import re

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValidationError(
                f"invalid regular expression {pattern!r}: {exc}"
            ) from None
        for page in self.iter_pages(pages):
            for parameter in page.parameters:
                haystack = f"{parameter.path} {parameter.description}"
                if regex.search(haystack):
                    yield parameter

    def snapshot(
        self,
        *,
        include_status: bool = False,
        redact: bool = False,
        writable_only: bool = True,
    ) -> dict:
        """A diffable picture of the device's configuration.

        Read-only parameters are excluded by default.  They are readouts, not
        settings -- and some of them, like the WAN page's clock, change every
        second, which would make every diff dirty and hide the one line that
        actually matters.
        """
        names = self.page_names() if include_status else self.config_pages()
        pages: dict[str, dict] = {}
        for page in self.iter_pages(names):
            entries = {}
            duplicated_names: dict = {}
            for parameter in page.parameters:
                key = parameter.name.lower()
                duplicated_names[key] = duplicated_names.get(key, 0) + 1
            for parameter in page.parameters:
                if writable_only and not parameter.writable:
                    continue
                value = parameter.value
                if redact and _is_secret(parameter):
                    value = "<redacted>"
                # A page may repeat a name across <object> blocks -- a codec
                # profile has five BitRates. Keying on the bare name kept only
                # the last, so a backup silently lost rows. Qualify every
                # member of a duplicated set, not just the later ones, or the
                # first keeps a bare key that means something different from
                # the others.
                key = (
                    f"{parameter.obj}.{parameter.name}"
                    if duplicated_names.get(parameter.name.lower(), 0) > 1
                    else parameter.name
                )
                entries[key] = {
                    "hash": parameter.hash,
                    "value": value,
                    "default": parameter.default,
                    "is_default": parameter.is_default,
                }
            pages[page.name] = {"title": page.title, "parameters": entries}
        return {
            "host": self.client.host,
            # Recorded so `diff` can read the device back at the same scope.
            # Comparing an --all --include-status snapshot against a default
            # read of the device reports every excluded parameter as deleted.
            "scope": {
                "include_status": include_status,
                "writable_only": writable_only,
            },
            "pages": pages,
        }

    def identity(self) -> dict[str, str]:
        """Model, firmware, MAC and so on.

        The status page is located by looking for the page that carries
        ``ModelName`` rather than by assuming a filename, so this keeps
        working on firmware that renames or moves it.
        """
        page = self._identity_page()
        # ModelName's *default* is "OBi100" across the whole family -- the real
        # model only ever appears in `current`, which Parameter.value handles.
        wanted = (
            "ModelName",
            "SerialNumber",
            "MACAddress",
            "HardwareVersion",
            "SoftwareVersion",
            "IPAddress",
        )
        found: dict[str, str] = {}
        for key in wanted:
            parameter = page.get(key)
            if parameter is not None:
                found[key] = parameter.value
        return found

    def _identity_page(self) -> Page:
        names = self.page_names()
        # Check the usual status pages first so the common case costs one fetch.
        ordered = [n for n in names if n in STATUS_PAGES]
        ordered += [n for n in names if n not in STATUS_PAGES]
        for name in ordered:
            page = self.page(name)
            if page.get("ModelName") is not None:
                return page
        raise ResolutionError(
            "no page on this device reports ModelName, so its identity cannot "
            "be read -- is this an OBi admin interface?"
        )

    # -- writes -----------------------------------------------------------

    def _guard_check_early(self, path: str) -> None:
        """Refuse a protected page before we even look it up on the device."""
        if "#" in path:
            page_name, _, wanted_hash = path.partition("#")
            self.guard.check_static(f"{page_name}.{wanted_hash.strip()}")
            return
        page_name, param_name = split_path(path)
        self.guard.check_static(f"{page_name}.{param_name}")

    def _guard_check(self, parameter: Parameter) -> None:
        """Run the guard with enough context to spot provisioned credentials.

        The siblings matter: a password is only recognisable as provisioned
        by the macro in the username sitting next to it.
        """
        siblings = self.page(parameter.page).parameters
        self.guard.check(parameter.path, parameter, siblings)

    def plan(self, assignments: Iterable[tuple[str, object]]) -> list[Change]:
        """Turn ``(path, value)`` pairs into validated, guard-checked changes.

        Nothing is sent.  Values are coerced against the device's declared
        syntax and unchanged settings are marked as no-ops, so a plan is safe
        to print and safe to hand to :meth:`apply`.
        """
        changes: list[Change] = []
        for path, value in assignments:
            self._guard_check_early(path)
            parameter = self.parameter(path)
            self._guard_check(parameter)
            new = parameter.coerce(value)
            if self.client.transport is Transport.QUERY:
                from .client import check_query_safe

                check_query_safe(new)
            changes.append(Change(parameter=parameter, old=parameter.value, new=new))
        return changes

    def apply(self, changes: list[Change], *, verify: bool = True) -> list[Change]:
        """Send the non-no-op changes, one request per page, then verify.

        Batching by page mirrors what the web UI does when you hit Submit,
        and turns a twenty-setting reconfiguration from twenty round trips
        into a handful.
        """
        pending = [c for c in changes if not c.noop]
        if not pending:
            return changes

        by_page: dict[str, list[Change]] = {}
        for change in pending:
            by_page.setdefault(change.parameter.page, []).append(change)

        for page_name, page_changes in by_page.items():
            self.client.write([(c.parameter.hash, c.new) for c in page_changes])
            for change in page_changes:
                change.applied = True
            if not verify:
                continue
            fresh = self.page(page_name, refresh=True)
            for change in page_changes:
                parameter = fresh.by_hash(change.parameter.hash)
                change.observed = None if parameter is None else parameter.value
                change.verified = change.observed == change.new
        return changes

    def verify_failures(self, changes: list[Change]) -> list[Change]:
        return [c for c in changes if c.applied and not c.verified]

    def assert_applied(self, changes: list[Change]) -> None:
        failures = self.verify_failures(changes)
        if failures:
            detail = "\n".join(
                f"  {c.path}: wanted {c.new!r}, device reports {c.observed!r}"
                for c in failures
            )
            raise VerificationError(
                "the device accepted these writes (HTTP 200) but did not apply "
                f"them:\n{detail}"
            )

    def plan_reset(self, paths: Iterable[str]) -> list[Change]:
        """What a reset would do, without doing it.

        A reset is destructive in the same way a write is -- it discards
        whatever is there now -- so it needs the same dry run a write gets.
        """
        changes: list[Change] = []
        for path in paths:
            self._guard_check_early(path)
            parameter = self.parameter(path)
            self._guard_check(parameter)
            changes.append(
                Change(
                    parameter=parameter,
                    old=parameter.value,
                    new=parameter.default,
                )
            )
        return changes

    def reset_to_default(self, paths: Iterable[str]) -> list[Change]:
        """Restore parameters to their factory values.

        The device's own reset mechanism is the literal string ``default``
        submitted in place of a value, which is why no parameter can ever
        legitimately hold that string.
        """
        changes: list[Change] = []
        for path in paths:
            self._guard_check_early(path)
            parameter = self.parameter(path)
            self._guard_check(parameter)
            if parameter.is_default:
                changes.append(
                    Change(parameter=parameter, old=parameter.value, new=parameter.value)
                )
                continue
            self.client.write([(parameter.hash, "default")])
            fresh = self.page(parameter.page, refresh=True).by_hash(parameter.hash)
            change = Change(
                parameter=parameter,
                old=parameter.value,
                new=parameter.default,
                applied=True,
            )
            change.observed = None if fresh is None else fresh.value
            change.verified = fresh is not None and fresh.is_default
            changes.append(change)
        return changes

    # -- lifecycle --------------------------------------------------------

    def reboot(self, *, wait: bool = False, timeout: float = 120.0) -> bool:
        """Reboot, optionally blocking until the admin interface answers again."""
        self.client.reboot()
        self._pages.clear()
        self._menu = None
        if not wait:
            return True
        deadline = time.time() + timeout
        time.sleep(5)  # it has not even dropped the link yet
        while time.time() < deadline:
            try:
                self.client.fetch("DI_S_.xml")
                return True
            except TransportError:
                time.sleep(3)
        return False


#: Marker written in place of a secret by ``snapshot(redact=True)``.
REDACTED = "<redacted>"


def _is_secret(parameter: Parameter) -> bool:
    lowered = parameter.name.lower()
    if lowered.startswith("show"):
        return False  # e.g. ShowAccessPointPassword is a checkbox, not a secret
    return "password" in lowered or "authpass" in lowered


def diff_snapshots(before: dict, after: dict) -> list[tuple[str, str, str]]:
    """``(path, before, after)`` for every parameter whose value differs.

    Redacted entries are skipped rather than reported as changes: a redacted
    snapshot never held the secret, so it cannot say whether it changed.
    """
    out: list[tuple[str, str, str]] = []
    old_pages = before.get("pages", {})
    new_pages = after.get("pages", {})
    for page in sorted(set(old_pages) | set(new_pages)):
        old = old_pages.get(page, {}).get("parameters", {})
        new = new_pages.get(page, {}).get("parameters", {})
        for name in sorted(set(old) | set(new)):
            old_value = old.get(name, {}).get("value", "<absent>")
            new_value = new.get(name, {}).get("value", "<absent>")
            if REDACTED in (old_value, new_value):
                continue
            if old_value != new_value:
                out.append((f"{page}.{name}", old_value, new_value))
    return out


def count_redacted(snapshot: dict) -> int:
    """How many values in a snapshot were blanked out."""
    return sum(
        1
        for page in snapshot.get("pages", {}).values()
        for entry in page.get("parameters", {}).values()
        if entry.get("value") == REDACTED
    )
