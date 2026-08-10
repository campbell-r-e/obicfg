"""Reading operational state off the device: status, line, RTP quality, calls.

Configuration is what the device *should* do; telemetry is what it is actually
doing. Five endpoints carry effectively all of it:

===========================  =========================================
``DI_S_.xml``                model, firmware, uptime, per-service registration
``DI_NS_.xml``               WAN addressing and the device's own clock
``PI_FXS_1_Stats.xml``       the analogue line: hook state, loop current, VBAT
``VS_1_VP_1_L_1_Stats.xml``  RTP counters per service, including packet loss
``callhistory.xml``          structured call detail records
===========================  =========================================

``callhistory.xml`` is worth calling out: it is **not listed in the admin
menu**, so nothing that discovers pages by crawling the UI will ever find it.
It is also the only one of the five that is not in the usual
model/object/parameter shape -- it has a schema of its own, closer to a log
than to a settings page.

Every value here is read-only. Nothing in this module writes.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

from .model import parse_objects

ENDPOINT_STATUS = "DI_S_.xml"
ENDPOINT_NETWORK = "DI_NS_.xml"
ENDPOINT_PORT = "PI_FXS_1_Stats.xml"
ENDPOINT_QUALITY = "VS_1_VP_1_L_1_Stats.xml"
ENDPOINT_CALLS = "callhistory.xml"

ENDPOINTS = (
    ENDPOINT_STATUS,
    ENDPOINT_NETWORK,
    ENDPOINT_PORT,
    ENDPOINT_QUALITY,
    ENDPOINT_CALLS,
)

# " 2 Days 17:07:14    (4)" -- the trailing count is reboots since power-on.
_UPTIME = re.compile(
    r"(?:(?P<days>\d+)\s*Days?\s*)?(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2})"
    r"(?:\s*\((?P<reboots>\d+)\))?",
    re.IGNORECASE,
)
# "From SP1(+15551234567)" / "To PH(1001)"
_TERMINAL = re.compile(r"(?:From|To)\s+([A-Za-z0-9]+)\s*\(([^)]*)\)")
_LEADING_INT = re.compile(r"\s*(-?\d+)")


def _int(text: str | None) -> int | None:
    if not text:
        return None
    match = _LEADING_INT.match(text)
    return int(match.group(1)) if match else None


def _float(text: str | None) -> float | None:
    if not text:
        return None
    match = re.match(r"\s*(-?\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def parse_uptime(text: str | None) -> tuple[int | None, int | None]:
    """``" 2 Days 17:07:14    (4)"`` -> ``(233234, 4)`` seconds and reboots."""
    if not text:
        return None, None
    match = _UPTIME.search(text)
    if not match:
        return None, None
    seconds = (
        int(match.group("days") or 0) * 86400
        + int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
    )
    reboots = match.group("reboots")
    return seconds, int(reboots) if reboots else None


# -- models ---------------------------------------------------------------


@dataclass
class ServiceStatus:
    """One SP service's registration state and call count."""

    sp: int
    status: str
    call_state: str
    active_calls: int

    @property
    def healthy(self) -> bool:
        """Is this service usable right now?

        "Registration Not Required" is a *healthy* state, not an error: it is
        what an IP-authenticated trunk reports when it has no registrar to
        talk to. "Service Not Configured" usually means the URI is empty --
        that field is what brings a registration-less service up.
        """
        lowered = self.status.lower()
        return "connected" in lowered or "not required" in lowered


@dataclass
class DeviceStatus:
    model: str | None = None
    firmware: str | None = None
    hardware: str | None = None
    serial: str | None = None
    mac: str | None = None
    uptime_s: int | None = None
    reboots: int | None = None
    system_time: str | None = None
    wan_ip: str | None = None
    obitalk_status: str | None = None
    services: list = field(default_factory=list)

    @property
    def active_calls(self) -> int:
        return sum(s.active_calls for s in self.services)


@dataclass
class PortStatus:
    """The analogue PHONE port's electrical and hook state."""

    state: str | None = None
    loop_current_ma: float | None = None
    vbat_v: float | None = None
    tipring_v: float | None = None
    last_caller: str | None = None

    @property
    def off_hook(self) -> bool:
        return bool(self.state) and "off" in self.state.lower()


