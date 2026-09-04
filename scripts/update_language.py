#!/usr/bin/env python3
"""Refresh current-Congress aggregate language metrics from GovInfo."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from analysis.daily_language import (  # noqa: E402
    aggregate_turn_files,
    load_daily,
    merge_long_run_payload,
    replace_daily_window,
    save_daily,
)
from analysis.ingest.schema import congress_from_year, year_from_congress  # noqa: E402


LOG = logging.getLogger("update_language")
DAILY_PATH = ROOT / "data" / "site" / "language_daily.parquet"
LONG_RUN_PATH = ROOT / "data" / "site" / "long_run_language.json"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily", type=Path, default=DAILY_PATH)
    parser.add_argument("--long-run", type=Path, default=LONG_RUN_PATH)
    parser.add_argument("--since", metavar="YYYY-MM-DD")
    parser.add_argument("--until", metavar="YYYY-MM-DD")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Remove incremental rows after a canonical full historical rebuild.",
    )
    return parser.parse_args(argv)


def resolve_window(
    args: argparse.Namespace,
    daily: Optional[pd.DataFrame],
    today: Optional[dt.date] = None,
) -> Optional[tuple[str, str]]:
    today = today or dt.date.today()
    end = args.until or today.isoformat()
    if args.since:
        return (args.since, end) if args.since <= end else None

    congress = congress_from_year(today.year)
    congress_start = dt.date(year_from_congress(congress), 1, 1)
    current = (
        daily[daily["congress"] == congress]
        if daily is not None and not daily.empty
        else pd.DataFrame()
    )
    if current.empty:
        start = congress_start
    else:
        latest = dt.date.fromisoformat(str(current["date"].max())[:10])
        start = max(
            congress_start,
            latest - dt.timedelta(days=max(0, args.lookback_days)),
        )
    return (start.isoformat(), end) if start.isoformat() <= end else None


def probe_windows(start: str, end: str) -> list[tuple[str, str]]:
    """Split a date range into GovInfo probe windows of at most one year."""
    cursor = dt.date.fromisoformat(start)
    final = dt.date.fromisoformat(end)
    windows = []
    while cursor <= final:
        window_end = min(final, cursor + dt.timedelta(days=364))
        windows.append((cursor.isoformat(), window_end.isoformat()))
        cursor = window_end + dt.timedelta(days=1)
    return windows


def fetch_and_score(
    start: str,
    end: str,
    workers: int,
) -> Optional[pd.DataFrame]:
    from analysis.ingest.govinfo_bulk import probe_packages, run_bulk

    packages = []
    for window_start, window_end in probe_windows(start, end):
        packages.extend(
            probe_packages(window_start, window_end, workers=workers)
        )
    if not packages:
        LOG.info("no issues published in %s..%s", start, end)
        return None
    LOG.info("fetching and aggregating %d issue(s) %s..%s", len(packages), start, end)
    with tempfile.TemporaryDirectory(prefix="crec-language-") as tmp:
        tmp_path = Path(tmp)
        run_bulk(packages, tmp_path / "bulk", tmp_path, workers=workers)
        turn_files = sorted((tmp_path / "turns").glob("*.parquet"))
        if not turn_files:
            raise RuntimeError(f"{len(packages)} GovInfo packages produced no turn files")
        return aggregate_turn_files(turn_files)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.reset:
        args.daily.unlink(missing_ok=True)
        LOG.info("cleared incremental aggregate state at %s", args.daily)
        return 0
    if not args.long_run.exists():
        raise SystemExit(f"missing canonical long-run payload: {args.long_run}")

    daily = load_daily(args.daily)
    window = resolve_window(args, daily)
    if window is None:
        LOG.info("nothing to do")
        return 0
    start, end = window
    if args.dry_run:
        LOG.info("dry run: would fetch %s..%s", start, end)
        return 0

    fresh = fetch_and_score(start, end, args.workers)
    if fresh is None:
        return 0
    merged = replace_daily_window(daily, fresh, start, end)
    save_daily(merged, args.daily)

    base = json.loads(args.long_run.read_text(encoding="utf-8"))
    payload = merge_long_run_payload(base, merged)
    args.long_run.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    LOG.info(
        "aggregate table now has %d daily rows through %s",
        len(merged),
        merged["date"].max() if not merged.empty else "no date",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
