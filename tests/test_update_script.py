"""Tests for the one-command refresh script (scripts/update.py).

Covers the window arithmetic that decides what to ingest -- getting this wrong
either re-downloads the whole corpus or silently skips a day.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", ROOT / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def env(tmp_path: Path):
    update = _load("update")
    status = update._load_coverage_status()
    status.ROOT = tmp_path
    status.DATA = tmp_path / "data"
    status.MAIN_MANIFEST = status.DATA / "manifest.jsonl"
    status.WORKER_GLOB = str(status.DATA / "manifest_w*.jsonl")
    status.TURNS_DIR = status.DATA / "interim" / "turns"
    status.BULK_ERRORS = status.DATA / "bulk" / "_errors.txt"
    status.STATUS_PATH = status.DATA / "coverage_status.json"
    status._DATE_CACHE.clear()
    return update, status


def _write_turns(status, dates: list[str]) -> None:
    status.TURNS_DIR.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "turn_id": [f"t{i}" for i in range(len(dates))],
            "date": dates,
            "congress": [119] * len(dates),
        }
    )
    pq.write_table(table, status.TURNS_DIR / "govinfo_bulk_119.parquet")


class _Args:
    def __init__(self, since=None, until=None):
        self.since = since
        self.until = until


def test_window_starts_the_day_after_the_newest_turn(env):
    update, status = env
    _write_turns(status, ["2026-07-13", "2026-07-23"])
    start, end = update.resolve_window(_Args(until="2026-07-26"), status)
    assert start == "2026-07-24"
    assert end == "2026-07-26"


def test_window_is_none_when_corpus_is_already_current(env):
    update, status = env
    _write_turns(status, ["2026-07-26"])
    assert update.resolve_window(_Args(until="2026-07-26"), status) is None


def test_explicit_since_overrides_the_corpus_date(env):
    update, status = env
    _write_turns(status, ["2026-07-23"])
    start, end = update.resolve_window(_Args(since="2026-01-01", until="2026-07-26"), status)
    assert (start, end) == ("2026-01-01", "2026-07-26")


def test_end_defaults_to_today(env):
    update, status = env
    _write_turns(status, ["2026-07-23"])
    start, end = update.resolve_window(_Args(), status)
    assert end == dt.date.today().isoformat()


def test_empty_corpus_reports_an_error_instead_of_guessing(env):
    update, status = env
    status.TURNS_DIR.mkdir(parents=True, exist_ok=True)
    assert update.corpus_latest_date(status) is None
    assert update.resolve_window(_Args(), status) is None


def test_corpus_latest_date_reads_the_newest_turn(env):
    update, status = env
    _write_turns(status, ["2025-01-02", "2026-07-23", "2026-03-01"])
    assert update.corpus_latest_date(status) == "2026-07-23"


def test_dry_run_makes_no_changes(env, tmp_path, monkeypatch, capsys):
    update, status = env
    _write_turns(status, ["2026-07-23"])
    monkeypatch.setattr(update, "_load_coverage_status", lambda: status)

    called = []
    monkeypatch.setattr(update, "step_ingest", lambda *a, **k: called.append("ingest"))
    monkeypatch.setattr(update, "step_aggregate", lambda *a, **k: called.append("aggregate"))
    monkeypatch.setattr(update, "step_viz", lambda *a, **k: called.append("viz"))

    assert update.main(["--dry-run"]) == 0
    assert called == []
    assert not status.STATUS_PATH.exists()


def test_skip_flags_are_honoured(env, monkeypatch):
    update, status = env
    _write_turns(status, ["2026-07-23"])
    monkeypatch.setattr(update, "_load_coverage_status", lambda: status)

    called = []
    monkeypatch.setattr(update, "step_ingest", lambda *a, **k: called.append("ingest"))
    monkeypatch.setattr(update, "step_aggregate", lambda *a, **k: called.append("aggregate"))
    monkeypatch.setattr(update, "step_viz", lambda *a, **k: called.append("viz"))

    assert update.main(["--skip-ingest", "--skip-viz"]) == 0
    assert called == ["aggregate"]
    # The run is self-verifying: it always writes a fresh coverage report.
    assert status.STATUS_PATH.exists()
