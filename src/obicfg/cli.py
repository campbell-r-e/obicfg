"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, config as config_mod, telemetry
from .client import Client, Transport
from .device import Device, count_redacted, diff_snapshots
from .errors import GuardError, ObiError, UsageError, VerificationError
from .guard import Guard
from .model import Page
from .naming import aliases_for
from .profile import check_requirements, load as load_profile

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_VERIFY = 3
EXIT_GUARD = 4


# ---------------------------------------------------------------- helpers


def _out(text: str = "") -> None:
    print(text)


def _table(rows: list[list[str]], headers: list[str]) -> str:
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


def _shorten(text: str, limit: int = 60) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _read_value(raw: str) -> str:
    """Support ``path=@file`` so huge DigitMap/CallRoute values dodge the shell."""
    if raw.startswith("@"):
        return Path(raw[1:]).read_text(encoding="utf-8").strip()
    return raw


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise ObiError(
            f"{prompt} -- refusing to assume an answer on a non-interactive "
            "run. Get the human's approval for this specific action, then "
            "re-run it with --yes."
        )
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def _device(args: argparse.Namespace) -> Device:
    config = config_mod.load_config()
    host = config_mod.resolve("host", args.host, config)
    if not host:
        raise ObiError(
            "no device given: pass --host, set OBI_HOST, or add host = \"...\" "
            f"to {config_mod.config_path()}"
        )
    transport_name = str(
        config_mod.resolve("transport", args.transport, config, "paramlist")
    )
    try:
        transport = Transport(transport_name)
    except ValueError:
        raise ObiError(
            f"unknown transport {transport_name!r}; expected 'paramlist' or 'query'"
        ) from None
    client = Client(
        host=str(host),
        username=str(config_mod.resolve("username", args.user, config, "admin")),
        password=config_mod.read_password(args.password, args.password_file, config),
        scheme=str(config_mod.resolve("scheme", args.scheme, config, "http")),
        port=args.port,
        timeout=float(config_mod.resolve("timeout", args.timeout, config, 15.0)),
        transport=transport,
    )
    guard = Guard.from_config(config.get("guard", {}), enabled=not args.unsafe)
    return Device(client, guard)


# --------------------------------------------------------------- commands


def cmd_pages(args: argparse.Namespace) -> int:
    device = _device(args)
    entries = []
    for filename, label in device.menu():
        name = filename[: -len(".xml")]
        entry = {"page": name, "label": label, "aliases": aliases_for(name)}
        if args.titles:
            # Menu labels repeat ("General", "SIP") because the menu is a
            # tree; the page's own title is unique but costs a fetch each.
            entry["title"] = device.page(name).title
        entries.append(entry)
    if args.json:
        _out(json.dumps(entries, indent=2))
        return EXIT_OK
    headers = ["PAGE", "MENU LABEL", "ALIASES"]
    rows = [[e["page"], e["label"], ", ".join(e["aliases"])] for e in entries]
    if args.titles:
        headers.insert(2, "TITLE")
        for row, entry in zip(rows, entries):
            row.insert(2, entry["title"])
    _out(_table(rows, headers))
    return EXIT_OK


def _page_rows(page: Page, changed_only: bool) -> list[list[str]]:
    rows = []
    for parameter in page.parameters:
        if changed_only and parameter.is_default:
            continue
        rows.append(
            [
                parameter.name,
                _shorten(parameter.value, 44),
                "default" if parameter.is_default else "set",
                parameter.hash,
                "ro" if not parameter.writable else "",
            ]
        )
    return rows


