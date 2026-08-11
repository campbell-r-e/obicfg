"""Live events, by listening to the syslog stream the device pushes.

Everything else in this package polls: ask the device a question, get the
answer as of now. Call history only appears once a call is over, and a
registration that flaps between two polls is invisible.

The device will also *push*, over plain UDP syslog, and that stream carries
call setup, teardown and registration changes as they happen. Point it at a
host, listen on a socket, and you have an event feed.

    Credit where it is due: the idea of using the OBi's UDP syslog stream as a
    live call-event source comes from **obicaller** by Ryan Young
    (https://github.com/YoRyan/obicaller), a public-domain talking caller-ID
    daemon that announces incoming calls through espeak. That project is
    archived and is a shell script rather than Python; nothing is copied from
    it. The insight -- that this otherwise unremarkable debug stream is the
    only real-time interface an OBi has -- is theirs.

The format is minimal: ``<PRI> CATEGORY:message``, NUL-terminated, where PRI
is the usual syslog priority (severity is the low three bits). There is no
timestamp and no hostname, so the receiver stamps arrival time itself.

Nothing here writes to the device. Pointing the device at a listener does
require two settings — see :func:`setup_commands`.
"""

from __future__ import annotations

import re
import socket
import time
from dataclasses import asdict, dataclass, field

#: The device's syslog settings live on the Device Admin page.
SERVER_PARAM = "admin.Server"
PORT_PARAM = "admin.Port"
LEVEL_PARAM = "admin.Level"

SEVERITIES = (
    "emergency",
    "alert",
    "critical",
    "error",
    "warning",
    "notice",
    "info",
    "debug",
)

_PRI = re.compile(r"^<(\d+)>\s*(.*)$", re.S)
_CATEGORY = re.compile(r"^([A-Za-z0-9_]+)\s*:")
_SP = re.compile(r"\bSP([1-4])\b")
#: The line that actually carries caller ID. Credit: obicaller -- this is not
#: something you would guess, and without it nothing is ever announced.
_CID = re.compile(r"\[SLIC\]\s*CID to deliver:\s*(.*)", re.IGNORECASE)
_NUMBER = re.compile(r"[+*#0-9][*#0-9]{2,}")

#: Checked in order, so anything that contains a shorter needle must come
#: first: "Unregistered" contains "registered", and labelling a service drop
#: as a successful registration is exactly backwards.
_CALL_EVENTS = (
    ("call connected", "connected"),
    ("call ended", "ended"),
    ("incoming call", "ringing"),
    ("registration failed", "registration_failed"),
    ("unregistered", "unregistered"),
    ("registered", "registered"),
    ("ringing", "ringing"),
    ("connected", "connected"),
    ("hangup", "ended"),
    ("hang up", "ended"),
)


#: Dropped by default. A unit whose OBiTALK provisioning is gone retries the
#: lookup forever -- measured at ~9.5 datagrams a second on a real device,
#: every one identical. It is not a fault and there is nothing to do about it;
#: it is simply not worth carrying.
DEFAULT_EXCLUDES = (re.compile(r"resolving root\.pnn\.obihai\.com", re.IGNORECASE),)


@dataclass
class Event:
    """One syslog datagram, parsed."""

    received_at: float
    source: str
    raw: str
    message: str
    severity: int | None = None
    severity_name: str | None = None
    facility: int | None = None
    category: str = "system"
    sp: int | None = None
    call_event: str | None = None
    #: The raw CID payload, when the line carried one.
    caller: str | None = None
    number: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def format(self) -> str:
        """A single readable line."""
        stamp = time.strftime("%H:%M:%S", time.localtime(self.received_at))
        marker = f"[{self.call_event}]" if self.call_event else ""
        where = f"SP{self.sp}" if self.sp else self.category
        return " ".join(part for part in (stamp, where, marker, self.message) if part)


@dataclass
class Listener:
    """A bound UDP socket, and the events arriving on it."""

    bind: str = "0.0.0.0"
    port: int = 514
    sock: object = field(default=None, repr=False)

    @property
    def address(self) -> tuple:
        return self.sock.getsockname() if self.sock else (self.bind, self.port)


def parse_line(data: bytes, source: str = "", received_at: float | None = None) -> Event:
    """Parse one datagram.

    Anything unrecognised still comes back as an Event with the raw text --
    a log feed that drops what it cannot classify is worse than useless when
    you are chasing something unusual.
    """
    text = data.decode("utf-8", "replace").replace("\x00", "").strip()
    event = Event(
        received_at=time.time() if received_at is None else received_at,
        source=source,
        raw=text,
        message=text,
    )

    priority = _PRI.match(text)
    if priority:
        value = int(priority.group(1))
        event.severity = value & 0x07
        event.facility = value >> 3
        event.severity_name = SEVERITIES[event.severity]
        event.message = priority.group(2).strip()

    category = _CATEGORY.match(event.message)
    if category:
        event.category = category.group(1).lower()

    service = _SP.search(event.message)
    if service:
        event.sp = int(service.group(1))

    # Caller ID first: it is a ringing event and it carries the caller, and
    # none of the generic needles match its wording.
    caller = _CID.search(event.message)
    if caller:
        event.call_event = "ringing"
        event.caller = caller.group(1).strip()
    else:
        lowered = event.message.lower()
        for needle, name in _CALL_EVENTS:
            if needle in lowered:
                event.call_event = name
                break

    number = _NUMBER.search(event.caller or event.message)
    if number:
        event.number = number.group(0)
    return event


def open_listener(bind: str = "0.0.0.0", port: int = 514, *, timeout: float = 1.0):
    """Bind a UDP socket for the device to send to.

    Port 514 is privileged on Unix, so an unprivileged run wants a high port
    and the device configured to match — which is why the port is settable on
    both ends rather than assumed.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(timeout)
    sock.bind((bind, port))
    return Listener(bind=bind, port=port, sock=sock)


def events(listener: Listener, *, count: int | None = None, deadline: float | None = None):
    """Yield events until ``count`` have arrived or ``deadline`` passes.

    The socket timeout makes this interruptible: without it, Ctrl-C during a
    blocking recv is not delivered until the next datagram, which on a quiet
    phone line can be a long time.
    """
    seen = 0
    while True:
        if count is not None and seen >= count:
            return
        if deadline is not None and time.time() >= deadline:
            return
        try:
            data, addr = listener.sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            return
        yield parse_line(data, source=addr[0])
        seen += 1


def local_address_for(host: str) -> str:
    """Which of our addresses the device would see us on.

    Connecting a UDP socket sends nothing; it just asks the routing table
    which source address would be used to reach that host. That beats
    guessing, on a machine with a VPN, a mesh interface and Wi-Fi all up.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((host, 9))
        return sock.getsockname()[0]
    except OSError:
        return "<this host's address>"
    finally:
        sock.close()


def setup_commands(address: str, port: int, level: int = 7) -> list:
    """The writes that would point the device at this listener.

    Returned rather than executed. Turning on a syslog feed changes device
    behaviour and sends call metadata across the network in the clear, so it
    is a decision for a person, taken once, not a side effect of running a
    listener.
    """
    return [
        f"obicfg set {SERVER_PARAM}={address}",
        f"obicfg set {PORT_PARAM}={port}",
        f"obicfg set {LEVEL_PARAM}={level}",
    ]
