#!/usr/bin/env python3
"""Rescore complete retained candidate turns, preserving checked full-corpus denominators."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import pandas as pd
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis.aggregate import _select_primary_source  # noqa: E402
from analysis.score.scorers import Scorers  # noqa: E402
from analysis.speakers import parse_profanity_terms, save_daily, serialize_profanity_terms  # noqa: E402
from scripts.audit_profanity_families import audit, validate_aggregate_counts  # noqa: E402

WORKER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
source, target, kind = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
if kind == "members":
    from analysis.speakers import speaker_counts
    frame = speaker_counts([source])
else:
    from analysis.aggregate import _score_shard, _finalize
    from analysis.incremental import Shard
    from analysis.score.scorers import Scorers
    counts, _ = _score_shard(Shard("retained-source", (source,)), Scorers(), False)
    frame = _finalize(counts)
frame.to_parquet(target, index=False)
"""


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _definitions(path):
    nodes = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            nodes[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    nodes[target.id] = node
    return nodes


def validate_quotation_extension(before, after):
    trees = [ast.parse(path.read_text()) for path in (before, after)]
    quotes = []
    for tree in trees:
        nodes = [
            node for node in tree.body
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "_REVIEWED_SOURCE_QUOTES"
                for target in node.targets
            )
        ]
        if len(nodes) != 1:
            raise ValueError("Source replay requires one explicit reviewed-quotation table")
        quotes.append(ast.literal_eval(nodes[0].value))
        tree.body.remove(nodes[0])
    if ast.dump(trees[0]) != ast.dump(trees[1]):
        raise ValueError("Source replay cannot change member scoring beyond reviewed quotations")
    old, new = quotes
    if not old.keys() <= new.keys():
        raise ValueError("Source replay cannot remove existing reviewed quotations")
    scorer = Scorers(profanity_contexts=[])
    for source, blocks in new.items():
        previous = old.get(source, ())
        if blocks[:len(previous)] != previous:
            raise ValueError("Source replay cannot change existing reviewed quotations")
        for opening, closing in blocks[len(previous):]:
            if not opening or not closing or not any(
                0 < start < end < len(opening)
                for start, end in scorer.signal_spans(opening)["profanity"]
            ):
                raise ValueError(
                    "New quotation cues must contain a complete profanity occurrence "
                    "so every affected turn is in the retained source cache"
                )
    return old != new


def validate_rules(baseline_root, summary, kind="members"):
    files = {
        "scorer_sha256": "analysis/score/scorers.py",
        "codebook_sha256": "analysis/score/lexicons/profanity.txt",
        "ingester_sha256": "analysis/ingest/govinfo.py",
        "pdf_ingester_sha256": "analysis/ingest/govinfo_pdf.py",
        "member_scorer_sha256": "analysis/speakers.py",
    }
    if "context_exclusions_sha256" in summary:
        files["context_exclusions_sha256"] = "analysis/score/lexicons/profanity_contexts.json"
    if "bulk_ingester_sha256" in summary:
        files["bulk_ingester_sha256"] = "analysis/ingest/govinfo_bulk.py"
    for key, name in files.items():
        if summary.get(key) != checksum(baseline_root / name):
            raise ValueError(f"Producer source does not match its audit: {name}")
    unchanged = [
        "analysis/aggregate.py", "analysis/score/registry.py",
        "analysis/inputs.py", "analysis/ingest/schema.py", "analysis/ingest/govinfo.py",
        "analysis/ingest/govinfo_pdf.py", "analysis/ingest/govinfo_bulk.py",
        "analysis/ingest/hein.py", "analysis/normalize/parties.py",
    ]
    unchanged.extend(
        str(path.relative_to(baseline_root))
        for path in (baseline_root / "analysis/score/lexicons").glob("*.txt")
    )
    for name in unchanged:
        if (baseline_root / name).read_bytes() != (ROOT / name).read_bytes():
            raise ValueError(f"Source replay cannot cover this non-profanity change: {name}")
    quotation_extension = False
    if kind == "members":
        quotation_extension = validate_quotation_extension(
            baseline_root / "analysis/speakers.py", ROOT / "analysis/speakers.py",
        )
    before = _definitions(baseline_root / "analysis/score/scorers.py")
    after = _definitions(ROOT / "analysis/score/scorers.py")
    for name in (
        "_TOKEN_RE", "_SLUR_EXCLUSIONS", "_FUNCTION", "_IRREGULAR", "_MIN_INFLECT_LEN",
        "_load_lines", "_regular_variants", "morph_variants", "plural_variants",
        "_word_regex", "_split_terms", "_phrase_regex", "_Lexicon", "_load_profanity",
    ):
        if ast.dump(before[name]) != ast.dump(after[name]):
            raise ValueError(f"Retained candidate coverage is invalid after changing {name}")
    old_class, new_class = before["Scorers"], after["Scorers"]
    old_methods = {node.name: node for node in old_class.body if isinstance(node, ast.FunctionDef)}
    new_methods = {node.name: node for node in new_class.body if isinstance(node, ast.FunctionDef)}
    for name in ("_slur_spans", "_slur_count", "_mild_profanity_spans", "_strong_profanity_spans"):
        if ast.dump(old_methods[name]) != ast.dump(new_methods[name]):
            raise ValueError(f"Profanity-only replay cannot change {name}")
    for attribute in ("formal_courtesy", "profanity", "slurs"):
        assignments = []
        for methods in (old_methods, new_methods):
            assignments.append(next(
                node for node in methods["__init__"].body
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Attribute) and target.attr == attribute
                    for target in node.targets
                )
            ))
        if ast.dump(assignments[0]) != ast.dump(assignments[1]):
            raise ValueError(f"Retained candidate coverage cannot change {attribute} construction")
    for name in ("score_turn",):
        old, new = old_methods[name], new_methods[name]
        if [ast.dump(node) for node in old.body[:2]] != [ast.dump(node) for node in new.body[:2]]:
            raise ValueError("Word tokenization changed")
        old_return = next(node.value for node in old.body if isinstance(node, ast.Return))
        new_return = next(node.value for node in new.body if isinstance(node, ast.Return))
        old_values = {key.value: value for key, value in zip(old_return.keys, old_return.values)}
        new_values = {key.value: value for key, value in zip(new_return.keys, new_return.values)}
        for key in ("n_words", "formal_courtesy_hits", "slur_hits"):
            if ast.dump(old_values[key]) != ast.dump(new_values[key]):
                raise ValueError(f"Profanity-only replay cannot change {key}")
    return quotation_extension


