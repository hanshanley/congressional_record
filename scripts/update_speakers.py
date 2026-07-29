#!/usr/bin/env python3
"""update_speakers.py -- extend the committed speaker table with newly published days.

This is the path CI uses. It deliberately does **not** need the ~6 GB turn corpus:
it reads the last date already present in ``data/site/speaker_daily/``,
downloads only the Congressional Record issues published since then into a
temporary directory, scores just those days, and merges the resulting counts back
into the committed table.

That keeps the repository state small enough to commit while still producing an
exact, append-only history -- which is what allows the site to update unattended.

Examples
--------
    python scripts/update_speakers.py
    python scripts/update_speakers.py --since 2026-01-01 --until 2026-01-31
    python scripts/update_speakers.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from analysis.speakers import (  # noqa: E402
    load_daily,
    merge_daily,
    save_daily,
    speaker_counts,
)

LOG = logging.getLogger("update_speakers")

DAILY_PATH = ROOT / "data" / "site" / "speaker_daily"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--daily", type=Path, default=DAILY_PATH, help="Speaker daily table.")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="Override the start date.")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="Override the end date.")
    p.add_argument("--workers", type=int, default=6, help="Parallel bulk downloads.")
    p.add_argument(
        "--lookback-days",
        type=int,
        default=3,
        help="Also re-fetch this many days before the last stored date, so issues "
             "published late are picked up (merging is idempotent). Default: 3.",
    )
    p.add_argument("--dry-run", action="store_true", help="Report the window and exit.")
    return p.parse_args(argv)


def resolve_window(args: argparse.Namespace, daily: Optional[pd.DataFrame]):
    """Return ``(start, end)`` for the fetch, or None when nothing is pending."""
    end = args.until or dt.date.today().isoformat()
    if args.since:
        return (args.since, end) if args.since <= end else None
    if daily is None or daily.empty:
        LOG.error(
            "no existing speaker table at %s; seed it once from the local corpus "
            "before relying on incremental updates",
            args.daily,
        )
        return None
    last = str(daily["date"].max())
    # Re-fetch a short tail: GovInfo occasionally publishes an issue a few days late,
    # and merge_daily replaces rather than duplicates any day we recompute.
    start = (
        dt.date.fromisoformat(last) - dt.timedelta(days=max(0, args.lookback_days))
    ).isoformat()
    return (start, end) if start <= end else None


def fetch_and_score(start: str, end: str, workers: int) -> pd.DataFrame:
    """Download the issues in ``[start, end]`` to a temp dir and score them.

    Package discovery probes the public bulk URLs rather than the GovInfo API, so
    the scheduled update needs no API key and no repository secret at all.
    """
    from analysis.ingest.govinfo_bulk import probe_packages, run_bulk

    packages = probe_packages(start, end, workers=workers)
    if not packages:
        LOG.info("no issues published in %s..%s", start, end)
        return pd.DataFrame()

    LOG.info("fetching %d issue(s) %s..%s", len(packages), start, end)
    with tempfile.TemporaryDirectory(prefix="crec-speakers-") as tmp:
        tmp_path = Path(tmp)
        run_bulk(packages, tmp_path / "bulk", tmp_path, workers=workers)
        turn_files = sorted((tmp_path / "turns").glob("*.parquet"))
        if not turn_files:
            LOG.warning("no turns parsed from %d package(s)", len(packages))
            return pd.DataFrame()
        counts = speaker_counts(turn_files)
    LOG.info("scored %d member-day rows", len(counts))
    return counts


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

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
    if fresh.empty:
        LOG.info("no new rows; speaker table unchanged")
        return 0

    merged = merge_daily(daily, fresh)
    before = 0 if daily is None else len(daily)
    save_daily(merged, args.daily)
    LOG.info(
        "speaker table %d -> %d rows; now covers %s..%s",
        before, len(merged), merged["date"].min(), merged["date"].max(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
