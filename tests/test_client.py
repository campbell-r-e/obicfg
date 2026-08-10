"""Wire-level tests.

These exist because the encoding is the whole ballgame.  A value like
``{(<**1:>(Msp1)):sp1}`` is ordinary in an OBi call route, and a conventional
HTTP client will percent-encode those braces on its way out; the device then
stores the escape sequences verbatim and the phone silently stops routing.
So the tests below assert on the bytes a real server receives, not on the
client's own idea of what it sent.
"""

from __future__ import annotations

import hashlib
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from obicfg.client import Client, Transport, build_parameter_list, encode_uri_component
from obicfg.errors import AuthError, ValidationError

REALM = "OBi"
NONCE = "deadbeef"
USER = "admin"
PASSWORD = "admin"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _expected_response(method: str, uri: str, nc: str, cnonce: str) -> str:
    ha1 = _md5(f"{USER}:{REALM}:{PASSWORD}")
    ha2 = _md5(f"{method}:{uri}")
    return _md5(f"{ha1}:{NONCE}:{nc}:{cnonce}:auth:{ha2}")


class _Recorder(BaseHTTPRequestHandler):
    """A tiny digest-protected server that records exactly what it received."""

    protocol_version = "HTTP/1.1"
    received: list = []

    def log_message(self, *args):  # keep test output clean
        pass

    def _challenge(self):
        self.send_response(401)
        self.send_header(
            "WWW-Authenticate",
            f'Digest realm="{REALM}", qop="auth", nonce="{NONCE}", opaque="0"',
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _authenticated(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Digest "):
            return False
        parsed = {
            key: quoted or bare
            for key, quoted, bare in re.findall(
                r'(\w+)=(?:"([^"]*)"|([^,\s]+))', header[len("Digest ") :]
            )
        }
        if parsed.get("username") != USER:
            return False
        expected = _expected_response(
            self.command, parsed.get("uri", ""), parsed.get("nc", ""), parsed.get("cnonce", "")
        )
        return parsed.get("response") == expected

    def _serve(self):
        if not self._authenticated():
            self._challenge()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).received.append(
            {"method": self.command, "path": self.path, "body": body.decode("latin-1")}
        )
        payload = b"<html>done</html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _serve
    do_POST = _serve


@pytest.fixture
def server():
    _Recorder.received = []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, _Recorder.received
    httpd.shutdown()
    httpd.server_close()


def _client(httpd, **kwargs) -> Client:
    host, port = httpd.server_address[:2]
    return Client(host=host, port=port, username=USER, password=PASSWORD, **kwargs)


# -- encoding units -------------------------------------------------------


def test_encode_uri_component_matches_javascript():
    # encodeURIComponent leaves these alone and escapes everything else.
    assert encode_uri_component("-_.!~*'()") == "-_.!~*'()"
    assert encode_uri_component("a b") == "a%20b"
    assert encode_uri_component("{x}") == "%7Bx%7D"
    assert encode_uri_component("a#b") == "a%23b"
    assert encode_uri_component("&=") == "%26%3D"


def test_build_parameter_list_joins_like_the_web_ui():
    assert build_parameter_list([("aaaa", "{x}"), ("bbbb", "y z")]) == (
        "aaaa=%7Bx%7D&bbbb=y%20z"
    )


# -- transport behaviour --------------------------------------------------


def test_digest_auth_and_reads(server):
    httpd, received = server
    body = _client(httpd).fetch("EXAMPLE_SVC_.xml")
    assert body == b"<html>done</html>"
    assert received[-1]["method"] == "GET"
    assert received[-1]["path"] == "/EXAMPLE_SVC_.xml"


def test_bad_credentials_raise_autherror(server):
    httpd, _ = server
    host, port = httpd.server_address[:2]
    client = Client(host=host, port=port, username=USER, password="wrong")
    with pytest.raises(AuthError):
        client.fetch("EXAMPLE_SVC_.xml")


def test_query_transport_sends_braces_unescaped(server):
    httpd, received = server
    client = _client(httpd, transport=Transport.QUERY)
    route = "{(<**1:>(Msp1)):sp1},{(Mpli):pli}"
    client.write([("fe7a1009", route)])
    # The braces, angle brackets and colons must arrive exactly as typed --
    # anything percent-encoded here would be stored as literal '%7B'.
    assert received[-1]["path"] == f"/result.html?fe7a1009={route}"


@pytest.mark.parametrize(
    "value",
    ["SP3 Service", "a#b", "a&b", "a=b", "a+b", "{x} & {y}", "50% off"],
)
def test_query_transport_rejects_anything_it_cannot_carry_intact(server, value):
    """The property, not the current contents of the hostile-character list.

    Asserting the two entries that used to be in that dict meant adding '&'
    changed nothing about the test -- and '&' was the dangerous one, because
    the tail of the value became a second write to a parameter nobody named.
    """
    httpd, received = server
    client = _client(httpd, transport=Transport.QUERY)
    safe = all(c not in value for c in " #&=+")
    if safe:
        client.write([("aaaa0007", value)])
        assert received[-1]["path"] == f"/result.html?aaaa0007={value}"
        return
    with pytest.raises(ValidationError):
        client.write([("aaaa0007", value)])
    assert not received, "a rejected value must never reach the wire"


def test_paramlist_transport_round_trips_through_two_decodes(server):
    httpd, received = server
    client = _client(httpd, transport=Transport.PARAMLIST)
    value = "SP3 Service #2 {x}"
    client.write([("aaaa0007", value)])

    request = received[-1]
    assert request["method"] == "POST"
    assert request["path"] == "/result.html"

    # Undo exactly what the browser does: form decode, then the per-value
    # encodeURIComponent the page's own JavaScript applied.
    outer = urllib.parse.parse_qs(request["body"], keep_blank_values=True)
    inner = outer["ParameterList"][0]
    pairs = dict(
        (k, urllib.parse.unquote(v))
        for k, v in (item.split("=", 1) for item in inner.split("&"))
    )
    assert pairs == {"aaaa0007": value}


def test_paramlist_batches_many_parameters_into_one_request(server):
    httpd, received = server
    client = _client(httpd)
    client.write([("aaaa0001", "true"), ("aaaa0002", "5061"), ("aaaa0003", "TLS")])
    assert len(received) == 1
    inner = urllib.parse.parse_qs(received[0]["body"])["ParameterList"][0]
    assert inner == "aaaa0001=true&aaaa0002=5061&aaaa0003=TLS"


def test_write_with_no_pairs_is_a_programming_error(server):
    httpd, _ = server
    with pytest.raises(ValueError):
        _client(httpd).write([])


def test_unreachable_host_reports_the_host():
    client = Client(host="127.0.0.1", port=1, timeout=1.0)
    with pytest.raises(Exception) as excinfo:
        client.fetch("DI_S_.xml")
    assert "127.0.0.1" in str(excinfo.value)


class _Broken(_Recorder):
    """A server that answers everything with 500, to exercise error mapping."""

    def _serve(self):
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _serve
    do_POST = _serve


@pytest.fixture
def broken_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Broken)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def test_a_server_error_is_reported_with_the_status(broken_server):
    from obicfg.errors import TransportError

    host, port = broken_server.server_address[:2]
    client = Client(host=host, port=port)
    with pytest.raises(TransportError, match="HTTP 500"):
        client.fetch("DI_S_.xml")


def test_a_socket_level_failure_is_a_transport_error(monkeypatch):
    from obicfg.errors import TransportError

    client = Client(host="127.0.0.1", port=9)

    def boom(*args, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(client._opener, "open", boom)
    with pytest.raises(TransportError, match="connection reset"):
        client.fetch("DI_S_.xml")


def test_reboot_requests_the_endpoint(server):
    httpd, received = server
    _client(httpd).reboot()
    assert received[-1]["path"] == "/rebootgetconfig.htm"


def test_reboot_tolerates_the_connection_dropping(monkeypatch):
    # The unit routinely dies mid-reply as it goes down. That is a successful
    # reboot, not a failure, and must not raise.
    client = Client(host="127.0.0.1", port=9)

    def boom(*args, **kwargs):
        raise OSError("connection reset by peer")

    monkeypatch.setattr(client._opener, "open", boom)
    client.reboot()


def test_the_query_transport_really_does_post(server):
    # An empty body is falsy; testing truthiness instead of `is not None`
    # turned every query-transport write into a GET.
    httpd, received = server
    _client(httpd, transport=Transport.QUERY).write([("aaaa0001", "true")])
    assert received[-1]["method"] == "POST"
