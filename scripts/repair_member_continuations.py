#!/usr/bin/env python3
"""Repair source-proven unmarked floor continuations in complete turn parquets."""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
import sys
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

import pyarrow as pa
import pyarrow.parquet as pq
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis.ingest.govinfo import (  # noqa: E402
    NON_SPOKEN_SUBCLASSES,
    has_initial_floor_address,
    identify_unmarked_floor_continuation,
)
from analysis.ingest.govinfo_bulk import (  # noqa: E402
    _TurnDeduplicator,
    _ordered_granules,
    parse_package_mods,
)
from analysis.ingest.schema import ARROW_SCHEMA, normalize_chamber  # noqa: E402
from analysis.normalize.parties import normalize_party  # noqa: E402

_PACKAGE_RE = re.compile(r"^CREC-\d{4}-\d{2}-\d{2}$")
_OWN_FRAGMENT_RE = re.compile(r"^0(?:-resumed-\d+)?$")
_METADATA_URL = "https://www.govinfo.gov/metadata/pkg/{package}/mods.xml"
_RETRY_DELAYS = (1.0, 3.0)


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _summary_path(audit: Path) -> Path:
    direct = audit / "summary.json"
    if direct.is_file():
        return direct
    candidates = [
        path for path in audit.glob("member-*/summary.json")
        if path.parent.name != "aggregate"
    ]
    if len(candidates) != 1:
        raise ValueError(f"Expected exactly one member audit summary under {audit}")
    return candidates[0]


def _turn_parts(turn_id: str) -> Optional[Tuple[str, str, str]]:
    if not turn_id.startswith("crec:") or "#" not in turn_id:
        return None
    gid, fragment = turn_id.removeprefix("crec:").split("#", 1)
    package = gid[:15]
    if not _PACKAGE_RE.fullmatch(package) or not gid.startswith(package + "-"):
        return None
    return package, gid, fragment


def _iter_rows(files: Iterable[Path]) -> Iterator[Tuple[Path, Dict[str, Any]]]:
    for path in files:
        parquet = pq.ParquetFile(path)
        if parquet.schema_arrow != ARROW_SCHEMA:
            raise ValueError(f"Unexpected normalized-turn schema: {path}")
        for batch in parquet.iter_batches(batch_size=5_000):
            for row in batch.to_pylist():
                yield path, row


def _validate_source(
    files: List[Path], summary: Dict[str, Any],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str], int]:
    if summary.get("mode") != "members":
        raise ValueError("Continuation repair requires a member-source audit")
    congress = summary.get("congress")
    expected = summary.get("turns_checked")
    if not isinstance(congress, int) or congress <= 0:
        raise ValueError("Audit summary has no valid Congress")
    if not isinstance(expected, int) or expected <= 0:
        raise ValueError("Audit summary has no nonzero turns_checked count")
    if summary.get("scorer_disagreements"):
        raise ValueError("Continuation repair requires a disagreement-free source audit")

    candidates: Dict[str, Dict[str, Any]] = {}
    ids = set()
    counts = Counter()
    input_hashes = {path.name: checksum(path) for path in files}
    for _, row in _iter_rows(files):
        counts[row["congress"]] += 1
        if row["congress"] != congress:
            continue
        turn_id = row["turn_id"]
        if turn_id in ids:
            raise ValueError(f"Complete source contains duplicate turn identity: {turn_id}")
        ids.add(turn_id)
        parts = _turn_parts(turn_id)
        if (
            parts
            and parts[2] == "0"
            and not row["is_procedural"]
            and not (row["bioguide"] or "").strip()
            and not (row["speaker_name"] or "").strip()
            and has_initial_floor_address(row["text"], row["chamber"])
        ):
            candidates[parts[1]] = {
                "package": parts[0],
                "turn_id": turn_id,
                "text": row["text"],
                "chamber": row["chamber"],
            }
    if sum(counts.values()) == 0:
        raise ValueError("Complete normalized source is empty")
    if counts != Counter({congress: expected}) or len(ids) != expected:
        raise ValueError(
            "Normalized source is incomplete or not scoped to its audit: "
            f"rows_by_congress={dict(counts)}, turns_checked={expected}"
        )
    return candidates, input_hashes, expected