def cmd_show(args: argparse.Namespace) -> int:
    device = _device(args)
    page = device.page(args.page)
    if args.json:
        _out(
            json.dumps(
                {
                    "page": page.name,
                    "title": page.title,
                    "reboot_required": page.reboot_required,
                    "parameters": [
                        {
                            "name": p.name,
                            "path": p.path,
                            "hash": p.hash,
                            "value": p.value,
                            "default": p.default,
                            "is_default": p.is_default,
                            "writable": p.writable,
                            "type": p.syntax.kind,
                            "options": list(p.syntax.enumeration),
                            # The device's own declared limits. Worth having in
                            # the output: after a rejected write these are what
                            # you check the value against.
                            "max_length": p.syntax.max_length,
                            "min": p.syntax.min_inclusive,
                            "max": p.syntax.max_inclusive,
                            "description": p.description,
                        }
                        for p in page.parameters
                        if not (args.changed and p.is_default)
                    ],
                },
                indent=2,
            )
        )
        return EXIT_OK
    _out(f"{page.name}  —  {page.title}")
    if page.reboot_required:
        _out("changes on this page require a reboot")
    _out()
    rows = _page_rows(page, args.changed)
    if not rows:
        _out("(every parameter on this page is still at its factory default)")
        return EXIT_OK
    _out(_table(rows, ["PARAMETER", "VALUE", "STATE", "HASH", ""]))
    return EXIT_OK


def cmd_search(args: argparse.Namespace) -> int:
    device = _device(args)
    found = list(device.search(args.pattern))
    if args.json:
        _out(
            json.dumps(
                [
                    {
                        "path": p.path,
                        "hash": p.hash,
                        "value": p.value,
                        "is_default": p.is_default,
                        "description": p.description,
                    }
                    for p in found
                ],
                indent=2,
            )
        )
        # Same exit code as the text form: no match is a result, not a crash,
        # but callers branch on it.
        return EXIT_OK if found else EXIT_ERROR
    if not found:
        _out(f"nothing matches {args.pattern!r}")
        return EXIT_ERROR
    _out(
        _table(
            [[p.path, _shorten(p.value, 34), _shorten(p.description, 46)] for p in found],
            ["PATH", "VALUE", "DESCRIPTION"],
        )
    )
    return EXIT_OK


def cmd_get(args: argparse.Namespace) -> int:
    device = _device(args)
    values = {}
    for path in args.paths:
        parameter = device.parameter(path)
        values[parameter.path] = parameter.value
        if not args.json and len(args.paths) > 1:
            _out(f"{parameter.path} = {parameter.value}")
        elif not args.json:
            _out(parameter.value)
    if args.json:
        _out(json.dumps(values, indent=2))
    return EXIT_OK


def _print_plan(changes) -> None:
    for change in changes:
        if change.noop:
            _out(f"  = {change.path} already {change.new!r}")
        else:
            _out(f"  ~ {change.path}: {change.old!r} -> {change.new!r}")


def _changes_json(changes, *, dry_run: bool = False, verified: bool = True) -> dict:
    """Machine-readable result of a write, for scripts and agents.

    Reports what was planned, what was sent, and — separately — what the
    device confirmed. The distinction matters: `applied` only means the
    request went out, and on an OBi that says nothing about whether it took.
    """
    return {
        "dry_run": dry_run,
        "verified": verified,
        "plan": [
            {"path": c.path, "old": c.old, "new": c.new, "noop": c.noop}
            for c in changes
        ],
        "applied": [
            {
                "path": c.path,
                "value": c.new,
                "verified": c.verified,
                "observed": c.observed,
            }
            for c in changes
            if c.applied
        ],
        "failures": [
            {"path": c.path, "wanted": c.new, "observed": c.observed}
            for c in changes
            if c.applied and not c.verified
        ],
    }