def _indexed(frame, keys):
    if frame.empty:
        return {}
    if frame.duplicated(keys).any():
        raise ValueError(f"Duplicate scored source identity: {keys}")
    return {
        tuple(row[name] for name in keys): row
        for row in frame.to_dict(orient="records")
    }


def replace_counts(baseline, before, after, kind, quotation_extension=False):
    keys = ["bioguide", "date", "chamber"] if kind == "members" else [
        "source", "congress", "chamber", "party",
    ]
    fields = (
        ["profanity_hits", "profanity_quoted_hits"]
        if kind == "members" else
        ["profanity_hits", "profanity_mild_hits", "profanity_strong_hits"]
    )
    original = _indexed(baseline, keys)
    old, new = _indexed(before, keys), _indexed(after, keys)
    if not set(old).issubset(original) or set(old) != set(new):
        raise ValueError("Replay changed source identities or eligibility")
    for key, row in original.items():
        for field in fields:
            if int(row[field]) != int(old.get(key, {}).get(field, 0)):
                raise ValueError(f"Retained sources do not reproduce the baseline: {key}, {field}")
        if kind == "members":
            if parse_profanity_terms(row["profanity_terms"]) != parse_profanity_terms(
                old.get(key, {}).get("profanity_terms", "{}")
            ):
                raise ValueError(f"Retained sources do not reproduce actual original forms: {key}")
    protected = ["words", "turns", "slur_hits"]
    if kind == "members":
        protected.append("slur_quoted_hits")
    else:
        protected.append("formal_courtesy_hits")
    moved_fields = []
    if kind == "members" and quotation_extension:
        moved_fields = ["words", "slur_hits", "slur_quoted_hits"]
        protected = [field for field in protected if field not in moved_fields]
    for key in old:
        if any(int(new[key][field]) < 0 for field in fields):
            raise ValueError(f"Replayed profanity counts cannot be negative: {key}")
        if kind == "members":
            if sum(parse_profanity_terms(new[key]["profanity_terms"]).values()) != int(
                new[key]["profanity_hits"]
            ):
                raise ValueError(f"Replayed original-form counts differ from total hits: {key}")
        elif int(new[key]["profanity_hits"]) != (
            int(new[key]["profanity_mild_hits"]) + int(new[key]["profanity_strong_hits"])
        ):
            raise ValueError(f"Replayed profanity tiers differ from total hits: {key}")
        for field in protected:
            if old[key][field] != new[key][field]:
                raise ValueError(f"Profanity-only replay changed {field}: {key}")
        if moved_fields and (
            new[key]["words"] > old[key]["words"]
            or new[key]["slur_hits"] > old[key]["slur_hits"]
            or new[key]["slur_quoted_hits"] < old[key]["slur_quoted_hits"]
            or new[key]["slur_hits"] + new[key]["slur_quoted_hits"]
            != old[key]["slur_hits"] + old[key]["slur_quoted_hits"]
        ):
            raise ValueError(f"Reviewed quotation extension changed more than quoted attribution: {key}")
    corrected = baseline.copy(deep=True)
    changed = 0
    for index, row in corrected.iterrows():
        key = tuple(row[name] for name in keys)
        values = new.get(key, {})
        different = any(int(row[field]) != int(values.get(field, 0)) for field in fields)
        for field in moved_fields:
            delta = int(values.get(field, 0)) - int(old.get(key, {}).get(field, 0))
            value = int(row[field]) + delta
            if value < 0:
                raise ValueError(f"Source quotation delta makes {field} negative: {key}")
            corrected.at[index, field] = value
            different |= delta != 0
        for field in fields:
            corrected.at[index, field] = int(values.get(field, 0))
        if kind == "members":
            corrected.at[index, "profanity_terms"] = serialize_profanity_terms(
                parse_profanity_terms(values.get("profanity_terms", "{}"))
            )
        changed += different
    if kind == "aggregate":
        for rate, hits in [
            ("profanity_per_1k", "profanity_hits"),
            ("profanity_mild_per_1k", "profanity_mild_hits"),
            ("profanity_strong_per_1k", "profanity_strong_hits"),
        ]:
            corrected[rate] = 1000 * corrected[hits] / corrected["words"].replace(0, 1)
    return corrected, changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["members", "aggregate"], required=True)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--baseline-data", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    args.baseline_root = args.baseline_root.resolve()
    summary = json.loads((args.audit / "summary.json").read_text())
    if summary["mode"] != args.kind or summary["scorer_disagreements"] or not summary["turns_checked"]:
        raise ValueError("A complete, disagreement-free original-source audit is required")
    snapshot = (args.audit / "candidate_turns.parquet").resolve()
    if pq.ParquetFile(snapshot).metadata.num_rows != summary["candidate_turns_retained"]:
        raise ValueError("Retained original source count differs from the completed audit")
    quotation_extension = validate_rules(args.baseline_root, summary, args.kind)
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="profanity-replay-") as temporary:
        directory = Path(temporary)
        scored = []
        for label, root in [("before", args.baseline_root), ("after", ROOT)]:
            target = directory / f"{label}.parquet"
            subprocess.run(
                [sys.executable, "-c", WORKER, str(root), str(snapshot), str(target), args.kind],
                cwd=root, check=True,
            )
            scored.append(pd.read_parquet(target))
        baseline = pd.read_parquet(args.baseline_data)
        if baseline.empty:
            raise ValueError("An empty baseline cannot establish full-corpus denominators")
        corrected, changed = replace_counts(
            baseline, *scored, args.kind, quotation_extension=quotation_extension,
        )
    report = audit(
        [snapshot], args.out / "audit", mode=args.kind,
        daily=corrected if args.kind == "members" else None,
        congress=summary["congress"],
    )
    if report["scorer_disagreements"] or report["stored_member_day_differences"]:
        raise ValueError("Replayed data do not reconcile with the independent original-word reference")
    if args.kind == "aggregate":
        validate_aggregate_counts(corrected, args.out / "audit/evidence.jsonl")
    if args.kind == "members":
        save_daily(corrected, args.out / "speaker_daily")
    else:
        metrics = args.out / "metrics"
        metrics.mkdir(parents=True, exist_ok=True)
        corrected.to_parquet(metrics / "civility_metrics_by_source.parquet", index=False)
        corrected.to_csv(metrics / "civility_metrics_by_source.csv", index=False)
        primary = _select_primary_source(corrected)
        primary.to_parquet(metrics / "civility_metrics.parquet", index=False)
        primary.to_csv(metrics / "civility_metrics.csv", index=False)
    (args.out / "source_replay.json").write_text(json.dumps({
        "kind": args.kind, "original_source_audit": summary,
        "reviewed_quotation_extension": quotation_extension,
        "baseline_data_sha256": checksum(args.baseline_data),
        "original_candidate_turns_sha256": checksum(snapshot),
        "replayed_scorer_sha256": checksum(ROOT / "analysis/score/scorers.py"),
        "replayed_context_exclusions_sha256": checksum(
            ROOT / "analysis/score/lexicons/profanity_contexts.json"
        ),
        "changed_groups": changed, "words_before": int(baseline.words.sum()),
        "words_after": int(corrected.words.sum()),
        "coverage": (
            "Complete original normalized candidate turns, with exact whole-baseline "
            "profanity-counter reconciliation; unchanged codebook, tokenizer and eligibility. "
            "Zero-match source turns are preserved. Full-corpus denominators are preserved "
            "except for source-proven additional quoted words in member attribution."
        ),
    }, indent=2) + "\n")
    print(
        f"{args.kind}: {changed} changed groups; "
        f"{int(baseline.words.sum())} -> {int(corrected.words.sum())} original words"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