def _download_metadata(package: str, cache: Path) -> Path:
    target = cache / f"{package}.mods.xml"
    if target.exists():
        if not target.is_file() or not target.stat().st_size:
            raise ValueError(f"Cached metadata is empty or invalid: {target}")
        return target

    url = _METADATA_URL.format(package=package)
    error = ""
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            response = requests.get(
                url,
                timeout=(10.0, 45.0),
                headers={"User-Agent": "congressional-record-continuation-repair/1.0"},
            )
            if response.status_code == 200 and response.content:
                if not parse_package_mods(response.content):
                    raise RuntimeError(f"GovInfo returned invalid package MODS for {package}")
                partial = target.with_name(target.name + ".partial")
                partial.write_bytes(response.content)
                partial.replace(target)
                return target
            error = f"HTTP {response.status_code}"
            if response.status_code != 429 and response.status_code < 500:
                break
        except requests.RequestException as exc:
            error = f"{type(exc).__name__}: {exc}"
        if attempt < len(_RETRY_DELAYS):
            time.sleep(_RETRY_DELAYS[attempt])
    raise RuntimeError(f"Unable to fetch GovInfo metadata for {package}: {error}")


def _load_metadata(
    packages: Iterable[str], cache: Path, workers: int,
) -> Tuple[Dict[str, Dict[str, Dict[str, Any]]], Dict[str, str]]:
    cache.mkdir(parents=True, exist_ok=True)
    package_list = sorted(set(packages))
    paths: Dict[str, Path] = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_download_metadata, package, cache): package
            for package in package_list
        }
        for future in cf.as_completed(futures):
            package = futures[future]
            paths[package] = future.result()

    metadata = {}
    hashes = {}
    for package in package_list:
        path = paths[package]
        parsed = parse_package_mods(path.read_bytes())
        if not parsed:
            raise ValueError(f"Cached package metadata cannot be parsed: {path}")
        metadata[package] = parsed
        hashes[path.name] = checksum(path)
    return metadata, hashes


def _resolve_candidates(
    candidates: Dict[str, Dict[str, Any]],
    metadata: Dict[str, Dict[str, Dict[str, Any]]],
) -> Tuple[Dict[str, Dict[str, str]], List[Dict[str, str]]]:
    resolved = {}
    unresolved = []
    for gid, candidate in sorted(candidates.items()):
        package = candidate["package"]
        package_metadata = metadata[package]
        if gid not in package_metadata:
            raise ValueError(f"Requested granule is absent from package MODS: {gid}")
        granule = package_metadata[gid]
        metadata_chamber = normalize_chamber(
            granule.get("granuleClass") or granule.get("chamber")
        )
        if metadata_chamber != candidate["chamber"]:
            reason = "metadata_chamber_mismatch"
            member = None
        else:
            member, reason = identify_unmarked_floor_continuation(
                candidate["text"],
                granule.get("members", []),
                candidate["chamber"],
                non_spoken=granule.get("subGranuleClass") in NON_SPOKEN_SUBCLASSES,
            )
        if member:
            resolved[gid] = member
        else:
            unresolved.append({
                "package": package,
                "granule_id": gid,
                "turn_id": candidate["turn_id"],
                "reason": reason,
            })
    return resolved, unresolved


