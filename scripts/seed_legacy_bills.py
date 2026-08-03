#!/usr/bin/env python3
"""Seed Congresses 103-107 H.R. and S. bills from the Congress.gov API."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.ingest.congress_api import (  # noqa: E402
    DEFAULT_BILL_TYPES,
    CongressAPIClient,
    CongressAPIError,
    seed_legacy_bills,
)

LOG = logging.getLogger("seed_legacy_bills")
DEFAULT_OUTPUT = ROOT / "data" / "site" / "bills"
DEFAULT_STATE = ROOT / "data" / "site" / "bills_seed_state.json"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-key",
        help="Congress.gov API key (default: CONGRESS_API_KEY, falling back to "
        "GOVINFO_API_KEY; .env is loaded when python-dotenv is available).",
    )
    parser.add_argument("--congress-start", type=int, default=103)
    parser.add_argument("--congress-end", type=int, default=107)
    parser.add_argument(
        "--bill-type",
        action="append",
        choices=("HR", "S", "hr", "s"),
        dest="bill_types",
        help="Measure type to seed; repeat as needed (default: HR and S).",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE,
        help="Resume checkpoint containing completed bill IDs.",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--request-interval", type=float, default=0.1)
    parser.add_argument(
        "--list-only",
        action="store_true",
        help="Enumerate matching bill IDs without fetching details or writing files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selected range and paths without contacting Congress.gov.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.congress_start <= 0 or args.congress_end < args.congress_start:
        raise SystemExit("invalid Congress range")
    congresses = tuple(range(args.congress_start, args.congress_end + 1))
    bill_types = tuple(args.bill_types or DEFAULT_BILL_TYPES)
    if args.dry_run:
        LOG.info(
            "dry run: Congresses %d-%d, types %s, output=%s, state=%s",
            congresses[0],
            congresses[-1],
            ",".join(value.upper() for value in bill_types),
            args.output,
            args.state,
        )
        return 0

    try:
        client = CongressAPIClient(
            args.api_key,
            timeout=args.timeout,
            max_retries=args.max_retries,
            min_interval=args.request_interval,
        )
        result = seed_legacy_bills(
            client,
            output_path=args.output,
            state_path=args.state,
            congresses=congresses,
            bill_types=bill_types,
            batch_size=args.batch_size,
            list_only=args.list_only,
        )
    except (CongressAPIError, ValueError) as exc:
        LOG.error("%s", exc)
        return 1
    LOG.info(
        "listed=%d fetched=%d resumed=%d partitions_changed=%d",
        result.listed,
        result.fetched,
        result.skipped,
        len(result.written),
    )
    if args.list_only:
        for identity in result.bill_ids:
            print(identity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
