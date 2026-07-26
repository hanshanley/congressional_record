#!/usr/bin/env python3
"""coverage_status.py -- report true corpus coverage across every ingest path.

The repository has two independent ingest pipelines that keep separate
bookkeeping, which makes "how current is this repo?" easy to get wrong:

* the GovInfo **API** path (``fetch_crec.py``) indexes granules in
  ``data/manifest.jsonl`` (plus transient ``data/manifest_w*.jsonl`` worker
  shards that must be folded in via ``scripts/merge_manifests.py``);
* the GovInfo **bulk** path (``scripts/bulk_pipeline.py``) writes speech turns
  to ``data/interim/turns/*.parquet``, and is the corpus the analysis code
  actually reads.

Reading either one alone gives a wrong answer. This script reports both, plus a
single ``latest_date`` roll-up, and writes ``data/coverage_status.json``.

Examples
--------
    python scripts/coverage_status.py
    python scripts/coverage_status.py --json
    python scripts/coverage_status.py --gap-days 45
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MAIN_MANIFEST = DATA / "manifest.jsonl"
WORKER_GLOB = str(DATA / "manifest_w*.jsonl")
TURNS_DIR = DATA / "interim" / "turns"
BULK_ERRORS = DATA / "bulk" / "_errors.txt"
STATUS_PATH = DATA / "coverage_status.json"

# Bulk turn parquets are named "<source>_<congress>.parquet"; the analysis corpus
# is the union of every source.
TURN_GLOB = "*.parquet"

# CREC package ids are exactly CREC-YYYY-MM-DD.
_PKG_DATE_RE = re.compile(r"^CREC-(\d{4}-\d{2}-\d{2})$")

# date-column sets, keyed by Parquet path, so repeated lookups read each file once.
_DATE_CACHE: Dict[Path, set] = {}


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--json", action="store_true", help="Print the JSON report instead of a table.")
    p.add_argument(
        "--gap-days",
        type=int,
        default=30,
        help="Warn if the newest turn is older than this many days (default: 30).",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=STATUS_PATH,
        help=f"Where to write the JSON report (default: {STATUS_PATH.relative_to(ROOT)}).",
    )
    p.add_argument("--no-write", action="store_true", help="Do not write the JSON report.")
    return p.parse_args(argv)


def _scan_manifest(path: Path, keep_ids: bool = False) -> Dict[str, Any]:
    """Summarise one JSONL manifest, streaming it a line at a time.

    When ``keep_ids`` is set the granuleId set and the set of covered dates are
    returned under ``_granule_ids`` / ``_dates`` so callers can check that worker
    shards are fully merged and that coverage has no interior holes. Both keys are
    stripped before the report is serialised.
    """
    if not path.exists():
        return {"path": str(path.relative_to(ROOT)), "exists": False}
    rows = 0
    malformed = 0
    granules: set[str] = set()
    dates: set[str] = set()
    earliest: Optional[str] = None
    latest: Optional[str] = None
    by_year: Dict[str, int] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            rows += 1
            gid = row.get("granuleId")
            if gid:
                granules.add(gid)
            date = row.get("dateIssued") or ""
            if len(date) >= 10:
                date = date[:10]
                dates.add(date)
                if earliest is None or date < earliest:
                    earliest = date
                if latest is None or date > latest:
                    latest = date
                by_year[date[:4]] = by_year.get(date[:4], 0) + 1
    return {
        "path": str(path.relative_to(ROOT)),
        "exists": True,
        "rows": rows,
        "unique_granules": len(granules),
        "duplicate_rows": rows - len(granules),
        "malformed_lines": malformed,
        "earliest_date": earliest,
        "latest_date": latest,
        "rows_by_year": dict(sorted(by_year.items())),
        **({"_granule_ids": granules, "_dates": dates} if keep_ids else {}),
    }


def _scan_turns(turns_dir: Path) -> Dict[str, Any]:
    """Summarise the bulk turn Parquet corpus via footer statistics only."""
    if not turns_dir.exists():
        return {"path": str(turns_dir.relative_to(ROOT)), "exists": False, "files": []}

    import pyarrow.parquet as pq

    files: List[Dict[str, Any]] = []
    by_source: Dict[str, Dict[str, Any]] = {}
    for path in sorted(turns_dir.glob(TURN_GLOB)):
        source = path.stem.rsplit("_", 1)[0]
        handle = pq.ParquetFile(path)
        rows = handle.metadata.num_rows
        earliest, latest = _date_bounds(handle)
        files.append(
            {
                "file": path.name,
                "source": source,
                "rows": rows,
                "earliest_date": earliest,
                "latest_date": latest,
                "bytes": path.stat().st_size,
            }
        )
        agg = by_source.setdefault(
            source, {"files": 0, "rows": 0, "earliest_date": None, "latest_date": None}
        )
        agg["files"] += 1
        agg["rows"] += rows
        if earliest and (agg["earliest_date"] is None or earliest < agg["earliest_date"]):
            agg["earliest_date"] = earliest
        if latest and (agg["latest_date"] is None or latest > agg["latest_date"]):
            agg["latest_date"] = latest

    return {
        "path": str(turns_dir.relative_to(ROOT)),
        "exists": True,
        "total_rows": sum(f["rows"] for f in files),
        "by_source": dict(sorted(by_source.items())),
        "files": files,
    }


def _date_bounds(handle: "pq.ParquetFile") -> tuple[Optional[str], Optional[str]]:
    """Min/max of the ``date`` column, read from row-group statistics when present.

    Falls back to reading just the ``date`` column if statistics are unavailable,
    which is still far cheaper than materialising the (large) ``text`` column.
    """
    field_index = handle.schema_arrow.get_field_index("date")
    if field_index < 0:
        return None, None
    earliest: Optional[str] = None
    latest: Optional[str] = None
    metadata = handle.metadata
    for group in range(metadata.num_row_groups):
        stats = metadata.row_group(group).column(field_index).statistics
        if stats is None or not stats.has_min_max:
            earliest = latest = None
            break
        low, high = str(stats.min), str(stats.max)
        if earliest is None or low < earliest:
            earliest = low
        if latest is None or high > latest:
            latest = high
    else:
        return earliest, latest

    table = handle.read(columns=["date"])
    values = [v for v in table.column("date").to_pylist() if v]
    if not values:
        return None, None
    return min(values), max(values)


def _resolve_failed_packages(errors_path: Path, bulk: Dict[str, Any]) -> Dict[str, Any]:
    """Check whether packages logged in ``_errors.txt`` are still missing.

    ``data/bulk/_errors.txt`` is an append-only log: a package that failed once
    and was ingested on a later run stays listed forever. Treating it as a live
    gap list is misleading, so each entry is resolved against the corpus and only
    genuinely absent dates are reported.
    """
    if not errors_path.exists():
        return {"path": str(errors_path.relative_to(ROOT)), "exists": False}

    dates: Dict[str, str] = {}
    for line in errors_path.read_text(encoding="utf-8").splitlines():
        for token in line.split():
            match = _PKG_DATE_RE.match(token)
            if not match:
                continue
            try:
                dt.date.fromisoformat(match.group(1))
            except ValueError:
                # A structurally-shaped but impossible date (e.g. CREC-2026-13-99)
                # could never be found in the corpus and would warn forever.
                continue
            dates[match.group(1)] = token
    if not dates:
        return {
            "path": str(errors_path.relative_to(ROOT)),
            "exists": True,
            "logged_packages": 0,
            "still_missing": [],
        }

    still_missing = sorted(
        package for date, package in dates.items() if not _corpus_has_date(bulk, date)
    )
    return {
        "path": str(errors_path.relative_to(ROOT)),
        "exists": True,
        "logged_packages": len(dates),
        "resolved_since_logged": len(dates) - len(still_missing),
        "still_missing": still_missing,
    }


def _corpus_has_date(bulk: Dict[str, Any], date: str) -> bool:
    """True if ``date`` appears in the bulk corpus.

    Only files whose row-group date bounds straddle ``date`` are opened, and only
    their ``date`` column is read, so this never touches the large ``text`` column.
    """
    import pyarrow.parquet as pq

    for entry in bulk.get("files", []):
        low, high = entry.get("earliest_date"), entry.get("latest_date")
        if not low or not high or not (low <= date <= high):
            continue
        path = TURNS_DIR / entry["file"]
        cached = _DATE_CACHE.get(path)
        if cached is None:
            table = pq.ParquetFile(path).read(columns=["date"])
            cached = {v for v in table.column("date").to_pylist() if v}
            _DATE_CACHE[path] = cached
        if date in cached:
            return True
    return False


def _corpus_dates(bulk: Dict[str, Any], source: str = "govinfo_bulk") -> set:
    """All dates covered by one bulk source, reading only the ``date`` column."""
    import pyarrow.parquet as pq

    covered: set = set()
    for entry in bulk.get("files", []):
        if entry.get("source") != source:
            continue
        path = TURNS_DIR / entry["file"]
        cached = _DATE_CACHE.get(path)
        if cached is None:
            table = pq.ParquetFile(path).read(columns=["date"])
            cached = {v for v in table.column("date").to_pylist() if v}
            _DATE_CACHE[path] = cached
        covered |= cached
    return covered


def _interior_gaps(manifest_dates: set, corpus_dates: set) -> Dict[str, Any]:
    """Days the corpus has that the manifest lacks, inside the manifest's own range.

    Comparing only the newest date hides interior holes: a manifest can end on the
    same day as the corpus while missing months in the middle. Only the manifest's
    own [earliest, latest] window is examined, so legitimately un-backfilled history
    outside that window is not reported.
    """
    if not manifest_dates or not corpus_dates:
        return {"missing_days": 0, "first_missing": None, "last_missing": None}
    low, high = min(manifest_dates), max(manifest_dates)
    missing = sorted(d for d in corpus_dates if low <= d <= high and d not in manifest_dates)
    return {
        "missing_days": len(missing),
        "first_missing": missing[0] if missing else None,
        "last_missing": missing[-1] if missing else None,
    }


def build_report(gap_days: int) -> Dict[str, Any]:
    # Memoise per report, not per process, so a second call never sees stale files.
    _DATE_CACHE.clear()
    today = dt.date.today()
    api = _scan_manifest(MAIN_MANIFEST, keep_ids=True)
    main_ids: set[str] = api.pop("_granule_ids", set())
    main_dates: set[str] = api.pop("_dates", set())
    workers = [_scan_manifest(Path(p), keep_ids=True) for p in sorted(glob.glob(WORKER_GLOB))]
    for worker in workers:
        worker_ids: set[str] = worker.pop("_granule_ids", set())
        worker.pop("_dates", None)
        missing = worker_ids - main_ids
        worker["unmerged_granules"] = len(missing)
        worker["merged_into_main"] = not missing
    bulk = _scan_turns(TURNS_DIR)
    failed = _resolve_failed_packages(BULK_ERRORS, bulk)
    gaps = _interior_gaps(main_dates, _corpus_dates(bulk))
    api["interior_gaps"] = gaps

    candidates = [d for d in (api.get("latest_date"), bulk_latest(bulk)) if d]
    latest = max(candidates) if candidates else None

    warnings: List[str] = []
    unmerged = [w for w in workers if w.get("unmerged_granules")]
    if unmerged:
        total_missing = sum(w["unmerged_granules"] for w in unmerged)
        warnings.append(
            f"{len(unmerged)} worker manifest(s) hold {total_missing} granules missing from "
            "manifest.jsonl; run scripts/merge_manifests.py"
        )
    if api.get("duplicate_rows"):
        warnings.append(
            f"manifest.jsonl has {api['duplicate_rows']} duplicate granuleId rows; "
            "run scripts/merge_manifests.py"
        )
    bulk_date = bulk_latest(bulk)
    if bulk_date:
        stale = (today - dt.date.fromisoformat(bulk_date)).days
        if stale > gap_days:
            warnings.append(
                f"analysis corpus (data/interim/turns) is {stale} days behind today "
                f"(newest turn {bulk_date}); run scripts/bulk_pipeline.py to catch up"
            )
    if api.get("latest_date") and bulk_date and api["latest_date"] < bulk_date:
        warnings.append(
            f"manifest.jsonl ({api['latest_date']}) lags the analysis corpus ({bulk_date}); "
            "the manifest is NOT the source of truth for coverage"
        )
    if failed.get("still_missing"):
        warnings.append(
            f"{len(failed['still_missing'])} package(s) logged in data/bulk/_errors.txt are "
            f"still absent from the corpus: {', '.join(failed['still_missing'][:5])}"
            + (" ..." if len(failed["still_missing"]) > 5 else "")
        )
    if gaps["missing_days"]:
        warnings.append(
            f"manifest.jsonl has {gaps['missing_days']} day(s) inside its own date range "
            f"({gaps['first_missing']} .. {gaps['last_missing']}) that the analysis corpus "
            "covers but the manifest does not; its newest date alone overstates coverage"
        )

    return {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "latest_date": latest,
        "analysis_corpus_latest_date": bulk_date,
        "api_manifest": api,
        "worker_manifests": workers,
        "bulk_turns": bulk,
        "failed_packages": failed,
        "warnings": warnings,
    }


def bulk_latest(bulk: Dict[str, Any]) -> Optional[str]:
    dates = [
        agg["latest_date"] for agg in bulk.get("by_source", {}).values() if agg.get("latest_date")
    ]
    return max(dates) if dates else None


def render(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"Coverage status as of {report['today']}")
    lines.append("")

    api = report["api_manifest"]
    lines.append("GovInfo API path (fetch_crec.py)")
    if api.get("exists"):
        lines.append(
            f"  data/manifest.jsonl   {api['unique_granules']:>9,} granules   "
            f"{api['earliest_date']} -> {api['latest_date']}"
        )
        gaps = api.get("interior_gaps") or {}
        if gaps.get("missing_days"):
            lines.append(
                f"  {'':<21} {gaps['missing_days']:>9,} days MISSING inside that range "
                f"({gaps['first_missing']} .. {gaps['last_missing']})"
            )
    else:
        lines.append("  data/manifest.jsonl   (missing)")
    for worker in report["worker_manifests"]:
        state = "merged" if worker.get("merged_into_main") else f"{worker.get('unmerged_granules', 0)} UNMERGED"
        lines.append(
            f"  {Path(worker['path']).name:<21} {worker.get('unique_granules', 0):>9,} granules   "
            f"{worker.get('earliest_date')} -> {worker.get('latest_date')}  [{state}]"
        )
    lines.append("")

    bulk = report["bulk_turns"]
    lines.append("GovInfo bulk path (scripts/bulk_pipeline.py) -- the analysis corpus")
    for source, agg in bulk.get("by_source", {}).items():
        lines.append(
            f"  {source:<21} {agg['rows']:>9,} turns      "
            f"{agg['earliest_date']} -> {agg['latest_date']}  ({agg['files']} files)"
        )
    lines.append(f"  {'TOTAL':<21} {bulk.get('total_rows', 0):>9,} turns")
    failed = report.get("failed_packages", {})
    if failed.get("exists"):
        lines.append(
            f"  logged failures        {failed.get('logged_packages', 0)} "
            f"({failed.get('resolved_since_logged', 0)} since ingested, "
            f"{len(failed.get('still_missing', []))} still missing)"
        )
    lines.append("")
    lines.append(f"Newest data anywhere:   {report['latest_date']}")
    lines.append(f"Newest analysis turn:   {report['analysis_corpus_latest_date']}")

    if report["warnings"]:
        lines.append("")
        lines.append("Warnings:")
        for warning in report["warnings"]:
            lines.append(f"  ! {warning}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    report = build_report(args.gap_days)
    if not args.no_write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
