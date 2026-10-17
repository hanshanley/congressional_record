#!/usr/bin/env python3
"""Retain complete Stanford originals before parallel source audits and scoring."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NORMALIZERS = (
    "analysis/ingest/hein.py", "analysis/ingest/schema.py", "analysis/normalize/parties.py",
)


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare(turns, out):
    sources = {}
    for path in sorted(turns.glob("hein_*.parquet")):
        scope = pq.read_table(path, columns=["congress", "source"]).to_pydict()
        congresses, editions = set(scope["congress"]), set(scope["source"])
        if len(congresses) != 1:
            raise ValueError(f"Historical input is not a complete single-Congress partition: {path}")
        congress = congresses.pop()
        expected = "hein_bound" if congress < 97 else "hein_daily"
        if not 43 <= congress <= 114 or editions != {expected} or congress in sources:
            raise ValueError(f"Unexpected or duplicate Stanford source partition: {path}")
        sources[congress] = (path, expected)
    if set(sources) != set(range(43, 115)):
        raise ValueError(f"Incomplete original Stanford coverage: {sorted(set(range(43, 115)) - sources.keys())}")
    if out.exists():
        raise ValueError(f"Refusing to overwrite retained source batches: {out}")
    normalizers = {name: checksum(ROOT / name) for name in NORMALIZERS}
    index = {}
    for first in range(43, 115, 12):
        label = f"{first}-{first + 11}"
        directory = out / label
        (directory / "turns").mkdir(parents=True)
        records = {}
        for congress in range(first, first + 12):
            path, edition = sources[congress]
            os.link(path, directory / "turns" / path.name)
            records[path.name] = {
                "congress": congress, "source": edition,
                "rows": pq.ParquetFile(path).metadata.num_rows, "sha256": checksum(path),
            }
        manifest = {
            "kind": "complete_original_stanford_partitions",
            "producer_commit": os.environ.get("GITHUB_SHA"),
            "congresses": list(range(first, first + 12)),
            "normalizers": normalizers, "files": records,
        }
        (directory / "source_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        index[label] = {"rows": sum(row["rows"] for row in records.values())}
    return index


def validate_batch(directory):
    manifest = json.loads((directory / "source_manifest.json").read_text())
    if manifest.get("kind") != "complete_original_stanford_partitions":
        raise ValueError("Unrecognized retained historical input")
    for name in NORMALIZERS:
        if manifest["normalizers"].get(name) != checksum(ROOT / name):
            raise ValueError(f"Historical normalization changed after source retention: {name}")
    files = {path.name: path for path in (directory / "turns").glob("*.parquet")}
    if files.keys() != manifest["files"].keys():
        raise ValueError("Retained source files differ from the original manifest")
    congresses = []
    for name, metadata in manifest["files"].items():
        path = files[name]
        if checksum(path) != metadata["sha256"] or pq.ParquetFile(path).metadata.num_rows != metadata["rows"]:
            raise ValueError(f"Retained original source changed: {name}")
        congresses.append(metadata["congress"])
    if sorted(congresses) != manifest["congresses"]:
        raise ValueError("Retained historical Congress coverage is incomplete")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(prepare(args.turns, args.out), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
