"""End-to-end tests through a real HTTP server that behaves like an OBi.

The rest of the suite tests the layers apart: `test_client.py` asserts the
bytes on the wire, `test_device.py` and `test_cli.py` drive a fake client that
stores values directly. Nothing joined them, so no test ever carried an
awkward value — braces, an ampersand, a percent sign, non-ASCII — from
`obicfg set` all the way through the encoder, over HTTP, into a decoder that
undoes what the device's own JavaScript would have done, and back out through
the read-back.

That is the seam where a plausible-looking encoding bug hides, so it gets a
server that decodes writes the way the firmware does.
"""

from __future__ import annotations

import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

import pytest
from conftest import fixture

from obicfg.client import Client, Transport
from obicfg.device import Device
from obicfg.guard import Guard

MENU = (
    """<a href="#" onclick="e('VS_1_VP_1_L_2_.xml')" class="cmenu">SP2</a>"""
)


class ObiEmulator(BaseHTTPRequestHandler):
    """An OBi that decodes ``ParameterList`` the way the real firmware does.

    The admin page's JavaScript percent-encodes each value once and the
    browser's form serialiser encodes the joined string again, so the device
    undoes two layers. Reproducing that here is the point: if the client's
    encoding is wrong in a way that happens to round-trip through a naive
    server, this catches it and a simpler fake would not.
    """

    protocol_version = "HTTP/1.1"
    pages: dict = {}
    #: Hashes to accept and ignore, reproducing the device's silent drop.
    deaf: set = set()

    def log_message(self, *args):
        pass

    def _send(self, body: bytes, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.lstrip("/")
        if path == "menu.htm":
            return self._send(MENU.encode())
        name = path[: -len(".xml")] if path.endswith(".xml") else path
        if name not in type(self).pages:
            return self._send(b"not found", 404)
        return self._send(type(self).pages[name].encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("latin-1") if length else ""
        query = urllib.parse.urlparse(self.path).query

        if query:  # the raw query-string transport: stored verbatim
            pairs = [
                (item.split("=", 1) + [""])[:2] for item in query.split("&") if item
            ]
        else:  # the ParameterList transport: two layers of decoding
            outer = urllib.parse.parse_qs(body, keep_blank_values=True)
            inner = outer.get("ParameterList", [""])[0]
            pairs = [
                (k, urllib.parse.unquote(v))
                for k, v in (
                    (item.split("=", 1) + [""])[:2]
                    for item in inner.split("&")
                    if item
                )
            ]

        for hash_, value in pairs:
            if hash_ in type(self).deaf:
                continue
            self._store(hash_, value)
        return self._send(b"<html>ok</html>")

    def _store(self, hash_: str, value: str) -> None:
        for name, text in type(self).pages.items():
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
                    match.group(1)
                    + ' current="%s"' % escape(value, {'"': "&quot;"})
                    + match.group(3)
                )
            type(self).pages[name] = text[: match.start()] + replacement + text[match.end() :]
            return


@pytest.fixture
def obi():
    ObiEmulator.pages = {"VS_1_VP_1_L_2_": fixture("EXAMPLE_SVC_.xml")}
    ObiEmulator.deaf = set()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ObiEmulator)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def device_for(httpd, **kwargs) -> Device:
    host, port = httpd.server_address[:2]
    return Device(Client(host=host, port=port, **kwargs), Guard(enabled=False))


# -- the values that actually break things --------------------------------

AWKWARD = [
    pytest.param("{(<**1:>(Msp1)):sp1}", id="call-route-braces"),
    pytest.param("Front Desk", id="space"),
    pytest.param("Desk #2", id="hash"),
    pytest.param("A&B", id="ampersand"),
    pytest.param("100%", id="percent"),
    pytest.param("a=b", id="equals"),
    pytest.param("a+b", id="plus"),
    pytest.param("Zoë Café", id="non-ascii"),
    pytest.param("  padded  ", id="surrounding-spaces"),
    pytest.param("%7Bnot-decoded%7D", id="looks-percent-encoded"),
    pytest.param("'quoted'", id="apostrophes"),
    pytest.param('say "hi"', id="double-quotes"),
    pytest.param("<tag>", id="angle-brackets"),
]