@dataclass
class LineQuality:
    """RTP counters for one service, cumulative since the last reset."""

    sp: int
    packets_sent: int = 0
    packets_received: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    packets_lost: int = 0
    overruns: int = 0
    underruns: int = 0

    @property
    def loss_percent(self) -> float | None:
        """Loss as a share of what should have arrived.

        Lost packets are counted against *received*, not sent: the sent
        counter is this device's own outbound stream, which it cannot lose.
        """
        expected = self.packets_received + self.packets_lost
        if expected <= 0:
            return None
        return 100.0 * self.packets_lost / expected


@dataclass
class CallEvent:
    time: str
    text: str


@dataclass
class CallRecord:
    """One call, assembled from the event log the device keeps per call."""

    date: str
    time: str
    direction: str = "unknown"
    peer: str | None = None
    from_terminal: str | None = None
    to_terminal: str | None = None
    connected: bool = False
    ring_s: int | None = None
    talk_s: int | None = None
    events: list = field(default_factory=list)

    @property
    def answered(self) -> bool:
        return self.connected


@dataclass
class Telemetry:
    device: DeviceStatus
    port: PortStatus
    quality: list
    calls: list
    errors: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "device": asdict(self.device),
            "port": asdict(self.port),
            "quality": [
                dict(asdict(q), loss_percent=q.loss_percent) for q in self.quality
            ],
            "calls": [asdict(c) for c in self.calls],
            "errors": self.errors,
        }


# -- parsers ---------------------------------------------------------------


def parse_status(xml: bytes | str) -> DeviceStatus:
    objects = parse_objects(xml)
    flat = {(name, key): value for name, entries in objects for key, value in entries.items()}

    def find(object_name: str, key: str) -> str | None:
        return flat.get((object_name, key))

    uptime_s, reboots = parse_uptime(find("Product Information", "UpTime"))
    services = []
    for index in range(1, 5):
        name = f"SP{index} Service Status"
        status = find(name, "Status")
        if status is None:
            continue
        call_state = find(name, "CallState") or ""
        services.append(
            ServiceStatus(
                sp=index,
                status=status.strip(),
                call_state=call_state.strip(),
                active_calls=_int(call_state) or 0,
            )
        )
    return DeviceStatus(
        model=find("Product Information", "ModelName"),
        firmware=find("Product Information", "SoftwareVersion"),
        hardware=find("Product Information", "HardwareVersion"),
        serial=find("Product Information", "SerialNumber"),
        mac=find("Product Information", "MACAddress") or find("WAN Status", "MACAddress"),
        uptime_s=uptime_s,
        reboots=reboots,
        system_time=(find("Product Information", "SystemTime") or "").strip() or None,
        wan_ip=find("WAN Status", "IPAddress"),
        obitalk_status=find("OBiTALK Service Status", "Status"),
        services=services,
    )


def parse_port(xml: bytes | str) -> PortStatus:
    entries: dict[str, str] = {}
    for _, values in parse_objects(xml):
        entries.update(values)
    return PortStatus(
        state=(entries.get("State") or "").strip() or None,
        loop_current_ma=_float(entries.get("LoopCurrent")),
        vbat_v=_float(entries.get("VBAT")),
        tipring_v=_float(entries.get("TipRingVoltage")),
        last_caller=(entries.get("LastCallerInfo") or "").strip() or None,
    )


def parse_quality(xml: bytes | str) -> list:
    """One :class:`LineQuality` per ``RTP Statistics`` block, in SP order."""
    out = []
    blocks = [values for name, values in parse_objects(xml) if name == "RTP Statistics"]
    for index, values in enumerate(blocks, start=1):
        out.append(
            LineQuality(
                sp=index,
                packets_sent=_int(values.get("PacketsSent")) or 0,
                packets_received=_int(values.get("PacketsReceived")) or 0,
                bytes_sent=_int(values.get("BytesSent")) or 0,
                bytes_received=_int(values.get("BytesReceived")) or 0,
                packets_lost=_int(values.get("PacketsLost")) or 0,
                overruns=_int(values.get("Overruns")) or 0,
                underruns=_int(values.get("Underruns")) or 0,
            )
        )
    return out


