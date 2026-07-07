#!/usr/bin/env python3
"""Orchestrate the congressional-comity analysis pipeline.

Subcommands:
    ingest-hein     Parse the Stanford hein zips into unified turn parquet.
    ingest-govinfo  Segment downloaded GovInfo CREC granules into turn parquet.
    aggregate       Score all turns and write the civility metrics table.
    viz             Render charts from the metrics table.
    all             ingest-hein -> aggregate -> viz.

Examples
--------
    python -m analysis.run ingest-hein --congresses 097 104 114
    python -m analysis.run aggregate --sentiment
    python -m analysis.run viz
    python -m analysis.run all
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

LOG = logging.getLogger("analysis.run")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
INTERIM = DATA / "interim"
PROCESSED = DATA / "processed"


def _p(args) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_ingest_hein(args) -> int:
    from analysis.ingest.hein import ingest_hein

    # Prefer the ditto-extracted bound directory (zip64 the stdlib can't read) if present.
    bound_dir = RAW / "hein-bound"
    bound_zip = RAW / "hein-bound.zip"
    bound = bound_dir if bound_dir.is_dir() else (bound_zip if bound_zip.exists() else None)
    daily = RAW / "hein-daily.zip" if (RAW / "hein-daily.zip").exists() else None

    counts = ingest_hein(bound, daily, INTERIM, congresses=args.congresses)
    LOG.info("ingested %d congresses; %d total turns", len(counts), sum(counts.values()))
    return 0


def cmd_ingest_govinfo(args) -> int:
    from analysis.ingest.govinfo import ingest_govinfo

    n = ingest_govinfo(DATA / "manifest.jsonl", DATA, INTERIM)
    LOG.info("ingested %d GovInfo turns", n)
    return 0


def cmd_ingest_govinfo_bulk(args) -> int:
    """Fast GovInfo ingest via whole-day package zips (no API rate limit)."""
    from crec.api import GovInfoClient
    from crec.enumerate import iter_packages
    from analysis.ingest.govinfo_bulk import run_bulk

    client = GovInfoClient(min_interval=0.0)
    pkgs = [p["packageId"] for p in iter_packages(client, args.start, args.end)]
    LOG.info("enumerated %d CREC packages %s..%s", len(pkgs), args.start, args.end)
    n = run_bulk(pkgs, DATA / "bulk", INTERIM, workers=args.workers)
    LOG.info("bulk-ingested %d GovInfo turns", n)
    return 0


def cmd_aggregate(args) -> int:
    from analysis.aggregate import score_and_aggregate

    df = score_and_aggregate(
        INTERIM / "turns", PROCESSED,
        use_sentiment=args.sentiment,
        include_procedural=args.include_procedural,
    )
    LOG.info("metrics rows: %d", len(df))
    return 0


def cmd_viz(args) -> int:
    from analysis.viz import render

    render(PROCESSED / "metrics" / "civility_metrics.parquet", DATA)
    return 0


def cmd_all(args) -> int:
    cmd_ingest_hein(args)
    cmd_aggregate(args)
    cmd_viz(args)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ph = sub.add_parser("ingest-hein")
    ph.add_argument("--congresses", nargs="+", default=None, help="e.g. 097 104 114 (default: all).")
    ph.set_defaults(func=cmd_ingest_hein)

    pg = sub.add_parser("ingest-govinfo")
    pg.set_defaults(func=cmd_ingest_govinfo)

    pgb = sub.add_parser("ingest-govinfo-bulk", help="Fast ingest via day-zips (no API rate limit).")
    pgb.add_argument("--start", default="2017-01-01")
    pgb.add_argument("--end", default="2026-12-31")
    pgb.add_argument("--workers", type=int, default=12)
    pgb.set_defaults(func=cmd_ingest_govinfo_bulk)

    pa = sub.add_parser("aggregate")
    pa.add_argument("--sentiment", action="store_true", help="Also compute VADER sentiment (slower).")
    pa.add_argument("--include-procedural", action="store_true", help="Keep procedural/chair turns.")
    pa.set_defaults(func=cmd_aggregate)

    pv = sub.add_parser("viz")
    pv.set_defaults(func=cmd_viz)

    pall = sub.add_parser("all")
    pall.add_argument("--congresses", nargs="+", default=None)
    pall.add_argument("--sentiment", action="store_true")
    pall.add_argument("--include-procedural", action="store_true")
    pall.set_defaults(func=cmd_all)

    args = ap.parse_args(argv)
    _p(args)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
