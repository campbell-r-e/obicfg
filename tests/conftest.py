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
from obicfg.errors import TransportError  # noqa: E402


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
            raise TransportError(f"{path} returned HTTP 404")
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


def _renumber(text: str, prefix: str) -> str:
    """Give a reused fixture its own hashes.

    FakeClient stores a write by finding the hash anywhere in its pages, so
    four services sharing one fixture's hashes would all land on the first
    copy -- and a test asserting "SP2 changed" would pass while SP1 moved.
    """
    return re.sub(r'hash="aaaa(\d{4})"', lambda m: f'hash="{prefix}{m.group(1)}"', text)


def make_device(*, guard=None, deaf_hashes=frozenset(), **kwargs):
    """A Device over a fake OBi laid out with real page names, so aliases work."""
    from obicfg.device import Device
    from obicfg.guard import Guard

    service = fixture("EXAMPLE_SVC_.xml")
    pages = {
        "DI_S_": fixture("EXAMPLE_STATUS_.xml"),
        "PI_FXS_1_Stats": fixture("EXAMPLE_PORT_.xml"),
        "VS_1_VP_1_L_1_Stats": fixture("EXAMPLE_QUALITY_.xml"),
        "callhistory": fixture("callhistory.xml"),
        "VS_1_VP_1_L_1_": _renumber(service, "a001"),
        "VS_1_VP_1_L_2_": _renumber(service, "a002"),
        "VS_1_VP_1_L_3_": _renumber(service, "a003"),
        "VS_1_VP_1_L_4_": _renumber(service, "a004"),
        "VS_1_X_FXS_1_": _renumber(service, "a005"),
    }
    client = FakeClient(pages=pages, deaf_hashes=deaf_hashes, **kwargs)
    client.menu = "\n".join(
        f"""<a href="javascript:void(0);" onclick="e('{name}.xml')" class="cmenu">{name}</a>"""
        for name in (
            "DI_S_",
            "PI_FXS_1_Stats",
            "VS_1_VP_1_L_1_Stats",
            "VS_1_VP_1_L_1_",
            "VS_1_VP_1_L_2_",
            "VS_1_VP_1_L_3_",
            "VS_1_VP_1_L_4_",
            "VS_1_X_FXS_1_",
        )
    )
    return Device(client, guard if guard is not None else Guard(enabled=False))
