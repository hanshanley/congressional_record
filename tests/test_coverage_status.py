"""Offline tests for scripts/coverage_status.py (no network, no repo data)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_module(tmp_root: Path):
    """Import coverage_status with its module-level paths rebound to a temp repo."""
    spec = importlib.util.spec_from_file_location(
        "coverage_status_under_test", ROOT / "scripts" / "coverage_status.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_root
    module.DATA = tmp_root / "data"
    module.MAIN_MANIFEST = module.DATA / "manifest.jsonl"
    module.WORKER_GLOB = str(module.DATA / "manifest_w*.jsonl")
    module.TURNS_DIR = module.DATA / "interim" / "turns"
    module.BULK_ERRORS = module.DATA / "bulk" / "_errors.txt"
    module.STATUS_PATH = module.DATA / "coverage_status.json"
    module._DATE_CACHE.clear()
    return module


def _write_manifest(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in rows),
        encoding="utf-8",
    )


def _write_turns(path: Path, dates: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "turn_id": [f"t{i}" for i in range(len(dates))],
            "date": dates,
            "congress": [119] * len(dates),
        }
    )
    pq.write_table(table, path)


@pytest.fixture()
def repo(tmp_path: Path):
    module = _load_module(tmp_path)
    _write_manifest(
        module.MAIN_MANIFEST,
        [
            {"granuleId": "g1", "dateIssued": "2025-01-02"},
            {"granuleId": "g2", "dateIssued": "2026-01-30"},
        ],
    )
    _write_turns(module.TURNS_DIR / "govinfo_bulk_119.parquet", ["2025-01-02", "2026-07-13"])
    return module


def test_reports_both_ingest_paths(repo):
    report = repo.build_report(gap_days=100000)
    assert report["api_manifest"]["unique_granules"] == 2
    assert report["api_manifest"]["latest_date"] == "2026-01-30"
    assert report["bulk_turns"]["by_source"]["govinfo_bulk"]["latest_date"] == "2026-07-13"
    # The roll-up must prefer the newest date across *both* paths, not just the manifest.
    assert report["latest_date"] == "2026-07-13"
    assert report["analysis_corpus_latest_date"] == "2026-07-13"


def test_warns_when_manifest_lags_analysis_corpus(repo):
    report = repo.build_report(gap_days=100000)
    assert any("lags the analysis corpus" in w for w in report["warnings"])


def test_detects_unmerged_worker_shard(repo):
    _write_manifest(
        repo.DATA / "manifest_w1.jsonl",
        [
            {"granuleId": "g2", "dateIssued": "2026-01-30"},  # already merged
            {"granuleId": "g9", "dateIssued": "2026-02-01"},  # missing from main
        ],
    )
    report = repo.build_report(gap_days=100000)
    worker = report["worker_manifests"][0]
    assert worker["unmerged_granules"] == 1
    assert worker["merged_into_main"] is False
    assert any("run scripts/merge_manifests.py" in w for w in report["warnings"])
    # The raw id set must not leak into the serialisable report.
    assert "_granule_ids" not in worker
    assert "_granule_ids" not in report["api_manifest"]
    json.dumps(report)


def test_no_unmerged_warning_when_shard_is_contained(repo):
    _write_manifest(
        repo.DATA / "manifest_w1.jsonl",
        [{"granuleId": "g1", "dateIssued": "2025-01-02"}],
    )
    report = repo.build_report(gap_days=100000)
    assert report["worker_manifests"][0]["merged_into_main"] is True
    assert not any("merge_manifests" in w for w in report["warnings"])


def test_warns_when_analysis_corpus_is_stale(repo):
    fresh = repo.build_report(gap_days=100000)
    assert not any("behind today" in w for w in fresh["warnings"])
    stale = repo.build_report(gap_days=0)
    assert any("behind today" in w for w in stale["warnings"])


def test_flags_duplicate_granule_rows(repo):
    _write_manifest(
        repo.MAIN_MANIFEST,
        [
            {"granuleId": "g1", "dateIssued": "2025-01-02"},
            {"granuleId": "g1", "dateIssued": "2025-01-02"},
        ],
    )
    report = repo.build_report(gap_days=100000)
    assert report["api_manifest"]["duplicate_rows"] == 1
    assert any("duplicate granuleId" in w for w in report["warnings"])


def test_tolerates_malformed_and_missing_inputs(tmp_path: Path):
    module = _load_module(tmp_path)
    module.MAIN_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    module.MAIN_MANIFEST.write_text('{"granuleId": "g1"}\nnot json\n\n', encoding="utf-8")
    report = module.build_report(gap_days=30)
    assert report["api_manifest"]["malformed_lines"] == 1
    assert report["api_manifest"]["rows"] == 1
    # No dateIssued anywhere and no turns dir: must not raise.
    assert report["api_manifest"]["latest_date"] is None
    assert report["bulk_turns"]["exists"] is False
    assert report["latest_date"] is None
    assert module.render(report)


def test_render_and_cli_write_status_file(repo, capsys):
    out = repo.DATA / "coverage_status.json"
    assert repo.main(["--out", str(out)]) == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["analysis_corpus_latest_date"] == "2026-07-13"
    assert "GovInfo bulk path" in capsys.readouterr().out


def test_no_write_flag_leaves_no_file(repo, capsys):
    out = repo.DATA / "coverage_status.json"
    assert repo.main(["--no-write", "--out", str(out)]) == 0
    assert not out.exists()
    capsys.readouterr()


def _write_errors(module, packages: list[str]) -> None:
    module.BULK_ERRORS.parent.mkdir(parents=True, exist_ok=True)
    module.BULK_ERRORS.write_text(
        "".join(f"BAD {p}\n" for p in packages), encoding="utf-8"
    )


def test_stale_error_log_entries_are_resolved_not_warned(repo):
    # _errors.txt is append-only: a package ingested on a later run stays listed.
    # Its date IS in the corpus, so it must not be reported as a gap.
    _write_errors(repo, ["CREC-2026-07-13"])
    report = repo.build_report(gap_days=100000)
    failed = report["failed_packages"]
    assert failed["logged_packages"] == 1
    assert failed["resolved_since_logged"] == 1
    assert failed["still_missing"] == []
    assert not any("_errors.txt" in w for w in report["warnings"])


def test_genuinely_missing_package_is_warned(repo):
    # 2026-07-20 falls inside the corpus date range but has no turns -> a real gap.
    _write_errors(repo, ["CREC-2026-07-20"])
    report = repo.build_report(gap_days=100000)
    failed = report["failed_packages"]
    assert failed["still_missing"] == ["CREC-2026-07-20"]
    assert failed["resolved_since_logged"] == 0
    assert any("_errors.txt" in w for w in report["warnings"])


def test_error_log_entries_outside_corpus_range_count_as_missing(repo):
    _write_errors(repo, ["CREC-1994-01-05"])
    report = repo.build_report(gap_days=100000)
    assert report["failed_packages"]["still_missing"] == ["CREC-1994-01-05"]


def test_error_log_ignores_non_package_tokens(repo):
    repo.BULK_ERRORS.parent.mkdir(parents=True, exist_ok=True)
    repo.BULK_ERRORS.write_text("BAD not-a-package\n\nBAD CREC-2026-13-99\n", encoding="utf-8")
    report = repo.build_report(gap_days=100000)
    assert report["failed_packages"]["logged_packages"] == 0
    assert report["failed_packages"]["still_missing"] == []


def test_missing_error_log_is_not_an_error(repo):
    assert not repo.BULK_ERRORS.exists()
    report = repo.build_report(gap_days=100000)
    assert report["failed_packages"]["exists"] is False
    assert repo.render(report)
    json.dumps(report)


def test_interior_gap_detected_even_when_latest_dates_match(repo):
    # Manifest ends on the same day as the corpus but is missing a day in between:
    # a max-date-only check would wrongly report full coverage.
    _write_manifest(
        repo.MAIN_MANIFEST,
        [
            {"granuleId": "g1", "dateIssued": "2025-01-02"},
            {"granuleId": "g2", "dateIssued": "2026-07-13"},
        ],
    )
    _write_turns(
        repo.TURNS_DIR / "govinfo_bulk_119.parquet",
        ["2025-01-02", "2026-01-30", "2026-07-13"],
    )
    report = repo.build_report(gap_days=100000)
    assert report["api_manifest"]["latest_date"] == report["analysis_corpus_latest_date"]
    gaps = report["api_manifest"]["interior_gaps"]
    assert gaps["missing_days"] == 1
    assert gaps["first_missing"] == "2026-01-30"
    assert any("overstates coverage" in w for w in report["warnings"])
    assert "MISSING inside that range" in repo.render(report)


def test_no_interior_gap_when_manifest_covers_every_corpus_day(repo):
    _write_manifest(
        repo.MAIN_MANIFEST,
        [
            {"granuleId": "g1", "dateIssued": "2025-01-02"},
            {"granuleId": "g2", "dateIssued": "2026-07-13"},
        ],
    )
    _write_turns(repo.TURNS_DIR / "govinfo_bulk_119.parquet", ["2025-01-02", "2026-07-13"])
    report = repo.build_report(gap_days=100000)
    assert report["api_manifest"]["interior_gaps"]["missing_days"] == 0
    assert not any("overstates coverage" in w for w in report["warnings"])


def test_corpus_history_outside_manifest_range_is_not_a_gap(repo):
    # The manifest starts in 2025; 1994 corpus data predates it and is not a hole.
    _write_manifest(repo.MAIN_MANIFEST, [{"granuleId": "g1", "dateIssued": "2025-01-02"}])
    _write_turns(repo.TURNS_DIR / "govinfo_bulk_119.parquet", ["1994-01-25", "2025-01-02"])
    report = repo.build_report(gap_days=100000)
    assert report["api_manifest"]["interior_gaps"]["missing_days"] == 0


def test_repeated_reports_do_not_use_stale_cached_dates(repo):
    # Manifest covers 2025-01-02 and 2026-07-13 but not 2026-01-30.
    _write_manifest(
        repo.MAIN_MANIFEST,
        [
            {"granuleId": "g1", "dateIssued": "2025-01-02"},
            {"granuleId": "g2", "dateIssued": "2026-07-13"},
        ],
    )
    first = repo.build_report(gap_days=100000)
    assert first["api_manifest"]["interior_gaps"]["missing_days"] == 0
    # Rewrite the corpus in-place, adding a day the manifest lacks; a process-global
    # cache would keep serving the old date set and miss the new gap.
    _write_turns(
        repo.TURNS_DIR / "govinfo_bulk_119.parquet",
        ["2025-01-02", "2026-01-30", "2026-07-13"],
    )
    second = repo.build_report(gap_days=100000)
    assert second["api_manifest"]["interior_gaps"]["missing_days"] == 1
    assert second["api_manifest"]["interior_gaps"]["first_missing"] == "2026-01-30"
