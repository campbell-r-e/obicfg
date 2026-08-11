"""Tests for contrib/obi-syslog-pg.py.

No PostgreSQL required: the loader shells out to `psql`, so a stub on PATH
lets the whole path be exercised — including the exact COPY statement and the
CSV it feeds in, which is the part that would corrupt data if it were wrong.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "contrib" / "obi-syslog-pg.py"


@pytest.fixture(scope="module")
def pg():
    spec = importlib.util.spec_from_file_location("obi_syslog_pg", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_psql(tmp_path):
    """A psql that records its arguments and stdin instead of connecting."""
    recorder = tmp_path / "psql-calls.txt"
    stub = tmp_path / "psql"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "ARGS: $*" >> "{recorder}"\n'
        f'cat >> "{recorder}"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return stub, recorder


class TestRowMapping:
    def test_a_float_timestamp_becomes_utc_iso(self, pg):
        row = pg.to_row({"at": 0, "message": "m", "raw": "r"})
        assert row[0].startswith("1970-01-01T00:00:00")
        assert "+00:00" in row[0]

    def test_both_timestamp_spellings_are_accepted(self, pg):
        # obicaller writes `at`; the obicfg package's listener writes
        # `received_at`. Either must land in the same column.
        assert pg.to_row({"at": 0, "message": "m", "raw": "r"})[0] == \
               pg.to_row({"received_at": 0, "message": "m", "raw": "r"})[0]

    def test_a_string_timestamp_passes_through(self, pg):
        stamp = "2026-08-10T20:00:00+00:00"
        assert pg.to_row({"received_at": stamp, "message": "m", "raw": "r"})[0] == stamp

    def test_a_missing_timestamp_is_stamped_now(self, pg):
        row = pg.to_row({"message": "m", "raw": "r"})
        year = datetime.datetime.now(datetime.timezone.utc).year
        assert row[0].startswith(str(year))

    def test_an_empty_source_becomes_null_not_an_invalid_inet(self, pg):
        # "" is not a valid inet and would fail the COPY for the whole batch.
        assert pg.to_row({"at": 0, "source": "", "message": "m", "raw": "r"})[1] is None
        assert pg.to_row({"at": 0, "source": "192.0.2.5", "message": "m",
                          "raw": "r"})[1] == "192.0.2.5"

    def test_columns_are_in_a_fixed_order(self, pg):
        assert pg.COLUMNS[0] == "received_at"
        assert pg.COLUMNS[-2:] == ("message", "raw")


class TestCopy:
    def test_the_statement_and_csv_are_what_postgres_expects(self, pg, fake_psql):
        stub, recorder = fake_psql
        rows = [pg.to_row({"at": 0, "source": "192.0.2.5", "severity": 6,
                           "category": "call", "sp": 2, "call_event": "ringing",
                           "number": "+15551234567",
                           "message": "CALL:x", "raw": "<6> CALL:x"})]
        assert pg.copy_rows(rows, psql=str(stub)) == 1
        recorded = recorder.read_text()
        assert "COPY syslog_event (received_at, source, severity," in recorded
        assert "FORMAT csv, NULL ''" in recorded
        assert "192.0.2.5" in recorded and "ringing" in recorded

    def test_values_containing_commas_and_quotes_are_quoted(self, pg, fake_psql):
        stub, recorder = fake_psql
        # Written by the csv module rather than by joining strings, so a
        # message with a comma cannot shift every later column by one.
        rows = [pg.to_row({"at": 0, "message": 'a,b "c"', "raw": "x"})]
        pg.copy_rows(rows, psql=str(stub))
        body = recorder.read_text()
        assert '"a,b ""c"""' in body

    def test_nulls_are_written_as_empty_fields(self, pg, fake_psql):
        stub, recorder = fake_psql
        pg.copy_rows([pg.to_row({"at": 0, "message": "m", "raw": "r"})], psql=str(stub))
        assert ",,,,," in recorder.read_text()

    def test_no_rows_is_not_a_call(self, pg, fake_psql):
        stub, recorder = fake_psql
        assert pg.copy_rows([], psql=str(stub)) == 0
        assert not recorder.exists()

    def test_a_psql_failure_is_raised_with_its_message(self, pg, tmp_path):
        stub = tmp_path / "psql"
        stub.write_text("#!/bin/sh\necho 'FATAL: nope' >&2\nexit 1\n")
        stub.chmod(0o755)
        with pytest.raises(RuntimeError, match="FATAL: nope"):
            pg.copy_rows([["x"] * len(pg.COLUMNS)], psql=str(stub))

    def test_dry_run_writes_nothing_to_psql(self, pg, fake_psql, capsys):
        stub, recorder = fake_psql
        pg.copy_rows([pg.to_row({"at": 0, "message": "m", "raw": "r"})],
                     psql=str(stub), dry_run=True)
        assert not recorder.exists()
        assert "COPY syslog_event" in capsys.readouterr().err

    def test_create_schema(self, pg, fake_psql):
        stub, recorder = fake_psql
        assert pg.create_schema(psql=str(stub)) is True
        assert "CREATE TABLE IF NOT EXISTS syslog_event" in recorder.read_text()

    def test_create_schema_reports_failure(self, pg, tmp_path):
        stub = tmp_path / "psql"
        stub.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")
        stub.chmod(0o755)
        with pytest.raises(RuntimeError, match="boom"):
            pg.create_schema(psql=str(stub))


