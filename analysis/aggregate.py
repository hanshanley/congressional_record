"""Score turns and aggregate to time-series metrics.

Streams the per-congress turn parquet files in batches, scores each turn with the
lexicon scorers, and accumulates sums per ``(congress, chamber, party)`` group so
the whole corpus never needs to live in memory. Emits a tidy metrics table with
rates per 1,000 words plus directed (toward-out-party) measures.
"""

from __future__ import annotations

import logging
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import pyarrow.parquet as pq

from analysis.ingest.schema import year_from_congress
from analysis.inputs import select_turn_files
from analysis.score.scorers import Scorers
from analysis.score.registry import METRICS, SCORE_KEYS

LOG = logging.getLogger("analysis.aggregate")

# Sum accumulators kept per group.
_SUM_KEYS = [
    "turns", "n_words", *SCORE_KEYS, "sentiment_sum", "neg_share_sum", "sentiment_n",
]

_READ_COLS = [
    "turn_id", "source", "congress", "chamber", "party", "word_count",
    "is_procedural", "text",
]

# Single source of truth for per-1,000-word rate columns: rate name -> the raw hit
# column it is derived from. Shared with :mod:`analysis.viz` so both modules emit
# identically named rates from the same formula.
RATE_TO_HITCOL: Dict[str, str] = {
    metric.rate: metric.raw_count for metric in METRICS if metric.denominator == "words"
}
CONTEXT_RATE_TO_COUNT: Dict[str, str] = {
    metric.rate: metric.raw_count
    for metric in METRICS
    if metric.denominator == "outgroup_refs"
}


def _iter_batches(path: Path, batch_size: int = 10_000):
    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=batch_size, columns=_READ_COLS):
        d = batch.to_pydict()
        yield d


def score_and_aggregate(
    turns_dir: Path,
    out_dir: Path,
    use_sentiment: bool = False,
    include_procedural: bool = False,
) -> pd.DataFrame:
    """Score all turn parquet files under ``turns_dir`` and write metrics.

    Returns the tidy metrics DataFrame (also written to
    ``out_dir/metrics/civility_metrics.{parquet,csv}``).
    """
    scorers = Scorers(use_sentiment=use_sentiment)
    acc: Dict[Tuple[str, int, str, str], Dict[str, float]] = defaultdict(
        lambda: {k: 0.0 for k in _SUM_KEYS}
    )
    coverage: Dict[Tuple[str, int, str], Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )

    files = select_turn_files(turns_dir)
    if not files:
        raise FileNotFoundError(f"no turn parquet files in {turns_dir}")

    # Only GovInfo paths can represent the same source turns twice (bulk package vs
    # manifest ingest). Keeping this set GovInfo-only avoids retaining 18M Hein IDs.
    seen_govinfo_ids: set[str] = set()

    for fp in files:
        n = 0
        is_govinfo = fp.name.startswith("govinfo")
        for d in _iter_batches(fp):
            turn_ids = d["turn_id"]
            sources = d["source"]
            texts = d["text"]
            parties = d["party"]
            word_counts = d["word_count"]
            congresses = d["congress"]
            chambers = d["chamber"]
            procs = d["is_procedural"]
            for i in range(len(texts)):
                if is_govinfo:
                    turn_id = turn_ids[i]
                    if turn_id in seen_govinfo_ids:
                        continue
                    seen_govinfo_ids.add(turn_id)
                coverage_key = (
                    sources[i] or "unknown",
                    int(congresses[i]),
                    chambers[i] or "other",
                )
                cov = coverage[coverage_key]
                row_words = int(word_counts[i] or 0)
                cov["total_turns"] += 1
                cov["total_words"] += row_words
                if procs[i]:
                    cov["procedural_turns"] += 1
                    cov["procedural_words"] += row_words
                else:
                    cov["nonprocedural_turns"] += 1
                    cov["nonprocedural_words"] += row_words
                    if parties[i] in {"D", "R", "I"}:
                        cov["analysis_party_turns"] += 1
                        cov["analysis_party_words"] += row_words
                if not include_procedural and procs[i]:
                    continue
                party = parties[i] or "other"
                s = scorers.score_turn(texts[i] or "", party)
                key = (
                    sources[i] or "unknown",
                    int(congresses[i]),
                    chambers[i] or "other",
                    party,
                )
                a = acc[key]
                a["turns"] += 1
                a["n_words"] += s["n_words"]
                for k in SCORE_KEYS:
                    a[k] += s[k]
                if "sentiment" in s:
                    # Sentence-count weight so long speeches count proportionally (matches
                    # the word-weighting of every other metric). A turn with no detectable
                    # sentences (empty/whitespace) carries weight 0 — it must not add a
                    # phantom neutral sentence to the mean.
                    w = s.get("n_sentences", 0)
                    if w:
                        a["sentiment_sum"] += s["sentiment"] * w
                        a["neg_share_sum"] += s.get("neg_share", 0.0) * w
                        a["sentiment_n"] += w
                n += 1
        LOG.info("scored %s (%d substantive turns)", fp.name, n)

    source_df = _finalize(acc)
    df = _select_primary_source(source_df)
    out_metrics = out_dir / "metrics"
    out_metrics.mkdir(parents=True, exist_ok=True)
    source_df.to_parquet(out_metrics / "civility_metrics_by_source.parquet", index=False)
    source_df.to_csv(out_metrics / "civility_metrics_by_source.csv", index=False)
    df.to_parquet(out_metrics / "civility_metrics.parquet", index=False)
    df.to_csv(out_metrics / "civility_metrics.csv", index=False)
    _write_coverage(coverage, out_dir)
    _write_source_metadata(source_df, out_dir)
    LOG.info("wrote metrics: %d rows -> %s", len(df), out_metrics)
    return df


