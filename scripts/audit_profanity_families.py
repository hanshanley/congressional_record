#!/usr/bin/env python3
"""Audit every profanity family against original turn text, without publishing data."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
import logging
import shutil
from pathlib import Path
import re
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from analysis.ingest.schema import ARROW_SCHEMA  # noqa: E402
from analysis.inputs import select_turn_files  # noqa: E402
from analysis.score.scorers import (  # noqa: E402
    LEXDIR, Scorers, _MILD_PROFANITY_EXCLUSIONS, _STRONG_PROFANITY_EXCLUSIONS,
)
from analysis.speakers import (  # noqa: E402
    PROFANITY_TERM_FAMILIES, load_daily, parse_profanity_terms, quotation_regions,
    save_daily, speaker_counts,
)

LOG = logging.getLogger("audit_profanity_families")
# The reference walks original words and whitespace, independently of _Lexicon's regexes.
WORDS = re.compile(r"[a-z0-9]+(?:[-'\u2019][a-z0-9]+)*", re.IGNORECASE)


class ReferenceMatcher:
    def __init__(self):
        self.trie = {}
        self.family_by_form = {
            form: family for family, forms in PROFANITY_TERM_FAMILIES.items() for form in forms
        }
        for line in (LEXDIR / "profanity.txt").read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            form, tier = line.split("\t")
            node = self.trie
            for word in form.split():
                node = node.setdefault(word, {})
            node[None] = (form, tier)

    def matches(self, text):
        tokens = list(WORDS.finditer(text))
        index = 0
        while index < len(tokens):
            node = self.trie
            best = None
            end_index = index
            while end_index < len(tokens):
                token = tokens[end_index]
                if end_index > index and not text[tokens[end_index - 1].end():token.start()].isspace():
                    break
                node = node.get(token.group().lower())
                if node is None:
                    break
                if None in node:
                    best = (end_index + 1, tokens[index].start(), token.end(), *node[None])
                end_index += 1
            if best is None:
                index += 1
            else:
                index, start, end, form, tier = best
                yield start, end, form, tier


def overlaps(span, regions):
    return any(span[0] < end and start < span[1] for start, end in regions)


def discover_packages(start, end, expected_dates, client=None):
    from crec.api import GovInfoClient
    from crec.enumerate import iter_packages

    packages = sorted({
        item["packageId"]
        for item in iter_packages(client or GovInfoClient(), start, end)
        if re.fullmatch(r"CREC-\d{4}-\d{2}-\d{2}", item["packageId"])
        and start <= item["packageId"][5:] <= end
    })
    missing = set(expected_dates) - {package[5:] for package in packages}
    if missing:
        from analysis.ingest.govinfo_bulk import probe_packages

        for day in sorted(missing):
            LOG.warning("API enumeration omitted stored date %s; verifying its public archive", day)
            packages.extend(probe_packages(day, day, workers=1))
        packages = sorted(set(packages))
        missing -= {package[5:] for package in packages}
    if missing:
        raise RuntimeError(
            f"Original-source enumeration omits {len(missing)} stored dates: {sorted(missing)}"
        )
    return packages


def congress_window(congress, today):
    start = "1994-01-25" if congress == 103 else f"{1787 + 2 * congress}-01-01"
    # A few outgoing Congresses have recorded floor proceedings on January 3.
    end = min(f"{1789 + 2 * congress}-01-03", today)
    return start, end


def inspect_turn(row, matcher, scorer, mode):
    text = row.get("text") or ""
    quotes = quotation_regions(text, row.get("turn_id")) if mode == "members" else []
    blocked = {
        "mild": [match.span() for match in _MILD_PROFANITY_EXCLUSIONS.finditer(text.lower())],
        "strong": [match.span() for match in _STRONG_PROFANITY_EXCLUSIONS.finditer(text.lower())],
    }
    reviewed = scorer.reviewed_profanity_spans(text)
    for tier in blocked:
        blocked[tier].extend(reviewed[tier])
    eligible = (
        not row.get("is_procedural")
        and row.get("chamber") in {"house", "senate"}
        and (bool(row.get("bioguide")) if mode == "members" else row.get("party") in {"D", "R"})
    )
    evidence = []
    expected = Counter()
    production_text = text
    if mode == "members" and quotes:
        production_text = list(text)
        for start, end in quotes:
            production_text[start:end] = " " * (end - start)
        production_text = "".join(production_text)
    for start, end, form, tier in matcher.matches(text):
        if overlaps((start, end), blocked[tier]):
            disposition = "literal_context_exclusion"
        elif not eligible:
            disposition = (
                "procedural_or_inserted" if row.get("is_procedural")
                else "outside_floor_scope" if row.get("chamber") not in {"house", "senate"}
                else "unattributed" if mode == "members" else "outside_party_scope"
            )
        elif mode == "members" and overlaps((start, end), quotes):
            disposition = "quotation_exclusion"
        else:
            disposition = "accepted"
            expected[form] += 1
        evidence.append({
            "turn_id": row.get("turn_id"), "source": row.get("source"),
            "congress": row.get("congress"), "date": row.get("date"),
            "chamber": row.get("chamber"), "party": row.get("party"),
            "bioguide": row.get("bioguide"), "speaker_name": row.get("speaker_name"),
            "form": form, "family": matcher.family_by_form[form], "tier": tier,
            "disposition": disposition, "start": start, "end": end,
            "matched_text": text[start:end],
            "context": text[max(0, start - 180):min(len(text), end + 180)],
        })
    actual = scorer.profanity_term_counts(production_text) if eligible else Counter()
    score = scorer.score_turn(production_text)["profanity_hits"] if eligible else 0
    mismatch = None
    if actual != expected or score != sum(expected.values()):
        mismatch = {
            "turn_id": row.get("turn_id"), "source": row.get("source"),
            "expected": dict(expected), "actual": dict(actual), "scored_hits": score,
            "context": text[:1000] if not evidence else evidence[0]["context"],
        }
    return expected, evidence, mismatch


def member_day_counts(daily):
    stored = {}
    stored_keys = set()
    for row in daily[daily["chamber"].isin(["house", "senate"])].itertuples(index=False):
        key = (row.bioguide, row.date, row.chamber)
        if key in stored_keys:
            raise ValueError(f"Duplicate stored member-day identity: {key}")
        stored_keys.add(key)
        counts = parse_profanity_terms(row.profanity_terms)
        if sum(counts.values()) != int(row.profanity_hits):
            raise ValueError(f"Stored family counts differ from profanity hits: {key}")
        if counts:
            stored[key] = counts
    return stored


def validate_candidate_counts(candidate, evidence_path):
    expected = defaultdict(Counter)
    with evidence_path.open() as handle:
        for line in handle:
            row = json.loads(line)
            if row["disposition"] == "accepted":
                expected[(row["bioguide"], row["date"], row["chamber"])][row["form"]] += 1
    actual = member_day_counts(candidate)
    differences = [
        key for key in set(expected) | set(actual)
        if expected.get(key, Counter()) != actual.get(key, Counter())
    ]
    if differences:
        raise RuntimeError(
            f"Candidate differs from original-source reference on {len(differences)} member-days: "
            f"{sorted(differences)[:5]}"
        )


def validate_aggregate_counts(frame, evidence_path):
    dimensions = ["congress", "chamber", "party"]
    if "source" in frame:
        dimensions.insert(0, "source")
    expected = Counter()
    with evidence_path.open() as handle:
        for line in handle:
            row = json.loads(line)
            if row["disposition"] == "accepted":
                expected[(*[row[name] for name in dimensions], row["tier"])] += 1
    actual = Counter()
    seen = set()
    scoped = frame[
        frame["party"].isin(["D", "R"]) & frame["chamber"].isin(["house", "senate"])
    ]
    for row in scoped.itertuples(index=False):
        key = tuple(getattr(row, name) for name in dimensions)
        if key in seen:
            raise ValueError(f"Duplicate aggregate identity: {key}")
        seen.add(key)
        if int(row.profanity_hits) != int(row.profanity_mild_hits) + int(row.profanity_strong_hits):
            raise ValueError(f"Aggregate tiers differ from total hits: {key}")
        for tier in ("mild", "strong"):
            count = int(getattr(row, f"profanity_{tier}_hits"))
            if count:
                actual[(*key, tier)] = count
    if actual != expected:
        raise RuntimeError("Aggregate tiers differ from original-source reference counts")


def audit(files, output, *, mode, daily=None, congress=None):
    output.mkdir(parents=True, exist_ok=True)
    matcher, scorer = ReferenceMatcher(), Scorers()
    seen = set()
    by_member_day = defaultdict(Counter)
    dispositions = defaultdict(Counter)
    mismatches = []
    turns = 0
    candidate_turns = 0
    candidate_batch = []
    with (output / "evidence.jsonl").open("w") as evidence_file, pq.ParquetWriter(
        output / "candidate_turns.parquet", ARROW_SCHEMA, compression="zstd",
    ) as candidate_writer:
        for path in files:
            for batch in pq.ParquetFile(path).iter_batches(batch_size=5000):
                for row in batch.to_pylist():
                    if congress is not None and row.get("congress") != congress:
                        continue
                    identity = row.get("turn_id")
                    if not identity:
                        raise ValueError(f"Original transcript turn has no identity: {path}")
                    if identity in seen:
                        continue
                    seen.add(identity)
                    turns += 1
                    counts, evidence, mismatch = inspect_turn(row, matcher, scorer, mode)
                    if evidence:
                        candidate_turns += 1
                        candidate_batch.append(row)
                        if len(candidate_batch) >= 1000:
                            candidate_writer.write_table(
                                pa.Table.from_pylist(candidate_batch, schema=ARROW_SCHEMA),
                            )
                            candidate_batch.clear()
                    for item in evidence:
                        dispositions[item["family"]][item["disposition"]] += 1
                        evidence_file.write(json.dumps(item, ensure_ascii=True) + "\n")
                    if mode == "members" and counts:
                        key = (row["bioguide"], row["date"], row["chamber"])
                        by_member_day[key].update(counts)
                    if mismatch:
                        mismatches.append(mismatch)
        if candidate_batch:
            candidate_writer.write_table(
                pa.Table.from_pylist(candidate_batch, schema=ARROW_SCHEMA),
            )
    differences = []
    if daily is not None:
        scoped = daily if congress is None else daily[daily["congress"] == congress]
        stored = member_day_counts(scoped)
        for key in sorted(set(stored) | set(by_member_day)):
            before, after = stored.get(key, Counter()), by_member_day.get(key, Counter())
            if before != after:
                differences.append({
                    "bioguide": key[0], "date": key[1], "chamber": key[2],
                    "stored": dict(before), "reference": dict(after),
                })
    summary = {
        "mode": mode, "congress": congress, "turns_checked": turns,
        "candidate_turns_retained": candidate_turns,
        "audited_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "codebook_sha256": hashlib.sha256((LEXDIR / "profanity.txt").read_bytes()).hexdigest(),
        "scorer_sha256": hashlib.sha256((LEXDIR.parent / "scorers.py").read_bytes()).hexdigest(),
        "ingester_sha256": hashlib.sha256(
            (ROOT / "analysis/ingest/govinfo.py").read_bytes()
        ).hexdigest(),
        "bulk_ingester_sha256": hashlib.sha256(
            (ROOT / "analysis/ingest/govinfo_bulk.py").read_bytes()
        ).hexdigest(),
        "pdf_ingester_sha256": hashlib.sha256(
            (ROOT / "analysis/ingest/govinfo_pdf.py").read_bytes()
        ).hexdigest(),
        "member_scorer_sha256": hashlib.sha256(
            (ROOT / "analysis/speakers.py").read_bytes()
        ).hexdigest(),
        "context_exclusions_sha256": hashlib.sha256(
            (LEXDIR / "profanity_contexts.json").read_bytes()
        ).hexdigest(),
        "reference": "Original normalized transcript words, never website display labels",
        "stored_comparison_performed": daily is not None,
        "families": {
            family: dict(dispositions.get(family, {}))
            for family in sorted(PROFANITY_TERM_FAMILIES)
        },
        "scorer_disagreements": len(mismatches),
        "stored_member_day_differences": len(differences),
        "limitation": (
            "Exact lexical audit of normalized original transcripts, not a semantic certification "
            "of each use. Excluded text is retained as evidence for attribution review."
        ),
    }
    for name, value in [
        ("summary.json", summary), ("scorer_disagreements.json", mismatches),
        ("stored_differences.json", differences),
    ]:
        (output / name).write_text(json.dumps(value, indent=2) + "\n")
    LOG.info(
        "%d source turns, %d families, %d matching disagreements, %d stored differences",
        turns, len(PROFANITY_TERM_FAMILIES), len(mismatches), len(differences),
    )
    if not turns:
        raise ValueError("Audit has no original transcript turns; refusing an empty success")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=Path)
    parser.add_argument("--fetch-congress", type=int)
    parser.add_argument("--mode", choices=["members", "aggregate"], default="members")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--daily", type=Path, default=ROOT / "data/site/speaker_daily")
    parser.add_argument(
        "--candidate-out", type=Path,
        help="Save independently checked replacement partitions as artifacts; never publish them.",
    )
    parser.add_argument(
        "--aggregate-out", type=Path,
        help="Also audit and retain modern GovInfo aggregate metrics from the same source download.",
    )
    parser.add_argument(
        "--source-out", type=Path,
        help="Retain every normalized original turn, including zero-match turns, for later recovery.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if bool(args.turns) == bool(args.fetch_congress):
        parser.error("provide exactly one of --turns or --fetch-congress")
    if args.candidate_out and (not args.fetch_congress or args.mode != "members"):
        parser.error("--candidate-out requires --fetch-congress with --mode members")
    if args.aggregate_out and (
        not args.candidate_out or not args.fetch_congress or args.fetch_congress < 115
    ):
        parser.error("--aggregate-out requires checked member candidates from Congress 115 onward")
    if args.source_out and not args.fetch_congress:
        parser.error("--source-out requires --fetch-congress")
    daily = load_daily(args.daily) if args.mode == "members" and args.fetch_congress else None
    if args.mode == "members" and args.fetch_congress and (daily is None or daily.empty):
        parser.error("the stored member table is required for a full source comparison")
    if args.fetch_congress:
        from analysis.ingest.govinfo_bulk import run_incremental_bulk
        congress = args.fetch_congress
        if congress < 103:
            parser.error("member audit coverage begins with Congress 103")
        start, end = congress_window(congress, dt.date.today().isoformat())
        expected_dates = (
            set(daily.loc[daily["congress"] == congress, "date"].astype(str))
            if daily is not None else set()
        )
        packages = discover_packages(start, end, expected_dates)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "source_coverage.json").write_text(json.dumps({
            "start": start, "end": end, "packages": packages,
            "stored_dates": sorted(expected_dates),
        }, indent=2) + "\n")
        with tempfile.TemporaryDirectory(prefix="crec-family-audit-") as temporary:
            root = Path(temporary)
            _, deferred = run_incremental_bulk(packages, root / "bulk", root, end=end, workers=6)
            if deferred:
                raise RuntimeError(f"Audit coverage is incomplete: {deferred}")
            if args.source_out:
                args.source_out.mkdir(parents=True, exist_ok=True)
                for path in select_turn_files(root / "turns"):
                    shutil.copy2(path, args.source_out / path.name)
            summary = audit(
                select_turn_files(root / "turns"), args.out,
                mode=args.mode, daily=daily, congress=congress,
            )
            if args.candidate_out and not summary["scorer_disagreements"]:
                candidate = speaker_counts(select_turn_files(root / "turns"))
                candidate = candidate[candidate["congress"] == congress]
                if candidate.empty:
                    raise ValueError("No member rows are available for the candidate partition")
                validate_candidate_counts(candidate, args.out / "evidence.jsonl")
                save_daily(candidate, args.candidate_out)
                if args.aggregate_out:
                    from analysis.aggregate import score_and_aggregate

                    aggregate_audit = args.out / "aggregate"
                    aggregate_summary = audit(
                        select_turn_files(root / "turns"), aggregate_audit,
                        mode="aggregate", congress=congress,
                    )
                    if aggregate_summary["scorer_disagreements"]:
                        raise RuntimeError("Original-source aggregate audit disagrees with the scorer")
                    aggregate = score_and_aggregate(
                        root / "turns", root / "aggregate", incremental=False,
                    )
                    aggregate = aggregate[aggregate["congress"] == congress]
                    validate_aggregate_counts(aggregate, aggregate_audit / "evidence.jsonl")
                    args.aggregate_out.mkdir(parents=True, exist_ok=True)
                    aggregate.to_parquet(
                        args.aggregate_out / f"congress_{congress}.parquet", index=False,
                    )
    else:
        summary = audit(select_turn_files(args.turns), args.out, mode=args.mode)
    return int(bool(
        summary["scorer_disagreements"]
        or (summary["stored_member_day_differences"] and not args.candidate_out)
    ))


if __name__ == "__main__":
    raise SystemExit(main())
