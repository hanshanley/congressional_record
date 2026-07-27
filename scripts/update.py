#!/usr/bin/env python3
"""update.py -- bring the corpus and every derived artifact up to date.

One command for the whole refresh cycle:

1. **ingest**    -- enumerate CREC issues published since the corpus's newest turn
                    and bulk-download/parse only those days.
2. **aggregate** -- rescore only the shards that changed (see
                    ``analysis/incremental.py``) and rewrite the metrics tables.
3. **viz**       -- re-render the figures from the refreshed metrics.
4. **status**    -- print the resulting coverage so the run is self-verifying.

Every step is incremental and safe to re-run: if nothing new has been published,
the ingest is skipped, the aggregate is served entirely from cache, and only the
figures are re-rendered.

Examples
--------
    python scripts/update.py                 # the normal weekly refresh
    python scripts/update.py --dry-run       # show what would happen
    python scripts/update.py --since 2026-01-01
    python scripts/update.py --skip-viz
    python scripts/update.py --full          # force a complete rescore
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
INTERIM = DATA / "interim"
PROCESSED = DATA / "processed"
OUTPUTS = ROOT / "outputs"

LOG = logging.getLogger("update")


def _load_coverage_status():
    """Import the sibling coverage_status script (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "coverage_status", Path(__file__).resolve().parent / "coverage_status.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Ingest from this date instead of the day after the newest turn.",
    )
    p.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        help="Ingest up to this date (default: today).",
    )
    p.add_argument("--workers", type=int, default=8, help="Parallel bulk downloads (default: 8).")
    p.add_argument("--skip-ingest", action="store_true", help="Do not fetch new issues.")
    p.add_argument("--skip-aggregate", action="store_true", help="Do not rescore metrics.")
    p.add_argument("--skip-viz", action="store_true", help="Do not re-render figures.")
    p.add_argument(
        "--full",
        action="store_true",
        help="Rescore every shard, ignoring the aggregate cache.",
    )
    p.add_argument(
        "--sentiment", action="store_true", help="Also compute VADER sentiment (slower)."
    )
    p.add_argument(
        "--include-procedural", action="store_true", help="Keep procedural/chair turns."
    )
    p.add_argument("--dry-run", action="store_true", help="Report the plan and exit.")
    return p.parse_args(argv)


def corpus_latest_date(status_module) -> Optional[str]:
    """Newest turn date in the analysis corpus, or None if it is empty."""
    bulk = status_module._scan_turns(status_module.TURNS_DIR)
    return status_module.bulk_latest(bulk)


def resolve_window(args, status_module) -> Optional[tuple[str, str]]:
    """Return the (start, end) ingest window, or None when nothing is pending."""
    end = args.until or dt.date.today().isoformat()
    if args.since:
        start = args.since
    else:
        latest = corpus_latest_date(status_module)
        if latest is None:
            LOG.error(
                "the analysis corpus is empty; run an initial ingest "
                "(python -m analysis.run ingest-govinfo-bulk) before using this script"
            )
            return None
        start = (dt.date.fromisoformat(latest) + dt.timedelta(days=1)).isoformat()
    if start > end:
        return None
    return start, end


def step_ingest(start: str, end: str, workers: int) -> int:
    """Download and parse every CREC issue published in [start, end]."""
    from crec.api import GovInfoClient
    from crec.enumerate import iter_packages
    from analysis.ingest.govinfo_bulk import run_bulk

    client = GovInfoClient(min_interval=0.0)
    packages = sorted({p["packageId"] for p in iter_packages(client, start, end)})
    if not packages:
        LOG.info("no CREC issues published in %s..%s; nothing to ingest", start, end)
        return 0
    LOG.info("ingesting %d CREC issue(s) %s..%s", len(packages), start, end)
    turns = run_bulk(packages, DATA / "bulk", INTERIM, workers=workers)
    LOG.info("ingest added %d new turns", turns)
    return turns


def step_aggregate(args) -> int:
    from analysis.aggregate import score_and_aggregate

    frame = score_and_aggregate(
        INTERIM / "turns",
        PROCESSED,
        use_sentiment=args.sentiment,
        include_procedural=args.include_procedural,
        incremental=not args.full,
    )
    LOG.info("metrics rows: %d", len(frame))
    return len(frame)


def step_viz() -> None:
    import matplotlib

    matplotlib.use("Agg")
    from analysis.viz import render

    render(PROCESSED / "metrics" / "civility_metrics.parquet", OUTPUTS)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    status_module = _load_coverage_status()

    before = corpus_latest_date(status_module)
    LOG.info("analysis corpus currently ends at %s", before or "(empty)")

    window = None if args.skip_ingest else resolve_window(args, status_module)
    if not args.skip_ingest and window is None and not args.since:
        LOG.info("corpus is already current; no ingest needed")

    if args.dry_run:
        LOG.info(
            "dry run: ingest=%s aggregate=%s viz=%s",
            f"{window[0]}..{window[1]}" if window else "skip",
            "skip" if args.skip_aggregate else ("full" if args.full else "incremental"),
            "skip" if args.skip_viz else "render",
        )
        return 0

    started = time.monotonic()
    if window:
        step_ingest(window[0], window[1], args.workers)

    if not args.skip_aggregate:
        step_aggregate(args)
    else:
        LOG.info("skipping aggregate")

    if not args.skip_viz:
        step_viz()
    else:
        LOG.info("skipping viz")

    report = status_module.build_report(gap_days=30)
    status_module.STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    status_module.STATUS_PATH.write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    print(status_module.render(report))

    after = report["analysis_corpus_latest_date"]
    LOG.info(
        "update complete in %.1fs: corpus %s -> %s",
        time.monotonic() - started, before or "(empty)", after,
    )
    for warning in report["warnings"]:
        LOG.warning("%s", warning)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
