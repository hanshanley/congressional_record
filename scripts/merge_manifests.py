#!/usr/bin/env python3
"""Merge parallel-worker manifests into the main manifest, deduped by granuleId.

Prefers a full (non-backfilled) row when the same granule appears more than once.
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MAIN = DATA / "manifest.jsonl"


def _load(path: Path, rows: dict) -> int:
    n = 0
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            gid = r.get("granuleId")
            if not gid:
                continue
            prev = rows.get(gid)
            # Prefer a non-backfilled (full) row over a backfilled one.
            if prev is None or (prev.get("backfilled") and not r.get("backfilled")):
                rows[gid] = r
            n += 1
    return n


def main() -> int:
    rows: dict = {}
    total = _load(MAIN, rows)
    for wf in sorted(glob.glob(str(DATA / "manifest_w*.jsonl"))):
        total += _load(Path(wf), rows)
    tmp = MAIN.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as out:
        for gid in sorted(rows):
            out.write(json.dumps(rows[gid], ensure_ascii=False) + "\n")
    tmp.replace(MAIN)
    print(f"merged {total} rows -> {len(rows)} unique granules in {MAIN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
