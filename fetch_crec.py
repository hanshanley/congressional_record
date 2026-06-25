#!/usr/bin/env python3
"""fetch_crec.py -- download Congressional Record transcripts from GovInfo.

Pulls speech/section-level granules from the GovInfo ``CREC`` collection and
stores each as a plain-text transcript plus its MODS metadata, indexed in
``data/manifest.jsonl``.

Examples
--------
    # Validate on a single month (uses GOVINFO_API_KEY, or DEMO_KEY if unset)
    python fetch_crec.py --sample-month 2024-01

    # Full backfill of the digital collection (long-running, resumable)
    python fetch_crec.py --start 1994-01-01 --end 2024-12-31 --min-interval 0.5

    # Only House + Extensions for one year
    python fetch_crec.py --start 2023-01-01 --end 2023-12-31 --classes HOUSE EXTENSIONS
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Optional, Set

from tqdm import tqdm

from crec.api import GovInfoClient, GovInfoError, RetryableError
from crec.download import download_granule, granule_paths
from crec.enumerate import daterange_months, iter_granules, iter_packages

LOG = logging.getLogger("crec")

DEFAULT_OUT = Path(__file__).parent / "data"
ALL_CLASSES = ["HOUSE", "SENATE", "EXTENSIONS", "DAILYDIGEST", "FRONTMATTER"]
# Abort cleanly if this many granules in a row fail with rate limiting.
RATE_LIMIT_GIVE_UP = 5


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", help="Start date YYYY-MM-DD (inclusive).")
    p.add_argument("--end", help="End date YYYY-MM-DD (inclusive). Defaults to today.")
    p.add_argument(
        "--sample-month",
        metavar="YYYY-MM",
        help="Convenience: download just one calendar month (overrides --start/--end).",
    )
    p.add_argument(
        "--classes",
        nargs="+",
        default=None,
        metavar="CLASS",
        help=f"granuleClass values to keep. Default: all ({', '.join(ALL_CLASSES)}).",
    )
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, help="Output directory.")
    p.add_argument("--api-key", default=None, help="GovInfo API key (else GOVINFO_API_KEY env).")
    p.add_argument(
        "--min-interval",
        type=float,
        default=0.0,
        help="Minimum seconds between API requests (throttle for the ~1000/hr limit).",
    )
    p.add_argument("--limit", type=int, default=None, help="Stop after N new granules (testing).")
    p.add_argument("--overwrite", action="store_true", help="Re-download granules already on disk.")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return p.parse_args(argv)


def resolve_dates(args: argparse.Namespace) -> tuple[str, str]:
    if args.sample_month:
        start = dt.date.fromisoformat(args.sample_month + "-01")
        if start.month == 12:
            nxt = start.replace(year=start.year + 1, month=1)
        else:
            nxt = start.replace(month=start.month + 1)
        end = nxt - dt.timedelta(days=1)
        return start.isoformat(), end.isoformat()
    if not args.start:
        raise SystemExit("error: provide --start (and optionally --end) or --sample-month")
    end = args.end or dt.date.today().isoformat()
    return args.start, end


def load_processed(manifest_path: Path) -> Set[str]:
    """Return the set of granuleIds already recorded in the manifest."""
    done: Set[str] = set()
    if not manifest_path.exists():
        return done
    with manifest_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line)["granuleId"])
            except (json.JSONDecodeError, KeyError):
                continue
    return done


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    start, end = resolve_dates(args)
    classes = [c.upper() for c in args.classes] if args.classes else None

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.jsonl"

    client = GovInfoClient(api_key=args.api_key, min_interval=args.min_interval)
    using_demo = client.api_key == "DEMO_KEY"
    LOG.info(
        "CREC fetch %s -> %s | classes=%s | key=%s",
        start, end, classes or "ALL", "DEMO_KEY (rate-limited)" if using_demo else "configured",
    )

    processed = load_processed(manifest_path)
    LOG.info("%d granules already in manifest; resuming.", len(processed))

    new_count = 0
    skip_count = 0
    fail_count = 0
    pkg_count = 0
    consecutive_rate_limits = 0

    manifest_fh = manifest_path.open("a", encoding="utf-8")
    try:
        for month_start, month_end in daterange_months(start, end):
            packages = list(iter_packages(client, month_start, month_end))
            if not packages:
                continue
            for pkg in tqdm(packages, desc=f"{month_start[:7]}", unit="day"):
                package_id = pkg["packageId"]
                pkg_count += 1
                for granule in iter_granules(client, package_id, classes=classes):
                    gid = granule["granuleId"]
                    if not args.overwrite and gid in processed:
                        skip_count += 1
                        continue
                    try:
                        row = download_granule(
                            client, package_id, granule, out_dir, overwrite=args.overwrite
                        )
                        consecutive_rate_limits = 0
                    except RetryableError:
                        consecutive_rate_limits += 1
                        if consecutive_rate_limits >= RATE_LIMIT_GIVE_UP:
                            raise  # surface to the clean handler below
                        LOG.warning("granule %s rate-limited; continuing.", gid)
                        fail_count += 1
                        continue
                    except GovInfoError as exc:
                        LOG.warning("granule %s failed: %s", gid, exc)
                        fail_count += 1
                        continue
                    except Exception as exc:  # noqa: BLE001 - isolate per-granule failures
                        LOG.warning("granule %s unexpected error: %s", gid, exc)
                        fail_count += 1
                        continue

                    if row is None:  # files already on disk (manifest lost)
                        # Re-read to backfill manifest deterministically.
                        paths = granule_paths(out_dir, package_id, gid)
                        row = {
                            "granuleId": gid,
                            "packageId": package_id,
                            "granuleClass": granule.get("granuleClass"),
                            "title": granule.get("title"),
                            "dateIssued": granule.get("dateIssued"),
                            "txt_path": str(paths["txt"].relative_to(out_dir)),
                            "mods_path": str(paths["mods"].relative_to(out_dir)),
                            "backfilled": True,
                        }
                        skip_count += 1
                    else:
                        new_count += 1

                    manifest_fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    manifest_fh.flush()
                    processed.add(gid)

                    if args.limit and new_count >= args.limit:
                        LOG.info("Reached --limit %d new granules; stopping.", args.limit)
                        return _summary(pkg_count, new_count, skip_count, fail_count, manifest_path)
    except KeyboardInterrupt:
        LOG.warning("Interrupted; progress is saved. Re-run the same command to resume.")
        _summary(pkg_count, new_count, skip_count, fail_count, manifest_path)
        return 130
    except RetryableError as exc:
        LOG.error(
            "Giving up after repeated rate limiting: %s\n"
            "DEMO_KEY allows only ~50 requests/day. Set a free key in GOVINFO_API_KEY "
            "(https://api.data.gov/signup/) and re-run -- progress is saved and will resume.",
            exc,
        )
        _summary(pkg_count, new_count, skip_count, fail_count, manifest_path)
        return 2
    finally:
        manifest_fh.close()

    return _summary(pkg_count, new_count, skip_count, fail_count, manifest_path)


def _summary(pkg_count: int, new: int, skip: int, fail: int, manifest_path: Path) -> int:
    LOG.info(
        "Done. packages=%d new=%d skipped=%d failed=%d | manifest=%s",
        pkg_count, new, skip, fail, manifest_path,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