def _changed_row(
    row: Dict[str, Any],
    resolved: Dict[str, Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    parts = _turn_parts(row["turn_id"])
    if (
        not parts
        or parts[1] not in resolved
        or not _OWN_FRAGMENT_RE.fullmatch(parts[2])
        or row["is_procedural"]
        or (row["bioguide"] or "").strip()
        or (row["speaker_name"] or "").strip()
    ):
        return None
    member = resolved[parts[1]]
    changed = dict(row)
    changed["bioguide"] = member["bioguide"]
    changed["party"] = normalize_party(member.get("party"))
    changed["state"] = member.get("state", "")
    changed["speaker_name"] = member.get("parsed_name") or member.get("name", "")
    return changed


def _find_kept_provenance(
    row: Dict[str, Any],
    page_range: Tuple[str, int, int],
    kept: List[Tuple[Dict[str, Any], Tuple[str, int, int]]],
) -> Optional[Tuple[Dict[str, Any], Tuple[str, int, int]]]:
    for previous, previous_range in reversed(kept):
        probe = _TurnDeduplicator()
        probe.duplicate(previous, previous_range)
        if probe.duplicate(row, page_range):
            return previous, previous_range
    return None


def _prepare_changes(
    files: List[Path],
    resolved: Dict[str, Dict[str, str]],
    metadata: Dict[str, Dict[str, Dict[str, Any]]],
) -> Tuple[Dict[str, Dict[str, Any]], set, List[Dict[str, Any]], List[Dict[str, Any]]]:
    packages = {gid[:15] for gid in resolved}
    package_rows: Dict[str, List[Tuple[int, Dict[str, Any]]]] = defaultdict(list)
    ordinal = 0
    for _, row in _iter_rows(files):
        parts = _turn_parts(row["turn_id"])
        if parts and parts[0] in packages:
            package_rows[parts[0]].append((ordinal, row))
        ordinal += 1

    changed_rows: Dict[str, Dict[str, Any]] = {}
    changes = []
    removals = set()
    removal_proof = []
    for package, indexed_rows in sorted(package_rows.items()):
        by_granule: Dict[str, List[Tuple[int, Dict[str, Any]]]] = defaultdict(list)
        for index, original in indexed_rows:
            parts = _turn_parts(original["turn_id"])
            assert parts is not None
            changed = _changed_row(original, resolved)
            row = changed or original
            if changed:
                changed_rows[row["turn_id"]] = row
                changes.append({
                    "turn_id": row["turn_id"],
                    "package": package,
                    "granule_id": parts[1],
                    "before": {
                        field: original[field]
                        for field in ("speaker_name", "bioguide", "party", "state")
                    },
                    "after": {
                        field: row[field]
                        for field in ("speaker_name", "bioguide", "party", "state")
                    },
                })
            by_granule[parts[1]].append((index, row))

        package_metadata = metadata[package]
        ranged = {
            gid: package_metadata[gid]
            for gid in by_granule
            if gid in package_metadata and package_metadata[gid].get("page_range")
        }
        ordered = [gid for gid, _ in _ordered_granules(ranged)]
        deduplicator = _TurnDeduplicator()
        kept: List[Tuple[Dict[str, Any], Tuple[str, int, int]]] = []
        for gid in ordered:
            page_range = package_metadata[gid]["page_range"]
            for _, row in sorted(by_granule[gid], key=lambda item: item[0]):
                if not deduplicator.duplicate(row, page_range):
                    kept.append((row, page_range))
                    continue
                provenance = _find_kept_provenance(row, page_range, kept)
                if provenance is None:
                    raise AssertionError(f"Strict deduplicator lost provenance for {row['turn_id']}")
                previous, previous_range = provenance
                if (
                    row["turn_id"] not in changed_rows
                    and previous["turn_id"] not in changed_rows
                ):
                    raise ValueError(
                        "Continuation replay found an unrelated pre-existing duplicate: "
                        f"{row['turn_id']}"
                    )
                removals.add(row["turn_id"])
                removal_proof.append({
                    "removed_turn_id": row["turn_id"],
                    "removed_page_range": list(page_range),
                    "kept_provenance": {
                        "turn_id": previous["turn_id"],
                        "page_range": list(previous_range),
                    },
                })
    return changed_rows, removals, changes, removal_proof


def _text_digest(rows: Iterable[Dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update((row["turn_id"] or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update((row["text"] or "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _write_outputs(
    files: List[Path],
    turns: Path,
    out: Path,
    changed_rows: Dict[str, Dict[str, Any]],
    removals: set,
) -> Tuple[Dict[str, int], str]:
    counts = {}
    source_text = hashlib.sha256()
    for path in files:
        relative = path.relative_to(turns)
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        writer = pq.ParquetWriter(target, ARROW_SCHEMA, compression="zstd")
        count = 0
        try:
            for batch in pq.ParquetFile(path).iter_batches(batch_size=5_000):
                rows = []
                for original in batch.to_pylist():
                    turn_id = original["turn_id"]
                    if turn_id in removals:
                        continue
                    row = changed_rows.get(turn_id, original)
                    source_text.update(turn_id.encode("utf-8"))
                    source_text.update(b"\0")
                    source_text.update((original["text"] or "").encode("utf-8"))
                    source_text.update(b"\0")
                    rows.append(row)
                if rows:
                    writer.write_table(pa.Table.from_pylist(rows, schema=ARROW_SCHEMA))
                    count += len(rows)
        finally:
            writer.close()
        counts[str(relative)] = count
    return counts, source_text.hexdigest()


def _output_text_digest(out: Path, files: List[Path], turns: Path) -> str:
    rows = (
        row
        for path in files
        for _, row in _iter_rows([out / path.relative_to(turns)])
    )
    return _text_digest(rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--metadata-cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)

    turns = args.turns.resolve()
    metadata_cache = args.metadata_cache.resolve()
    out = args.out.resolve()
    if args.workers < 1 or args.workers > 16:
        parser.error("--workers must be between 1 and 16")
    if not turns.is_dir():
        raise ValueError(f"Turn source directory does not exist: {turns}")
    if metadata_cache in {turns, out}:
        raise ValueError("--metadata-cache must be separate from --turns and --out")
    if out == turns or out in turns.parents or turns in out.parents:
        raise ValueError("--out must be a separate, non-nested directory")
    if out.exists() and any(out.iterdir()):
        raise ValueError("--out must be fresh and empty")

    files = sorted(turns.glob("*.parquet"))
    if not files:
        raise ValueError(f"No normalized source parquets found in {turns}")
    summary_path = _summary_path(args.audit.resolve())
    summary = json.loads(summary_path.read_text())
    candidates, input_hashes, original_count = _validate_source(files, summary)
    metadata, metadata_hashes = _load_metadata(
        (candidate["package"] for candidate in candidates.values()),
        metadata_cache,
        args.workers,
    )
    resolved, unresolved = _resolve_candidates(candidates, metadata)
    changed_rows, removals, changes, removal_proof = _prepare_changes(
        files, resolved, metadata,
    )

    out.mkdir(parents=True, exist_ok=True)
    final_counts, source_text_hash = _write_outputs(
        files, turns, out, changed_rows, removals,
    )
    output_text_hash = _output_text_digest(out, files, turns)
    if source_text_hash != output_text_hash:
        raise AssertionError("Continuation repair changed original transcript text")
    after_hashes = {path.name: checksum(path) for path in files}
    if after_hashes != input_hashes:
        raise AssertionError("Continuation repair modified an input parquet")

    proof = {
        "kind": "complete_normalized_source_metadata_only_continuation_repair",
        "congress": summary["congress"],
        "audit_summary_sha256": checksum(summary_path),
        "input_file_sha256": input_hashes,
        "metadata_file_sha256": metadata_hashes,
        "scoped_original_turn_count": original_count,
        "candidate_initial_continuations": len(candidates),
        "proven_granules": sorted(resolved),
        "attribution_changes": changes,
        "duplicate_removals": removal_proof,
        "unresolved_non_actionable_reasons": unresolved,
        "original_text_preservation": {
            "asserted": True,
            "kept_input_turn_text_sha256": source_text_hash,
            "output_turn_text_sha256": output_text_hash,
        },
        "input_immutability": {
            "asserted": True,
            "sha256_after": after_hashes,
        },
        "final_row_counts": {
            "by_file": final_counts,
            "total": sum(final_counts.values()),
            "removed_strict_physical_overlaps": len(removals),
        },
        "scope": (
            "Metadata-only attribution of source-proven unmarked initial floor "
            "continuations and their own resumed fragments. Transcript text, "
            "tokenization, quotation policy, and all other attribution are unchanged."
        ),
    }
    (out / "continuation_repair_proof.json").write_text(
        json.dumps(proof, indent=2, sort_keys=True) + "\n"
    )
    print(
        f"Congress {summary['congress']}: {len(changes)} attribution changes, "
        f"{len(removals)} strict overlap removals, {len(unresolved)} unresolved"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
