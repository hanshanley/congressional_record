#!/usr/bin/env python3
"""Generate publication graphs for the ten most-used swear-word families."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.plotting import term_charts  # noqa: E402
from analysis.speakers import load_daily  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily", type=Path, default=ROOT / "data/site/speaker_daily")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/figures/profanity_terms")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    daily = load_daily(args.daily)
    if daily is None or daily.empty:
        parser.error(f"no speaker daily data at {args.daily}")
    if args.top <= 0:
        parser.error("--top must be positive")
    term_charts.render(daily, args.out, top=args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