def _seconds(clock: str) -> int | None:
    parts = clock.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(p) for p in parts)
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _elapsed(start: str, end: str) -> int | None:
    """Seconds between two wall-clock stamps, tolerating a midnight rollover.

    The device stamps events with a time and the call with a date, so a call
    that spans midnight would otherwise come out negative.
    """
    first, second = _seconds(start), _seconds(end)
    if first is None or second is None:
        return None
    delta = second - first
    return delta + 86400 if delta < 0 else delta


def parse_calls(xml: bytes | str, *, limit: int | None = None) -> list:
    """Parse ``callhistory.xml`` into call records, most recent first.

    Each ``<CallHistory>`` holds a small event log rather than fields: the
    first event names the two terminals, a later one says the call connected,
    a last one says it ended.  Ring and talk time are the gaps between them.
    """
    root = ET.fromstring(xml)
    calls = []
    for entry in root.findall("CallHistory"):
        record = CallRecord(date=entry.get("date", ""), time=entry.get("time", ""))
        start = record.time
        connected_at = None
        ended_at = None

        for event in entry.findall("Event"):
            stamp = event.get("time", "")
            for line in event:
                text = (line.text or "").strip()
                if not text:
                    continue
                record.events.append(CallEvent(time=stamp, text=text))
                terminal = _TERMINAL.match(text)
                if terminal and text.lower().startswith("from"):
                    record.from_terminal = terminal.group(1)
                    record.peer = terminal.group(2) or record.peer
                elif terminal and text.lower().startswith("to"):
                    record.to_terminal = terminal.group(1)
                    if not record.peer:
                        record.peer = terminal.group(2)
                elif "connected" in text.lower():
                    record.connected = True
                    connected_at = connected_at or stamp
                elif "ended" in text.lower():
                    ended_at = stamp

        # A call *from* the analogue port is one the handset placed.
        if record.from_terminal:
            record.direction = (
                "outbound" if record.from_terminal.upper().startswith("PH") else "inbound"
            )
        if connected_at:
            record.ring_s = _elapsed(start, connected_at)
            if ended_at:
                record.talk_s = _elapsed(connected_at, ended_at)
        elif ended_at:
            record.ring_s = _elapsed(start, ended_at)

        calls.append(record)
        if limit is not None and len(calls) >= limit:
            break
    return calls


# -- redaction -------------------------------------------------------------

_DIGITS = re.compile(r"\d")


def redact_number(number: str | None) -> str | None:
    """Mask all but the last two digits of a phone number.

    Call history is the one part of an OBi's state that is personal data, so
    it gets the same treatment as credentials in a config dump.
    """
    if not number:
        return number
    digits = _DIGITS.findall(number)
    if len(digits) <= 2:
        return number
    keep = 0
    out = []
    total = len(digits)
    for char in number:
        if char.isdigit():
            keep += 1
            out.append(char if keep > total - 2 else "x")
        else:
            out.append(char)
    return "".join(out)


def redact(telemetry: Telemetry) -> Telemetry:
    """Mask caller numbers in place and return the same object."""
    telemetry.port.last_caller = redact_number(telemetry.port.last_caller)
    for call in telemetry.calls:
        call.peer = redact_number(call.peer)
        for event in call.events:
            event.text = _TERMINAL.sub(
                lambda m: f"{m.group(0).split()[0]} {m.group(1)}({redact_number(m.group(2))})",
                event.text,
            )
    return telemetry


# -- collection ------------------------------------------------------------


def read(device, *, calls: int | None = 20, include_calls: bool = True) -> Telemetry:
    """Fetch and parse everything, tolerating endpoints a model does not have.

    A missing endpoint is recorded in ``errors`` rather than raised: an OBi100
    has no Bluetooth status and some firmware lacks call history, and partial
    telemetry beats none.
    """
    from .errors import ObiError

    errors: dict = {}

    def grab(endpoint: str, parser, default):
        try:
            return parser(device.client.fetch(endpoint))
        except (ObiError, ET.ParseError, KeyError) as exc:
            errors[endpoint] = str(exc)
            return default

    status = grab(ENDPOINT_STATUS, parse_status, DeviceStatus())
    port = grab(ENDPOINT_PORT, parse_port, PortStatus())
    quality = grab(ENDPOINT_QUALITY, parse_quality, [])
    history = (
        grab(ENDPOINT_CALLS, lambda x: parse_calls(x, limit=calls), [])
        if include_calls
        else []
    )
    return Telemetry(
        device=status, port=port, quality=quality, calls=history, errors=errors
    )
