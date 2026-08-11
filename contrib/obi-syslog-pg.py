#!/usr/bin/env python3
"""Store OBi syslog events in PostgreSQL.

Reads the JSON-lines that `obicaller.py --json` writes and inserts them. The
two are separate processes on purpose: one knows about UDP and OBi log
formats, the other knows about a database, and a pipe between them means
either can be restarted, replaced or run on a different host without touching
the other.

    obicaller.py --port 5515 --json | obi-syslog-pg.py

**No Python database driver is required.** It shells out to `psql` and streams
`COPY ... FROM STDIN` — which is both the fastest way to load rows and the one
that works on a machine where you would rather not install anything. Values
are written as CSV by the `csv` module, so quoting is handled properly rather
than by string concatenation into SQL.

Credentials come from the standard libpq environment (`PGHOST`, `PGDATABASE`,
`PGUSER`, `PGPASSWORD`, …), so they can live in a mode-600 environment file
that systemd loads, and never appear in a command line.

Schema
------

    CREATE TABLE syslog_event (
        id            bigserial PRIMARY KEY,
        received_at   timestamptz NOT NULL,
        source        inet,
        severity      smallint,
        severity_name text,
        facility      smallint,
        category      text,
        sp            smallint,
        call_event    text,
        number        text,
        message       text NOT NULL,
        raw           text NOT NULL
    );

`--create` will create exactly that, plus its indexes, and exit.

Idea credit for the underlying syslog approach: obicaller by Ryan Young
(https://github.com/YoRyan/obicaller), public domain. See obicaller.py.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import io
import json
import os
import subprocess
import sys
import time

__version__ = "1.0.0"

#: Lines that are pure device housekeeping: no call, no fault, no meaning
#: beyond "still alive". They are excluded at the listener, but rows written
#: before an exclude existed -- or by a second consumer that has none -- still
#: pile up, so the cleanup pass knows them too. SQL LIKE patterns.
NOISE_PATTERNS = (
    "BASE:resolving %",             # the retired OBiTALK provisioning retry
    "CallHistoryXmlSize=%",         # a buffer gauge, once a minute
    "PARAM Cache Write Back%",      # flash bookkeeping
)

COLUMNS = (
    "received_at", "source", "severity", "severity_name", "facility",
    "category", "sp", "call_event", "number", "message", "raw",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS syslog_event (
    id            bigserial PRIMARY KEY,
    received_at   timestamptz NOT NULL,
    source        inet,
    severity      smallint,
    severity_name text,
    facility      smallint,
    category      text,
    sp            smallint,
    call_event    text,
    number        text,
    message       text NOT NULL,
    raw           text NOT NULL
);
CREATE INDEX IF NOT EXISTS syslog_event_received_idx
    ON syslog_event (received_at DESC);
CREATE INDEX IF NOT EXISTS syslog_event_call_idx
    ON syslog_event (call_event, received_at DESC) WHERE call_event IS NOT NULL;
CREATE INDEX IF NOT EXISTS syslog_event_sp_idx
    ON syslog_event (sp, received_at DESC);
"""


def to_row(event):
    """One event dict -> one row, in COLUMNS order.

    Accepts either spelling of the timestamp: obicaller writes `at`, the
    obicfg package's own listener writes `received_at`.
    """
    stamp = event.get("received_at", event.get("at"))
    if isinstance(stamp, (int, float)):
        stamp = datetime.datetime.fromtimestamp(
            stamp, datetime.timezone.utc
        ).isoformat()
    row = {key: event.get(key) for key in COLUMNS}
    row["received_at"] = stamp or datetime.datetime.now(
        datetime.timezone.utc
    ).isoformat()
    # An empty string is not a valid inet, and "" is what a missing source
    # looks like coming out of the listener.
    row["source"] = row["source"] or None
    return [row[key] for key in COLUMNS]