def _write_coverage(
    coverage: Dict[Tuple[str, int, str], Dict[str, int]], out_dir: Path
) -> None:
    """Write source/chamber coverage and usable party-attribution shares."""
    rows: List[dict] = []
    for (source, congress, chamber), cov in sorted(coverage.items()):
        nonproc_turns = cov["nonprocedural_turns"]
        nonproc_words = cov["nonprocedural_words"]
        counts = {
            key: int(cov[key])
            for key in (
                "total_turns", "total_words", "procedural_turns", "procedural_words",
                "nonprocedural_turns", "nonprocedural_words", "analysis_party_turns",
                "analysis_party_words",
            )
        }
        rows.append({
            "source": source,
            "congress": congress,
            "year": year_from_congress(congress),
            "chamber": chamber,
            **counts,
            "analysis_party_turn_share": (
                cov["analysis_party_turns"] / nonproc_turns if nonproc_turns else 0.0
            ),
            "analysis_party_word_share": (
                cov["analysis_party_words"] / nonproc_words if nonproc_words else 0.0
            ),
        })
    coverage_dir = out_dir / "coverage"
    coverage_dir.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(rows)
    frame.to_parquet(coverage_dir / "turn_coverage.parquet", index=False)
    frame.to_csv(coverage_dir / "turn_coverage.csv", index=False)


def _write_source_metadata(source_df: pd.DataFrame, out_dir: Path) -> None:
    """Persist source ranges and the primary source transition used by plots."""
    sources = []
    for source, group in source_df.groupby("source"):
        sources.append({
            "source": source,
            "min_congress": int(group["congress"].min()),
            "max_congress": int(group["congress"].max()),
            "min_year": int(group["year"].min()),
            "max_year": int(group["year"].max()),
            "rows": int(len(group)),
        })
    gov_primary = source_df[
        source_df["source"].eq("govinfo") & source_df["congress"].ge(115)
    ]
    boundary_year = (
        int(gov_primary["year"].min()) if not gov_primary.empty else None
    )
    payload = {
        "sources": sorted(sources, key=lambda item: item["source"]),
        "primary_boundary_year": boundary_year,
        "primary_rule": "Hein through Congress 114; GovInfo from Congress 115",
    }
    coverage_dir = out_dir / "coverage"
    coverage_dir.mkdir(parents=True, exist_ok=True)
    (coverage_dir / "source_metadata.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def _select_primary_source(source_df: pd.DataFrame) -> pd.DataFrame:
    """Select the documented long-run source regime without combining overlap rows."""
    if source_df.empty:
        return source_df.drop(columns=["source"], errors="ignore")
    selected = source_df[
        ((source_df["congress"] <= 114) & source_df["source"].str.startswith("hein_"))
        | ((source_df["congress"] >= 115) & source_df["source"].eq("govinfo"))
    ].copy()
    # The Hein ingester writes only one edition per Congress, but keep a deterministic
    # preference if legacy files ever contain both.
    selected["_source_rank"] = selected["source"].map(
        {"hein_daily": 0, "hein_bound": 1, "govinfo": 0}
    ).fillna(9)
    selected = (
        selected.sort_values("_source_rank")
        .drop_duplicates(["congress", "chamber", "party"], keep="first")
        .drop(columns=["source", "_source_rank"])
        .sort_values(["congress", "chamber", "party"])
        .reset_index(drop=True)
    )
    return selected


def _finalize(acc: Dict[Tuple[str, int, str, str], Dict[str, float]]) -> pd.DataFrame:
    rows: List[dict] = []
    for (source, congress, chamber, party), a in sorted(acc.items()):
        words = a["n_words"] or 1.0
        row = {
            "source": source,
            "congress": congress,
            "year": year_from_congress(congress),  # Congress N convenes in this year
            "chamber": chamber,
            "party": party,
            "turns": int(a["turns"]),
            "words": int(a["n_words"]),
        }
        for metric in METRICS:
            row[metric.raw_count] = int(a[metric.score_key])
        # convenience rates at this (congress, chamber, party) granularity, derived from
        # the raw hit columns via the shared RATE_TO_HITCOL map (same names/formula as viz)
        for metric in METRICS:
            denominator = words if metric.denominator == "words" else row["outgroup_refs"]
            row[metric.rate] = (
                metric.scale * row[metric.raw_count] / denominator if denominator else 0.0
            )
        row["mean_sentiment"] = (
            a["sentiment_sum"] / a["sentiment_n"] if a["sentiment_n"] else None
        )
        row["mean_neg_share"] = (
            a["neg_share_sum"] / a["sentiment_n"] if a["sentiment_n"] else None
        )
        rows.append(row)
    return pd.DataFrame(rows)