def cmd_set(args: argparse.Namespace) -> int:
    device = _device(args)
    assignments = []
    for item in args.assignments:
        if "=" not in item:
            raise UsageError(
                f"{item!r} is not a path=value pair -- expected "
                "<page>.<parameter>=<value>, e.g. sp2.URI=200"
            )
        path, _, value = item.partition("=")
        assignments.append((path.strip(), _read_value(value)))

    changes = device.plan(assignments)
    pending = [c for c in changes if not c.noop]
    if args.dry_run or not pending:
        if args.json:
            _out(json.dumps(_changes_json(changes, dry_run=True), indent=2))
        else:
            _out("plan:")
            _print_plan(changes)
            _out(
                "nothing to do"
                if not pending
                else f"dry run: {len(pending)} change(s) not sent"
            )
        return EXIT_OK
    if not args.json:
        _out("plan:")
        _print_plan(changes)

    device.apply(changes, verify=not args.no_verify)
    failed = bool(device.verify_failures(changes)) and not args.no_verify

    if args.json:
        _out(json.dumps(_changes_json(changes, verified=not args.no_verify), indent=2))
    else:
        _out()
        for change in pending:
            if args.no_verify:
                _out(f"  sent {change.path}")
            elif change.verified:
                _out(f"  ok   {change.path} = {change.new!r}")
            else:
                _out(f"  FAIL {change.path}: device reports {change.observed!r}")
        if failed:
            _out()
            _out(
                "the device returned HTTP 200 but did not apply the writes above. "
                "That is normal OBi behaviour for a rejected value -- check the "
                "syntax, or try --transport query."
            )
    if failed:
        # Do not reboot on top of a failed write; leave the device as found.
        return EXIT_VERIFY
    if args.reboot:
        _out("rebooting...")
        device.reboot(wait=True)
        _out("back up")
    return EXIT_OK


def cmd_unset(args: argparse.Namespace) -> int:
    device = _device(args)
    # A reset discards whatever is there now, so it gets the same dry run a
    # write gets -- otherwise "put it back to default" is the one destructive
    # operation with no way to preview it.
    planned = device.plan_reset(args.paths)
    if args.dry_run:
        if args.json:
            _out(json.dumps(_changes_json(planned, dry_run=True), indent=2))
        else:
            _out("plan:")
            _print_plan(planned)
            _out(f"dry run: {len([c for c in planned if not c.noop])} reset(s) not sent")
        return EXIT_OK
    if not args.json:
        _out("plan:")
        _print_plan(planned)
    if not _confirm(
        f"restore {len(args.paths)} parameter(s) to factory default?", args.yes
    ):
        _out("aborted")
        return EXIT_OK
    changes = device.reset_to_default(args.paths)
    if args.json:
        _out(json.dumps(_changes_json(changes), indent=2))
    else:
        for change in changes:
            if change.noop:
                _out(f"  = {change.path} was already at its default")
            elif change.verified:
                _out(f"  ok   {change.path} -> {change.new!r}")
            else:
                _out(f"  FAIL {change.path}: device reports {change.observed!r}")
    return EXIT_VERIFY if device.verify_failures(changes) else EXIT_OK


def cmd_dump(args: argparse.Namespace) -> int:
    device = _device(args)
    directory = Path(args.directory)
    # A backup that overwrites the previous backup is not a backup. Two runs
    # on the same day would otherwise put post-change state on top of the only
    # restore point, silently.
    if (directory / "snapshot.json").exists() and not args.force:
        raise ObiError(
            f"{directory / 'snapshot.json'} already exists -- refusing to "
            "overwrite an existing restore point. Choose another directory, "
            "or pass --force if you meant to replace it."
        )
    directory.mkdir(parents=True, exist_ok=True)

    identity = device.identity()
    names = device.page_names() if args.include_status else device.config_pages()
    if args.raw:
        for name in names:
            (directory / f"{name}.xml").write_bytes(device.client.fetch(f"{name}.xml"))

    snapshot = device.snapshot(
        include_status=args.include_status,
        redact=args.redact,
        writable_only=not args.all,
    )
    snapshot["meta"] = {
        "taken": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tool": f"obicfg {__version__}",
        "identity": {
            k: ("<redacted>" if args.redact and k in ("MACAddress", "SerialNumber") else v)
            for k, v in identity.items()
        },
    }
    (directory / "snapshot.json").write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    count = sum(len(p["parameters"]) for p in snapshot["pages"].values())
    _out(f"wrote {len(snapshot['pages'])} pages, {count} parameters to {directory}")
    if not args.redact:
        _out(
            "note: this dump contains the device's MAC, serial and any stored "
            "credentials -- keep it out of public repositories, or use --redact"
        )
    return EXIT_OK