def copy_rows(rows, table="syslog_event", psql="psql", dry_run=False):
    """Load rows with COPY ... FROM STDIN. Returns the number sent."""
    if not rows:
        return 0
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for row in rows:
        writer.writerow(["" if value is None else value for value in row])
    payload = buffer.getvalue()

    statement = (
        "COPY %s (%s) FROM STDIN WITH (FORMAT csv, NULL '')"
        % (table, ", ".join(COLUMNS))
    )
    if dry_run:
        sys.stderr.write(statement + "\n" + payload)
        return len(rows)

    process = subprocess.run(
        [psql, "--quiet", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-c", statement],
        input=payload,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "psql failed")
    return len(rows)


def create_schema(psql="psql"):
    process = subprocess.run(
        [psql, "--quiet", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-c", SCHEMA],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "psql failed")
    return True


def prune(keep_days=30, keep_call_days=365, drop_noise=True,
          table="syslog_event", psql="psql", dry_run=False):
    """Delete what is not worth keeping. Returns the SQL that was run.

    Two retentions, because the rows are not equally valuable: a call event is
    a record of something that happened to a person and is worth a year; a
    line saying the device is still alive is worth a month at most. Noise is
    worth nothing the moment it lands, but rows written before the listener
    had an exclude for it are already in the table.
    """
    clauses = [
        "DELETE FROM %s WHERE call_event IS NULL"
        " AND received_at < now() - interval '%d days'" % (table, keep_days),
        "DELETE FROM %s WHERE call_event IS NOT NULL"
        " AND received_at < now() - interval '%d days'" % (table, keep_call_days),
    ]
    if drop_noise:
        # Never delete a row that carries a call event, however it is worded.
        likes = " OR ".join("message LIKE %s" % _literal(p) for p in NOISE_PATTERNS)
        clauses.append(
            "DELETE FROM %s WHERE call_event IS NULL AND (%s)" % (table, likes)
        )
    statement = ";\n".join(clauses) + ";"
    if dry_run:
        sys.stderr.write(statement + "\n")
        return statement
    process = subprocess.run(
        [psql, "--quiet", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-c", statement],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or "psql failed")
    return statement


def _literal(text):
    """Quote a string for SQL. These are our own constants, but still."""
    return "'" + text.replace("'", "''") + "'"


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="obi-syslog-pg",
        description="Store OBi syslog events (JSON lines on stdin) in PostgreSQL.",
        epilog="Connection settings come from the libpq environment: PGHOST, "
               "PGDATABASE, PGUSER, PGPASSWORD. Keep them in a mode-600 file.",
    )
    parser.add_argument("--version", action="version",
                        version="obi-syslog-pg " + __version__)
    parser.add_argument("--table", default="syslog_event")
    parser.add_argument("--psql", default="psql", help="path to the psql client")
    parser.add_argument("--batch", type=int, default=50,
                        help="rows per COPY (default 50)")
    parser.add_argument("--flush-seconds", type=float, default=5.0,
                        help="write a partial batch after this long (default 5). "
                             "Call events are rare and you want them visible "
                             "now, not once fifty have accumulated")
    parser.add_argument("--create", action="store_true",
                        help="create the table and indexes, then exit")
    parser.add_argument("--prune", action="store_true",
                        help="delete expired and junk rows, then exit")
    parser.add_argument("--keep-days", type=int, default=30, metavar="N",
                        help="how long to keep rows with no call event (default 30)")
    parser.add_argument("--keep-call-days", type=int, default=365, metavar="N",
                        help="how long to keep call events (default 365)")
    parser.add_argument("--keep-noise", action="store_true",
                        help="do not delete device housekeeping lines on sight")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the COPY and its rows instead of running it")
    args = parser.parse_args(argv)

    if args.create:
        create_schema(args.psql)
        print("schema ready")
        return 0

    if args.prune:
        prune(args.keep_days, args.keep_call_days, not args.keep_noise,
              args.table, args.psql, args.dry_run)
        if not args.dry_run:
            print("pruned")
        return 0

    pending = []
    last_flush = time.time()
    written = 0

    def flush():
        nonlocal pending, last_flush, written
        if not pending:
            return
        try:
            written += copy_rows(pending, args.table, args.psql, args.dry_run)
        except RuntimeError as exc:
            # Losing the database must not kill the pipeline: the listener
            # upstream keeps running and the next batch may well succeed.
            print("obi-syslog-pg: %s" % exc, file=sys.stderr)
        pending = []
        last_flush = time.time()

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                print("obi-syslog-pg: skipping unparseable line", file=sys.stderr)
                continue
            pending.append(to_row(event))
            if len(pending) >= args.batch:
                flush()
            elif time.time() - last_flush >= args.flush_seconds:
                flush()
    except KeyboardInterrupt:
        pass
    finally:
        flush()
    if not args.dry_run:
        print("obi-syslog-pg: wrote %d row(s)" % written, file=sys.stderr)
    return 0


if __name__ == "__main__":
    if os.environ.get("PGPASSWORD") and "--dry-run" not in sys.argv:
        # A reminder rather than a refusal: it is legitimate, but it is also
        # visible in /proc to anyone who can read it on some systems.
        pass
    sys.exit(main())
