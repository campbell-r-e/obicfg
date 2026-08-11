#!/usr/bin/env python3
"""obicaller — live caller ID and call events from an OBi ATA, over syslog.

An OBi will push a plain UDP syslog stream, and that stream carries call
setup, answer, teardown and registration changes *as they happen*. Everything
else these devices offer is polling: call history only appears once a call is
over, and a registration that flaps between two polls is invisible. This is
the only real-time interface the hardware has.

    ACKNOWLEDGEMENT
    ---------------
    The idea is not mine. It comes from **obicaller** by Ryan Young,
    https://github.com/YoRyan/obicaller — a public-domain talking caller-ID
    daemon for the OBi200 that announces incoming calls through espeak. That
    project is archived (November 2022) and is a shell script; no code from it
    is reused here. The insight worth crediting is that an otherwise
    unremarkable debug stream is the only live event source these devices
    have. This script is a Python re-take on that idea, adding relaying,
    structured output and an exec hook. The name is kept in tribute.

Standard library only, Python 3.6+, so it runs on whatever is already on the
box that can hear the device — in the case it was written for, an Alpine
netbook running an Asterisk PBX.

Typical use
-----------

    # watch calls as they happen
    obicaller.py --port 5515

    # announce the caller aloud, and relay everything to a collector that is
    # already listening on 5514 so both consumers get the stream
    obicaller.py --port 5515 --say --forward 127.0.0.1:5514

    # run a command on each call event; the event is in the environment
    obicaller.py --port 5515 --exec '/usr/local/bin/notify-call'

Point the device at it under System Management -> Device Admin -> Syslog:
set Server to this host, Port to the port below, and Level to 7. With obicfg:

    obicfg set admin.Server=<this host> admin.Port=5515 admin.Level=7

This script never talks to the device. It only listens.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time

__version__ = "1.0.0"

SEVERITIES = (
    "emergency", "alert", "critical", "error",
    "warning", "notice", "info", "debug",
)

_PRI = re.compile(r"^<(\d+)>\s*(.*)$", re.S)
# CATEGORY:message. Requiring the colon stops the first word of an
# ordinary sentence being promoted to a category.
_CATEGORY = re.compile(r"^([A-Za-z0-9_]+)\s*:")
_SP = re.compile(r"\bSP([1-4])\b")
# A dialled or presented number: at least three of digits/*/# , optionally +.
_NUMBER = re.compile(r"\+?[0-9*#]{3,}")

#: Checked in order, so anything containing a shorter needle comes first:
#: "Unregistered" contains "registered", and calling a service drop a
#: successful registration is exactly backwards.
CALL_EVENTS = (
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

#: Events worth interrupting a human for.
ANNOUNCE = ("ringing",)


def parse(data, source="", now=None):
    """Parse one datagram into a dict.

    Unrecognised lines still come back, with `raw` intact. A feed that drops
    what it cannot classify is worst exactly when something unusual is
    happening, which is when you are reading it.
    """
    text = data.decode("utf-8", "replace").replace("\x00", "").strip()
    event = {
        "at": time.time() if now is None else now,
        "source": source,
        "raw": text,
        "message": text,
        "severity": None,
        "severity_name": None,
        "facility": None,
        "category": "system",
        "sp": None,
        "call_event": None,
        "number": None,
    }

    priority = _PRI.match(text)
    if priority:
        value = int(priority.group(1))
        event["severity"] = value & 0x07
        event["facility"] = value >> 3
        event["severity_name"] = SEVERITIES[value & 0x07]
        event["message"] = priority.group(2).strip()

    category = _CATEGORY.match(event["message"])
    if category:
        event["category"] = category.group(1).lower()

    service = _SP.search(event["message"])
    if service:
        event["sp"] = int(service.group(1))

    lowered = event["message"].lower()
    for needle, name in CALL_EVENTS:
        if needle in lowered:
            event["call_event"] = name
            break

    number = _NUMBER.search(event["message"])
    if number:
        event["number"] = number.group(0)
    return event


def spoken(number):
    """Digits read one at a time, which is the only way they are intelligible."""
    if not number:
        return "an unknown caller"
    return " ".join(number.replace("+", ""))


def announce(event, voice="en-us"):
    """Say the caller aloud, if a speech engine is installed."""
    engine = shutil.which("espeak-ng") or shutil.which("espeak")
    if not engine:
        return False
    text = "Call from %s" % spoken(event.get("number"))
    try:
        subprocess.Popen(
            [engine, "-v", voice, text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def run_hook(command, event):
    """Run a command per event, passing it through the environment.

    Fire and forget on purpose: a slow or wedged hook must not stall the
    listener and lose the next datagram.
    """
    environment = dict(os.environ)
    for key, value in event.items():
        environment["OBI_" + key.upper()] = "" if value is None else str(value)
    try:
        subprocess.Popen(
            command,
            shell=True,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        print("hook failed: %s" % exc, file=sys.stderr)


def parse_target(text):
    host, _, port = text.rpartition(":")
    if not host:
        raise ValueError("expected host:port, got %r" % text)
    return (host, int(port))


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="obicaller",
        description=__doc__.split("ACKNOWLEDGEMENT")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Idea credit: obicaller by Ryan Young "
               "(https://github.com/YoRyan/obicaller), public domain.",
    )
    parser.add_argument("--version", action="version", version="obicaller " + __version__)
    parser.add_argument("--bind", default="0.0.0.0", help="address to listen on")
    parser.add_argument("--port", type=int, default=5515,
                        help="UDP port to listen on (default 5515; 514 needs root)")
    parser.add_argument("--json", action="store_true",
                        help="one JSON object per line, for piping")
    parser.add_argument("--calls-only", action="store_true",
                        help="only lines that mark a point in a call's life")
    parser.add_argument("--say", action="store_true",
                        help="announce incoming callers aloud via espeak")
    parser.add_argument("--voice", default="en-us", help="espeak voice for --say")
    parser.add_argument("--forward", action="append", default=[], metavar="HOST:PORT",
                        help="relay every datagram here too; repeatable. Use this "
                             "when something else already wants the stream -- the "
                             "device can only send to one address")
    parser.add_argument("--exec", dest="hook", metavar="CMD",
                        help="run CMD per event, with OBI_* set in its environment")
    parser.add_argument("--log", metavar="FILE", help="append output to FILE as well")
    parser.add_argument("--count", type=int, help="stop after N datagrams")
    args = parser.parse_args(argv)

    try:
        targets = [parse_target(t) for t in args.forward]
    except ValueError as exc:
        parser.error(str(exc))

    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.settimeout(1.0)
    try:
        listener.bind((args.bind, args.port))
    except OSError as exc:
        hint = ""
        if args.port < 1024:
            hint = " (ports below 1024 need root; try --port 5515)"
        print("cannot listen on %s:%d: %s%s" % (args.bind, args.port, exc, hint),
              file=sys.stderr)
        return 1

    relay = socket.socket(socket.AF_INET, socket.SOCK_DGRAM) if targets else None
    handle = open(args.log, "a", buffering=1) if args.log else None

    if not args.json:
        print("obicaller %s listening on %s:%d" % (__version__, args.bind, args.port))
        if targets:
            print("relaying to " + ", ".join("%s:%d" % t for t in targets))

    seen = 0
    try:
        while args.count is None or seen < args.count:
            try:
                data, addr = listener.recvfrom(65535)
            except socket.timeout:
                continue
            seen += 1

            # Relay first, and never let a relay failure cost us the event:
            # the other consumer being down is not our problem to escalate.
            for target in targets:
                try:
                    relay.sendto(data, target)
                except OSError:
                    pass

            event = parse(data, source=addr[0])
            if args.calls_only and not event["call_event"]:
                continue

            if args.json:
                line = json.dumps(event, sort_keys=True)
            else:
                stamp = time.strftime("%H:%M:%S", time.localtime(event["at"]))
                where = "SP%d" % event["sp"] if event["sp"] else event["category"]
                marker = "[%s] " % event["call_event"] if event["call_event"] else ""
                line = "%s %-8s %s%s" % (stamp, where, marker, event["message"])
            print(line)
            sys.stdout.flush()
            if handle:
                handle.write(line + "\n")

            if args.say and event["call_event"] in ANNOUNCE:
                announce(event, args.voice)
            if args.hook and event["call_event"]:
                run_hook(args.hook, event)
    except KeyboardInterrupt:
        pass
    finally:
        listener.close()
        if relay:
            relay.close()
        if handle:
            handle.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
