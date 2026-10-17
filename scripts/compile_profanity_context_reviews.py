#!/usr/bin/env python3
"""Bind semantic review decisions to complete, retained original transcript turns."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis.score.scorers import Scorers  # noqa: E402
from scripts.audit_profanity_families import ReferenceMatcher  # noqa: E402


def identity(row):
    return row["turn_id"], row["start"], row["end"], row["form"]


def compile_reviews(audit, review_paths):
    summary = json.loads((audit / "summary.json").read_text())
    if summary.get("scorer_disagreements") != 0 or not summary.get("turns_checked"):
        raise ValueError("Reviews require a complete, disagreement-free source audit")
    snapshot = audit / "candidate_turns.parquet"
    if pq.ParquetFile(snapshot).metadata.num_rows != summary.get("candidate_turns_retained"):
        raise ValueError("Original-turn snapshot does not match the completed audit")
    reviews = {}
    for path in review_paths:
        for line in path.read_text().splitlines():
            row = json.loads(line)
            key = identity(row)
            if key in reviews:
                raise ValueError(f"Duplicate reviewed original occurrence: {key}")
            if (
                row.get("decision") not in {"retain", "exclude_nonprofane", "uncertain"}
                or type(row.get("confidence")) not in (int, float)
                or not 0 <= row["confidence"] <= 1
                or not isinstance(row.get("reason"), str) or not row["reason"].strip()
            ):
                raise ValueError(f"Invalid semantic review: {key}")
            reviews[key] = row
    if not reviews:
        raise ValueError("No semantic review decisions supplied")
    evidence = {}
    with (audit / "evidence.jsonl").open() as handle:
        for line in handle:
            row = json.loads(line)
            key = identity(row)
            if key in reviews:
                if key in evidence:
                    raise ValueError(f"Duplicate original audit evidence: {key}")
                evidence[key] = row
    if reviews.keys() != evidence.keys():
        raise ValueError(f"Reviews are absent from current original evidence: {reviews.keys() - evidence.keys()}")
    wanted = {key[0] for key in reviews}
    originals = {}
    for batch in pq.ParquetFile(snapshot).iter_batches(
        batch_size=128, columns=["turn_id", "text"],
    ):
        for row in batch.to_pylist():
            if row["turn_id"] in wanted:
                if row["turn_id"] in originals:
                    raise ValueError(f"Duplicate retained original turn: {row['turn_id']}")
                originals[row["turn_id"]] = row["text"]
    if wanted != originals.keys():
        raise ValueError(f"Full original turns are missing: {wanted - originals.keys()}")
    matcher = ReferenceMatcher()
    lexical = {
        turn_id: set(matcher.matches(text)) for turn_id, text in originals.items()
    }
    records = {}
    decisions = {}
    for key, review in reviews.items():
        turn_id, start, end, form = key
        source = evidence[key]
        text = originals[turn_id]
        if (
            type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)
            or text[start:end] != source["matched_text"]
            or (start, end, form, source["tier"]) not in lexical[turn_id]
        ):
            raise ValueError(f"Reviewed occurrence does not match its complete original turn: {key}")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        span = digest, start, end
        if span in decisions and decisions[span] != review["decision"]:
            raise ValueError(f"Conflicting semantic decisions about identical original text: {key}")
        decisions[span] = review["decision"]
        if review["decision"] != "exclude_nonprofane":
            continue
        if span in records:
            records[span]["source_turn_ids"].append(turn_id)
        else:
            records[span] = {
                "text_sha256": digest, "start": start, "end": end,
                "form": form, "tier": source["tier"], "matched_text": text[start:end],
                "source_turn_id": turn_id, "source_turn_ids": [turn_id],
                "reason": review["reason"],
            }
    manifest = {"version": 1, "records": [records[key] for key in sorted(records)]}
    Scorers(profanity_contexts=manifest["records"])
    report = {
        "reviewed_occurrences": len(reviews),
        "decisions": dict(Counter(row["decision"] for row in reviews.values())),
        "compiled_exclusion_spans": len(records),
        "complete_original_turns_verified": len(originals),
        "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "review_files": {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in review_paths
        },
        "limitation": "Uncertain decisions remain lexical matches; no source text or word counts changed.",
    }
    return manifest, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    manifest, report = compile_reviews(args.audit, args.reviews)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "profanity_contexts.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (args.out / "source_review_validation.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