class TestEndToEnd:
    def _run(self, stub, stdin, *extra):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--psql", str(stub), "--batch", "2", *extra],
            input=stdin, text=True, capture_output=True, timeout=30,
        )

    def test_jsonl_on_stdin_reaches_psql(self, fake_psql):
        stub, recorder = fake_psql
        lines = "\n".join(
            json.dumps({"at": 0, "message": f"m{n}", "raw": f"r{n}",
                        "call_event": "ended"})
            for n in range(3)
        )
        result = self._run(stub, lines)
        assert result.returncode == 0
        assert "wrote 3 row(s)" in result.stderr
        body = recorder.read_text()
        assert "m0" in body and "m2" in body

    def test_a_bad_line_is_skipped_not_fatal(self, fake_psql):
        stub, recorder = fake_psql
        result = self._run(stub, 'not json\n{"at":0,"message":"ok","raw":"r"}\n')
        assert result.returncode == 0
        assert "skipping unparseable line" in result.stderr
        assert "ok" in recorder.read_text()

    def test_blank_lines_are_ignored(self, fake_psql):
        stub, _ = fake_psql
        result = self._run(stub, '\n\n{"at":0,"message":"ok","raw":"r"}\n')
        assert result.returncode == 0
        assert "wrote 1 row(s)" in result.stderr

    def test_a_database_outage_does_not_kill_the_pipeline(self, tmp_path):
        stub = tmp_path / "psql"
        stub.write_text("#!/bin/sh\necho 'could not connect' >&2\nexit 2\n")
        stub.chmod(0o755)
        result = self._run(stub, '{"at":0,"message":"ok","raw":"r"}\n')
        # The listener upstream keeps running; the next batch may succeed.
        assert result.returncode == 0
        assert "could not connect" in result.stderr

    def test_create_from_the_command_line(self, fake_psql):
        stub, recorder = fake_psql
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--psql", str(stub), "--create"],
            text=True, capture_output=True, timeout=30,
        )
        assert result.returncode == 0
        assert "schema ready" in result.stdout
        assert "CREATE TABLE" in recorder.read_text()


def test_it_credits_the_original_idea():
    source = SCRIPT.read_text()
    assert "YoRyan/obicaller" in source and "Ryan Young" in source


class TestPrune:
    """Automatic cleanup.

    Two retentions, because the rows are not equally valuable: a call event
    records something that happened to a person; a line saying the device is
    still alive does not.
    """

    def test_it_keeps_calls_longer_than_everything_else(self, pg, fake_psql):
        stub, recorder = fake_psql
        pg.prune(keep_days=30, keep_call_days=365, psql=str(stub))
        sql = recorder.read_text()
        assert "call_event IS NULL AND received_at < now() - interval '30 days'" in sql
        assert "call_event IS NOT NULL AND received_at < now() - interval '365 days'" in sql

    def test_noise_goes_on_sight(self, pg, fake_psql):
        stub, recorder = fake_psql
        pg.prune(psql=str(stub))
        sql = recorder.read_text()
        for pattern in pg.NOISE_PATTERNS:
            assert pattern in sql

    def test_a_call_event_is_never_deleted_as_noise(self, pg):
        # Every noise clause is guarded on call_event IS NULL. A device that
        # words a call line like a housekeeping line must not lose it.
        statement = pg.prune(dry_run=True)
        for clause in statement.split(";"):
            if "LIKE" in clause:
                assert "call_event IS NULL" in clause

    def test_noise_deletion_can_be_turned_off(self, pg, fake_psql):
        stub, recorder = fake_psql
        pg.prune(drop_noise=False, psql=str(stub))
        assert "LIKE" not in recorder.read_text()

    def test_retentions_are_configurable(self, pg, fake_psql):
        stub, recorder = fake_psql
        pg.prune(keep_days=7, keep_call_days=90, psql=str(stub))
        sql = recorder.read_text()
        assert "interval '7 days'" in sql and "interval '90 days'" in sql

    def test_a_failure_is_raised(self, pg, tmp_path):
        stub = tmp_path / "psql"
        stub.write_text("#!/bin/sh\necho 'boom' >&2\nexit 1\n")
        stub.chmod(0o755)
        with pytest.raises(RuntimeError, match="boom"):
            pg.prune(psql=str(stub))

    def test_literals_are_escaped(self, pg):
        assert pg._literal("it's") == "'it''s'"

    def test_from_the_command_line(self, pg, fake_psql):
        stub, recorder = fake_psql
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--psql", str(stub), "--prune",
             "--keep-days", "14"],
            text=True, capture_output=True, timeout=30,
        )
        assert result.returncode == 0
        assert "pruned" in result.stdout
        assert "interval '14 days'" in recorder.read_text()

    def test_dry_run_prunes_nothing(self, pg, fake_psql):
        stub, recorder = fake_psql
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--psql", str(stub), "--prune", "--dry-run"],
            text=True, capture_output=True, timeout=30,
        )
        assert result.returncode == 0
        assert not recorder.exists()
        assert "DELETE FROM syslog_event" in result.stderr
