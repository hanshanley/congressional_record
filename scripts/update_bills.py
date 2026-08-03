#!/usr/bin/env python3
"""Refresh canonical H.R. and S. bills from public GovInfo Bill Status XML.

With no arguments, this performs a listing-based incremental update of the
current Congress. During the first two weeks of an odd-numbered January it also
refreshes the outgoing Congress, whose final updates can arrive after rollover.
Use ``--full`` to replace one Congress from the official H.R. and S. ZIPs, or
``--backfill START`` to load every Congress from START through ``--congress``
from those ZIPs. No GovInfo API key or other secret is required.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.ingest.billstatus import MIN_CONGRESS, update_bill_status  # noqa: E402
from analysis.ingest.schema import congress_from_year  # noqa: E402

LOG = logging.getLogger("update_bills")
DEFAULT_OUTPUT = ROOT / "data" / "site" / "bills"
ROLLOVER_WINDOW_DAYS = 14


def current_congress(today: Optional[dt.date] = None) -> int:
    """Return the Congress containing today."""
    return congress_from_year((today or dt.date.today()).year)


def routine_congresses(today: Optional[dt.date] = None) -> tuple[int, ...]:
    """Return Congresses refreshed by the routine incremental update."""
    today = today or dt.date.today()
    current = current_congress(today)
    if (
        today.year % 2 == 1
        and today.month == 1
        and today.day <= ROLLOVER_WINDOW_DAYS
    ):
        return (current - 1, current)
    return (current,)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    today = dt.date.today()
    current = current_congress(today)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--congress",
        type=int,
        default=None,
        help=f"Congress to update, or backfill endpoint (default: {current}).",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Replace --congress from the official H.R. and S. ZIPs.",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        metavar="START_CONGRESS",
        help="Fully fetch START_CONGRESS through --congress (inclusive).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Canonical partition directory (default: data/site/bills).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Concurrent downloads (default: 8).",
    )
    args = parser.parse_args(argv)
    args.routine = (
        args.congress is None and not args.full and args.backfill is None
    )
    if args.congress is None:
        args.congress = current
    args.routine_congresses = routine_congresses(today) if args.routine else ()

    if args.congress < MIN_CONGRESS:
        parser.error(f"--congress must be at least {MIN_CONGRESS}")
    if args.congress > current:
        parser.error(f"--congress cannot exceed the current Congress ({current})")
    if args.workers <= 0:
        parser.error("--workers must be positive")
    if args.backfill is not None:
        if args.full:
            parser.error("--full and --backfill are mutually exclusive")
        if args.backfill < MIN_CONGRESS:
            parser.error(f"--backfill must be at least {MIN_CONGRESS}")
        if args.backfill > args.congress:
            parser.error("--backfill cannot exceed --congress")
    elif args.congress != current and not args.full:
        parser.error("historical Congresses require explicit --full mode")
    return args


def congresses_for(args: argparse.Namespace) -> tuple[int, ...]:
    """Resolve the validated target Congress sequence."""
    if args.backfill is not None:
        return tuple(range(args.backfill, args.congress + 1))
    if args.routine:
        return args.routine_congresses
    return (args.congress,)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    congresses = congresses_for(args)
    full = args.full or args.backfill is not None
    mode = "full" if full else "incremental"
    LOG.info(
        "%s GovInfo Bill Status update for Congress%s %s",
        mode,
        "es" if len(congresses) > 1 else "",
        ", ".join(str(value) for value in congresses),
    )
    try:
        result = update_bill_status(
            congresses,
            args.output,
            full=full,
            allow_missing_listings_for=(
                congresses[-1:]
                if args.routine and len(congresses) > 1
                else ()
            ),
            workers=args.workers,
        )
    except Exception as exc:  # noqa: BLE001 - CLI must return a clear nonzero status
        LOG.error("Bill Status update failed: %s", exc)
        return 1

    LOG.info(
        "discovered=%d selected=%d fetched=%d partitions_written=%d",
        result.discovered,
        result.selected,
        result.fetched,
        len(result.written),
    )
    for path in result.written:
        LOG.info("wrote %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