def _load_snapshot(path: str) -> dict:
    target = Path(path)
    if target.is_dir():
        target = target / "snapshot.json"
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ObiError(f"cannot read snapshot {target}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise ObiError(f"{target} is not valid JSON: {exc}") from None


def cmd_diff(args: argparse.Namespace) -> int:
    before = _load_snapshot(args.snapshot)
    if args.against:
        after = _load_snapshot(args.against)
        after_label = args.against
    else:
        after = _device(args).snapshot()
        after_label = "device"
    differences = diff_snapshots(before, after)
    hidden = count_redacted(before) + count_redacted(after)
    if hidden and not args.json:
        _out(f"note: {hidden} redacted value(s) cannot be compared and were skipped")
    if args.json:
        _out(
            json.dumps(
                [{"path": p, "before": b, "after": a} for p, b, a in differences],
                indent=2,
            )
        )
        return EXIT_OK if not differences else EXIT_ERROR
    if not differences:
        _out(f"no differences between {args.snapshot} and {after_label}")
        return EXIT_OK
    _out(f"{args.snapshot} -> {after_label}")
    for path, old, new in differences:
        _out(f"  ~ {path}")
        _out(f"      - {old!r}")
        _out(f"      + {new!r}")
    _out()
    _out(f"{len(differences)} difference(s)")
    return EXIT_ERROR


def cmd_apply(args: argparse.Namespace) -> int:
    device = _device(args)
    profile = load_profile(args.profile)
    if profile.require:
        check_requirements(profile, device.identity())

    _out(f"profile: {profile.name}")
    if profile.description:
        _out(profile.description)
    _out()

    changes = device.plan(profile.assignments)
    pending = [c for c in changes if not c.noop]
    _out("plan:")
    _print_plan(changes)
    for path in profile.reset:
        _out(f"  ! {path} -> factory default")
    if not pending and not profile.reset:
        _out("device already matches this profile")
        return EXIT_OK
    if args.dry_run:
        _out()
        _out(f"dry run: {len(pending)} change(s) not sent")
        return EXIT_OK
    if not _confirm(f"apply {len(pending)} change(s)?", args.yes):
        _out("aborted")
        return EXIT_OK

    device.apply(changes)
    reset_changes = device.reset_to_default(profile.reset) if profile.reset else []
    _out()
    for change in pending + [c for c in reset_changes if not c.noop]:
        marker = "ok  " if change.verified else "FAIL"
        _out(f"  {marker} {change.path} = {change.new!r}")
    failures = device.verify_failures(changes) + device.verify_failures(reset_changes)
    if failures:
        return EXIT_VERIFY

    should_reboot = profile.reboot if args.reboot is None else args.reboot
    if should_reboot:
        _out("rebooting...")
        _out("back up" if device.reboot(wait=True) else "still down; check manually")
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    device = _device(args)
    identity = device.identity()
    services = []
    for index, page_name in enumerate(
        ("VS_1_VP_1_L_1_", "VS_1_VP_1_L_2_", "VS_1_VP_1_L_3_", "VS_1_VP_1_L_4_"), 1
    ):
        try:
            page = device.page(page_name)
        except ObiError:
            continue
        enable = page.get("Enable")
        services.append(
            {
                "service": f"SP{index}",
                "enabled": enable.value if enable else "?",
                "profile": (page.get("X_ServProvProfile").value if page.get("X_ServProvProfile") else "?"),
                "inbound": (page.get("X_InboundCallRoute").value if page.get("X_InboundCallRoute") else ""),
            }
        )
    if args.json:
        _out(json.dumps({"identity": identity, "services": services}, indent=2))
        return EXIT_OK
    for key, value in identity.items():
        _out(f"{key:16} {value}")
    _out()
    _out(
        _table(
            [
                [s["service"], s["enabled"], s["profile"], _shorten(s["inbound"], 40)]
                for s in services
            ],
            ["SERVICE", "ENABLED", "ITSP", "INBOUND ROUTE"],
        )
    )
    return EXIT_OK


def _format_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return f"{days}d {hours}h {minutes}m" if days else f"{hours}h {minutes}m"


def _print_telemetry(reading) -> None:
    device = reading.device
    _out(
        f"{device.model or '?'}  fw {device.firmware or '?'}  "
        f"up {_format_uptime(device.uptime_s)}"
        + (f"  ({device.reboots} reboots)" if device.reboots is not None else "")
    )
    if device.system_time:
        _out(f"device clock: {device.system_time}")
    _out()

    quality = {q.sp: q for q in reading.quality}
    rows = []
    for service in device.services:
        stats = quality.get(service.sp)
        loss = "" if stats is None or stats.loss_percent is None else f"{stats.loss_percent:.2f}%"
        rows.append(
            [
                f"SP{service.sp}",
                _shorten(service.status, 34),
                str(service.active_calls),
                str(stats.packets_sent) if stats else "",
                str(stats.packets_received) if stats else "",
                str(stats.packets_lost) if stats else "",
                loss,
                "" if service.healthy else "!",
            ]
        )
    if rows:
        _out(_table(rows, ["SVC", "STATUS", "CALLS", "TX PKTS", "RX PKTS", "LOST", "LOSS", ""]))
        _out()

    port = reading.port
    details = ", ".join(
        part
        for part in (
            port.state,
            None if port.loop_current_ma is None else f"loop {port.loop_current_ma:g} mA",
            None if port.vbat_v is None else f"VBAT {port.vbat_v:g} V",
            None if port.tipring_v is None else f"tip-ring {port.tipring_v:g} V",
            f"last caller {port.last_caller}" if port.last_caller else None,
        )
        if part
    )
    # Parenthesised deliberately: "phone port: " + details is always truthy,
    # so an `or` on the whole expression would never reach the fallback.
    _out("phone port: " + (details or "no data"))

    if reading.calls:
        _out()
        _out(
            _table(
                [
                    [
                        f"{c.date} {c.time}",
                        c.direction,
                        _shorten(c.peer or "", 22),
                        f"{c.from_terminal or '?'}->{c.to_terminal or '?'}",
                        "yes" if c.answered else "no",
                        "" if c.ring_s is None else f"{c.ring_s}s",
                        "" if c.talk_s is None else f"{c.talk_s}s",
                    ]
                    for c in reading.calls
                ],
                ["WHEN", "DIRECTION", "PEER", "PATH", "ANSWERED", "RING", "TALK"],
            )
        )
    for endpoint, problem in reading.errors.items():
        _out(f"note: {endpoint} unavailable ({problem})")


def cmd_telemetry(args: argparse.Namespace) -> int:
    device = _device(args)
    while True:
        reading = telemetry.read(
            device, calls=args.calls, include_calls=not args.no_calls
        )
        if args.redact:
            telemetry.redact(reading)
        if args.json:
            _out(json.dumps(reading.to_dict(), indent=2))
        else:
            _print_telemetry(reading)
        if not args.watch:
            return EXIT_OK
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            return EXIT_OK
        _out()
        _out("-" * 60)


def cmd_reboot(args: argparse.Namespace) -> int:
    device = _device(args)
    if not _confirm(f"reboot {device.client.host}?", args.yes):
        _out("aborted")
        return EXIT_OK
    ok = device.reboot(wait=args.wait)
    _out("back up" if (ok and args.wait) else "reboot requested")
    return EXIT_OK if ok else EXIT_ERROR


def cmd_probe(args: argparse.Namespace) -> int:
    """Empirically confirm how this firmware decodes writes.

    The claim that ``ParameterList`` handles spaces and ``#`` comes from
    reading the admin UI's own JavaScript.  This proves it on the unit in
    front of you, using a parameter you nominate, and puts it back afterwards.
    """
    device = _device(args)
    parameter = device.parameter(args.scratch)
    original = parameter.value
    if not parameter.writable:
        raise ObiError(f"{parameter.path} is read-only; pick a writable scratch parameter")
    _out(f"scratch parameter: {parameter.path} (currently {original!r})")
    if not _confirm("write test values to it and restore afterwards?", args.yes):
        _out("aborted")
        return EXIT_OK

    probes = [("plain", "obicfgtest"), ("space", "obicfg test"), ("hash", "obicfg#test")]
    results = []
    for label, value in probes:
        try:
            changes = device.apply([device.plan([(parameter.path, value)])[0]])
            ok = changes[0].verified
            observed = changes[0].observed
        except ObiError as exc:
            ok, observed = False, str(exc)
        results.append((label, value, ok, observed))
        _out(f"  {'ok  ' if ok else 'FAIL'} {label:6} {value!r} -> {observed!r}")

    if original == parameter.default:
        device.reset_to_default([parameter.path])
    else:
        device.apply(device.plan([(parameter.path, original)]))
    _out(f"restored {parameter.path} to {original!r}")

    if args.json:
        _out(
            json.dumps(
                [
                    {"case": c, "sent": v, "applied": ok, "observed": o}
                    for c, v, ok, o in results
                ],
                indent=2,
            )
        )
    return EXIT_OK if all(r[2] for r in results) else EXIT_VERIFY


# ---------------------------------------------------------------- parsing


def _add_global_args(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Add the connection and safety flags.

    Called twice: once on the top-level parser with real defaults, and once on
    a parent shared by every subcommand with ``SUPPRESS`` defaults.  The
    SUPPRESS half is what makes ``obicfg status --host X`` work as well as
    ``obicfg --host X status`` -- a subparser overwrites the whole namespace
    with its own results, so a suppressed default is the only way for a flag
    given before the subcommand to survive one that was not given after it.
    """

    def default(value):
        return argparse.SUPPRESS if suppress else value

    def helptext(text: str) -> str:
        return argparse.SUPPRESS if suppress else text

    connection = parser.add_argument_group("connection")
    connection.add_argument("-H", "--host", default=default(None),
                            help=helptext("device address (env: OBI_HOST)"))
    connection.add_argument("-u", "--user", default=default(None),
                            help=helptext("admin username (default: admin)"))
    connection.add_argument("-p", "--password", default=default(None),
                            help=helptext("admin password (prefer --password-file)"))
    connection.add_argument("--password-file", default=default(None),
                            help=helptext("file containing the admin password"))
    connection.add_argument("--port", type=int, default=default(None),
                            help=helptext("admin port, if not the default"))
    connection.add_argument("--scheme", choices=("http", "https"), default=default(None),
                            help=helptext("default: http"))
    connection.add_argument("--timeout", type=float, default=default(None),
                            help=helptext("per-request timeout in seconds"))
    connection.add_argument("--transport", choices=("paramlist", "query"), default=default(None),
                            help=helptext("how writes are encoded (default: paramlist)"))

    safety = parser.add_argument_group("safety")
    safety.add_argument(
        "--unsafe",
        action="store_true",
        default=default(False),
        help=helptext("disable write protection on provisioned/irreplaceable settings"),
    )
    safety.add_argument("-y", "--yes", action="store_true", default=default(False),
                        help=helptext("do not prompt"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obicfg",
        description="Configure an OBi200-family ATA from the command line.",
        epilog=(
            "Connection and safety flags may be given before or after the "
            "subcommand. Exit codes: 0 ok, 1 error, 2 usage, 3 write not "
            "applied, 4 blocked by guard."
        ),
    )
    parser.add_argument("--version", action="version", version=f"obicfg {__version__}")
    _add_global_args(parser, suppress=False)

    common = argparse.ArgumentParser(add_help=False)
    _add_global_args(common, suppress=True)

    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help_text: str, *, json_flag: bool = True):
        sub = subparsers.add_parser(
            name, help=help_text, description=help_text, parents=[common]
        )
        sub.set_defaults(func=func)
        if json_flag:
            sub.add_argument("--json", action="store_true", help="machine-readable output")
        return sub

    pages = add("pages", cmd_pages, "list the configuration pages this device exposes")
    pages.add_argument(
        "--titles",
        action="store_true",
        help="also show each page's own title (fetches every page)",
    )

    show = add("show", cmd_show, "list the parameters on one page")
    show.add_argument("page", help="page name or alias, e.g. sp2 or VS_1_VP_2_SIP_")
    show.add_argument("--changed", action="store_true", help="only non-default values")

    search = add("search", cmd_search, "find parameters by name or description")
    search.add_argument("pattern", help="regular expression")

    get = add("get", cmd_get, "print the current value of one or more parameters")
    get.add_argument("paths", nargs="+", metavar="PATH")

    set_ = add("set", cmd_set, "write parameters, then read them back to confirm")
    set_.add_argument("assignments", nargs="+", metavar="PATH=VALUE",
                      help="value may be @filename to read it from a file")
    set_.add_argument("-n", "--dry-run", action="store_true", help="show the plan only")
    set_.add_argument("--no-verify", action="store_true", help="skip the read-back (not advised)")
    set_.add_argument("--reboot", action="store_true", help="reboot afterwards and wait")

    unset = add("unset", cmd_unset, "restore parameters to their factory default")
    unset.add_argument("paths", nargs="+", metavar="PATH")
    unset.add_argument("-n", "--dry-run", action="store_true",
                       help="show what would be reset, and send nothing")

    dump = add("dump", cmd_dump, "back up the whole configuration to a directory", json_flag=False)
    dump.add_argument("directory")
    dump.add_argument("--include-status", action="store_true", help="also capture live status pages")
    dump.add_argument("--all", action="store_true",
                      help="include read-only readouts (noisy: some change every second)")
    dump.add_argument("--redact", action="store_true", help="blank passwords, MAC and serial")
    dump.add_argument("--force", action="store_true",
                      help="overwrite an existing backup in this directory")
    dump.add_argument("--no-raw", dest="raw", action="store_false", help="snapshot.json only")

    diff = add("diff", cmd_diff, "compare a snapshot against the device or another snapshot")
    diff.add_argument("snapshot", help="snapshot.json, or the directory holding it")
    diff.add_argument("--against", help="compare against this snapshot instead of the device")

    apply_ = add("apply", cmd_apply, "bring the device in line with a profile")
    apply_.add_argument("profile", help="a .toml profile")
    apply_.add_argument("-n", "--dry-run", action="store_true", help="show the plan only")
    reboot_group = apply_.add_mutually_exclusive_group()
    reboot_group.add_argument("--reboot", dest="reboot", action="store_true", default=None,
                              help="reboot afterwards, overriding the profile")
    reboot_group.add_argument("--no-reboot", dest="reboot", action="store_false",
                              help="never reboot, overriding the profile")

    add("status", cmd_status, "show device identity and per-service state")

    tel = add("telemetry", cmd_telemetry,
              "read live operational data: uptime, registration, RTP quality, "
              "line voltages and call history")
    tel.add_argument("--calls", type=int, default=20, metavar="N",
                     help="how many recent calls to include (default: 20)")
    tel.add_argument("--no-calls", action="store_true",
                     help="skip call history (it is the largest fetch by far)")
    tel.add_argument("--redact", action="store_true",
                     help="mask phone numbers, and the device serial and MAC")
    tel.add_argument("--watch", type=float, metavar="SECONDS",
                     help="repeat every SECONDS until interrupted")

    reboot = add("reboot", cmd_reboot, "reboot the device", json_flag=False)
    reboot.add_argument("--wait", action="store_true", help="block until it answers again")

    probe = add("probe", cmd_probe, "prove how this firmware decodes writes")
    probe.add_argument("--scratch", default="VS_1_VP_1_L_4_.CallerIDName",
                       help="a harmless writable parameter to test with (default: SP4's caller ID name)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except UsageError as exc:
        print(f"obicfg: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except GuardError as exc:
        print(f"obicfg: {exc}", file=sys.stderr)
        return EXIT_GUARD
    except VerificationError as exc:
        print(f"obicfg: {exc}", file=sys.stderr)
        return EXIT_VERIFY
    except ObiError as exc:
        print(f"obicfg: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("obicfg: interrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