@pytest.mark.parametrize("value", AWKWARD)
def test_a_value_survives_the_whole_round_trip(obi, value):
    device = device_for(obi)
    # Park the parameter on a sentinel first, so every case is a genuine
    # write. One of the awkward values happens to be the fixture's existing
    # value, and a no-op would prove nothing about the encoding.
    device.apply(device.plan([("sp2.X_InboundCallRoute", "sentinel")]))
    changes = device.apply(device.plan([("sp2.X_InboundCallRoute", value)]))
    assert not changes[0].noop
    assert changes[0].verified, f"{value!r} came back as {changes[0].observed!r}"
    assert device.parameter("sp2.X_InboundCallRoute", refresh=True).value == value


def test_an_empty_value_round_trips(obi):
    device = device_for(obi)
    device.apply(device.plan([("sp2.CallerIDName", "Something")]))
    changes = device.apply(device.plan([("sp2.CallerIDName", "")]))
    assert changes[0].verified
    assert device.parameter("sp2.CallerIDName", refresh=True).value == ""


def test_several_awkward_values_in_one_batched_write(obi):
    # One request carries them all, so a mis-encoded separator in any of them
    # corrupts its neighbours rather than only itself.
    device = device_for(obi)
    changes = device.apply(
        device.plan(
            [
                ("sp2.X_InboundCallRoute", "{(Msp1)):sp1}&x=1"),
                ("sp2.CallerIDName", "Ops #1"),
            ]
        )
    )
    assert all(c.verified for c in changes)
    assert device.parameter("sp2.CallerIDName", refresh=True).value == "Ops #1"


def test_the_query_transport_stores_braces_verbatim(obi):
    device = device_for(obi, transport=Transport.QUERY)
    device.apply(device.plan([("sp2.X_InboundCallRoute", "sentinel")]))
    value = "{(<**1:>(Msp1)):sp1}"
    changes = device.apply(device.plan([("sp2.X_InboundCallRoute", value)]))
    assert not changes[0].noop
    assert changes[0].verified


@pytest.mark.parametrize("value", ["a b", "a#b", "a&b", "a=b", "a+b"])
def test_the_query_transport_refuses_what_it_cannot_carry(obi, value):
    from obicfg.errors import ValidationError

    device = device_for(obi, transport=Transport.QUERY)
    with pytest.raises(ValidationError):
        device.plan([("sp2.CallerIDName", value)])


def test_an_ampersand_would_have_written_a_second_parameter(obi):
    # Proof the refusal above is not academic: sent raw, the tail of the value
    # is parsed as another assignment and lands wherever it names.
    host, port = obi.server_address[:2]
    client = Client(host=host, port=port, transport=Transport.QUERY)
    # Deliberately bypass check_query_safe to show what it is protecting
    # against -- this is the request the tool used to be willing to send.
    client._open(f"{client.base_url}/result.html?aaaa0004=x&aaaa0007=hijacked", data=b"")
    device = device_for(obi)
    assert device.parameter("sp2.X_InboundCallRoute").value == "x"
    assert device.parameter("sp2.CallerIDName").value == "hijacked"


def test_a_silently_dropped_write_is_caught_over_real_http(obi):
    ObiEmulator.deaf = {"aaaa0007"}
    device = device_for(obi)
    changes = device.apply(device.plan([("sp2.CallerIDName", "Nope")]))
    assert changes[0].applied is True
    assert changes[0].verified is False
    assert changes[0].observed == ""


def test_reset_to_default_over_real_http(obi):
    device = device_for(obi)
    assert device.parameter("sp2.Enable").value == "false"
    changes = device.reset_to_default(["sp2.Enable"])
    assert changes[0].verified
    assert device.parameter("sp2.Enable", refresh=True).is_default


def test_a_page_the_menu_does_not_list_is_still_reachable(obi):
    # DM_S_ and callhistory.xml behave exactly like this on real firmware.
    ObiEmulator.pages["DM_S_"] = fixture("EXAMPLE_SVC_.xml")
    device = device_for(obi)
    assert "DM_S_" not in device.page_names()
    assert device.page("DM_S_").title == "Example Service"


def test_a_page_that_really_is_absent_reports_the_404(obi):
    from obicfg.errors import ResolutionError

    device = device_for(obi)
    with pytest.raises(ResolutionError, match="404"):
        device.page("NO_SUCH_PAGE_")
