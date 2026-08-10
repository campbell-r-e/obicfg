"""HTTP transport for the OBi200 admin interface.

Stdlib only, on purpose: this tool is meant to run unchanged on the little
Alpine/i686 boxes that sit next to these ATAs, where ``pip install requests``
is a chore and a compiled dependency is a trap.  It also matters that we can
control the exact bytes on the wire, which a higher-level HTTP library
actively prevents (see ``Transport.QUERY`` below).

Two ways to write a parameter
-----------------------------

``Transport.PARAMLIST`` (default) is what the admin web UI itself does.
``default.xsl`` builds ``hash=encodeURIComponent(value)`` pairs, joins them
with ``&``, stuffs the result into a single hidden ``ParameterList`` field and
POSTs that form to ``result.html``.  The value therefore crosses the wire
percent-encoded *twice* -- once by the page's JavaScript, once by the browser's
form serialiser -- and the device undoes both.  Consequences:

* values containing spaces and ``#`` work, which the query form cannot express;
* many parameters can be written in one request, which is far faster than one
  round trip per setting.

``Transport.QUERY`` is the older, blunter form: ``POST /result.html?hash=value``
with the value sitting raw in the query string.  Nothing decodes it, so the
bytes are stored verbatim -- send ``%20`` and you get a literal ``%20``.  It is
kept because it is the method with the most field mileage behind it, and
because it is the fallback if a firmware ever ignores ``ParameterList``.

Neither form reports failure.  ``result.html`` returns 200 for writes it
quietly discards, so callers must read the value back.  :mod:`obicfg.device`
does that for you.
"""

from __future__ import annotations

import enum
import http.client
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .errors import AuthError, TransportError, ValidationError

#: Characters JavaScript's encodeURIComponent() leaves alone.  Matching it
#: exactly keeps our writes byte-identical to the ones the web UI sends.
_JS_SAFE = "-_.!~*'()"

#: Characters that cannot survive the query-string transport. The last three
#: are not merely mangled -- '&' ends the assignment, so the remainder of the
#: value is read as a *second* parameter and written somewhere nobody asked
#: for, which the read-back on the intended parameter cannot detect.
_QUERY_HOSTILE = {
    " ": "a space is not legal in an HTTP request line",
    "#": "a '#' starts a URL fragment and truncates everything after it",
    "&": "an '&' ends this assignment, so the rest of the value would be "
         "parsed as a second parameter and written to whatever it names",
    "=": "an '=' makes the split between parameter and value ambiguous",
    "+": "a '+' may be decoded as a space, silently changing the value",
}


class Transport(enum.Enum):
    """How a write is put on the wire."""

    PARAMLIST = "paramlist"
    QUERY = "query"


def encode_uri_component(text: str) -> str:
    """Python equivalent of JavaScript's ``encodeURIComponent``."""
    return urllib.parse.quote(text, safe=_JS_SAFE, encoding="utf-8")


def build_parameter_list(pairs: list[tuple[str, str]]) -> str:
    """Build the inner ``ParameterList`` string for a batch of writes."""
    return "&".join(
        f"{encode_uri_component(h)}={encode_uri_component(v)}" for h, v in pairs
    )


def check_query_safe(value: str) -> None:
    """Reject values the query transport would silently mangle."""
    for char, why in _QUERY_HOSTILE.items():
        if char in value:
            raise ValidationError(
                f"value {value!r} cannot be sent with the 'query' transport: "
                f"{why}. Use the default 'paramlist' transport instead."
            )


@dataclass
class Response:
    status: int
    body: bytes
    url: str


class Client:
    """A thin, deliberately dumb HTTP client for one device."""

    def __init__(
        self,
        host: str,
        username: str = "admin",
        password: str = "admin",
        *,
        scheme: str = "http",
        port: int | None = None,
        timeout: float = 15.0,
        transport: Transport = Transport.PARAMLIST,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.scheme = scheme
        self.port = port
        self.timeout = timeout
        self.transport = transport
        self._opener = self._build_opener()

    # -- plumbing ---------------------------------------------------------

    @property
    def base_url(self) -> str:
        netloc = self.host if self.port is None else f"{self.host}:{self.port}"
        return f"{self.scheme}://{netloc}"

    def _build_opener(self) -> urllib.request.OpenerDirector:
        manager = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        manager.add_password(None, self.base_url, self.username, self.password)
        return urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(manager),
            urllib.request.HTTPBasicAuthHandler(manager),
        )

    def _open(self, url: str, data: bytes | None = None) -> Response:
        # `data is not None`, not `data` -- the query transport posts an empty
        # body, and truthiness would silently downgrade those writes to GET.
        method = "POST" if data is not None else "GET"
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("User-Agent", "obicfg")
        if data is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                # .status only exists from 3.9; .getcode() goes back further.
                status = getattr(response, "status", None) or response.getcode()
                return Response(int(status), response.read(), response.url)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise AuthError(
                    f"{self.host} rejected the credentials for user "
                    f"{self.username!r} (HTTP {exc.code})"
                ) from None
            raise TransportError(f"{url} returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise TransportError(f"cannot reach {self.host}: {exc.reason}") from None
        except (OSError, http.client.HTTPException) as exc:
            # HTTPException is NOT an OSError subclass. A device that is
            # rebooting routinely produces IncompleteRead or BadStatusLine,
            # and those must be retryable transport failures rather than a
            # traceback out of `reboot --wait`.
            raise TransportError(f"cannot reach {self.host}: {exc}") from None

    # -- reads ------------------------------------------------------------

    def fetch(self, path: str) -> bytes:
        """GET a path (e.g. ``DI_S_.xml``) and return the raw body."""
        return self._open(f"{self.base_url}/{path.lstrip('/')}").body

    # -- writes -----------------------------------------------------------

    def write(self, pairs: list[tuple[str, str]]) -> Response:
        """Write ``(hash, value)`` pairs. Success here means nothing; read back."""
        if not pairs:
            raise ValueError("no parameters to write")
        if self.transport is Transport.PARAMLIST:
            return self._write_paramlist(pairs)
        return self._write_query(pairs)

    def _write_paramlist(self, pairs: list[tuple[str, str]]) -> Response:
        inner = build_parameter_list(pairs)
        body = urllib.parse.urlencode({"ParameterList": inner}).encode("ascii")
        return self._open(f"{self.base_url}/result.html", data=body)

    def _write_query(self, pairs: list[tuple[str, str]]) -> Response:
        # The value goes into the request line untouched -- no quoting, which
        # is exactly why this cannot go through requests/urllib3 (they would
        # helpfully percent-encode the braces that DigitMap and CallRoute
        # values are made of, and the device would store the escapes).
        for _, value in pairs:
            check_query_safe(value)
        query = "&".join(f"{h}={v}" for h, v in pairs)
        return self._open(f"{self.base_url}/result.html?{query}", data=b"")

    # -- device control ---------------------------------------------------

    def reboot(self) -> None:
        """Reboot the device. Configuration survives; the unit is gone ~30 s."""
        try:
            self._open(f"{self.base_url}/rebootgetconfig.htm")
        except TransportError:
            # The unit frequently drops the connection mid-reply as it goes
            # down, which is a successful reboot, not a failure.
            pass
