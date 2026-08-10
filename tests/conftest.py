"""Shared test helpers.

The fake device below is deliberately faithful about the one behaviour that
matters: it can accept a write, answer HTTP 200, and not apply it.  Real OBi
firmware does exactly that for values it dislikes, and any tool that trusts
the status code will report success on a device it never changed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

FIXTURES = Path(__file__).parent / "fixtures"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from obicfg.client import Response, Transport  # noqa: E402


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeClient:
    """An in-memory OBi that stores writes by hash."""

    def __init__(
        self,
        pages: dict | None = None,
        *,
        deaf_hashes: frozenset = frozenset(),
        transport: Transport = Transport.PARAMLIST,
    ) -> None:
        self.pages = dict(
            pages
            or {
                "EXAMPLE_SVC_": fixture("EXAMPLE_SVC_.xml"),
                "EXAMPLE_SYS_": fixture("EXAMPLE_SYS_.xml"),
            }
        )
        self.menu = fixture("menu.htm")
        #: Hashes the fake device silently refuses to change.
        self.deaf_hashes = deaf_hashes
        self.transport = transport
        self.host = "fake"
        self.writes: list = []
        self.reboots = 0
        self.fetches: list = []

    # -- reads ------------------------------------------------------------

    def fetch(self, path: str) -> bytes:
        self.fetches.append(path)
        if path == "menu.htm":
            return self.menu.encode()
        name = path[: -len(".xml")] if path.endswith(".xml") else path
        if name not in self.pages:
            raise KeyError(f"no such page: {path}")
        return self.pages[name].encode()

    # -- writes -----------------------------------------------------------

    def write(self, pairs) -> Response:
        self.writes.append(list(pairs))
        for hash_, value in pairs:
            if hash_ in self.deaf_hashes:
                continue
            self._store(hash_, value)
        return Response(200, b"<html>ok</html>", "http://fake/result.html")

    def _store(self, hash_: str, value: str) -> None:
        for name, text in self.pages.items():
            pattern = re.compile(
                r'(<value hash="%s"[^>]*?)( current="[^"]*")?( ?>)' % re.escape(hash_)
            )
            match = pattern.search(text)
            if not match:
                continue
            if value == "default":
                replacement = match.group(1) + match.group(3)
            else:
                replacement = (
                    match.group(1) + f' current="{escape(value, {chr(34): "&quot;"})}"'
                    + match.group(3)
                )
            self.pages[name] = text[: match.start()] + replacement + text[match.end() :]
            return

    def reboot(self) -> None:
        self.reboots += 1
