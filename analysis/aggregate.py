"""Score turns and aggregate to time-series metrics.

Streams the per-congress turn parquet files in batches, scores each turn with the
lexicon scorers, and accumulates sums per ``(congress, chamber, party)`` group so
the whole corpus never needs to live in memory. Emits a tidy metrics table with
rates per 1,000 words plus directed (toward-out-party) measures.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import pyarrow.parquet as pq

from analysis.ingest.schema import year_from_congress
from analysis.score.scorers import Scorers

LOG = logging.getLogger("analysis.aggregate")

# Sum accumulators kept per group.
_SUM_KEYS = [
    "turns", "n_words", "comity_hits", "formal_courtesy_hits",
    "gratitude_praise_hits", "cooperation_hits", "hostility_hits",
    "misconduct_hits", "ideological_label_hits",
    "profanity_mild", "profanity_strong", "profanity_slurs", "profanity_hits",
    "outgroup_refs", "democrat_party_pej",
    "directed_comity_hits", "directed_hostility_hits", "directed_misconduct_hits",
    "outgroup_comity_contexts", "outgroup_hostility_contexts",
    "outgroup_misconduct_contexts",
    "sentiment_sum", "neg_share_sum", "sentiment_n",
]

_READ_COLS = [
    "turn_id", "source", "congress", "chamber", "party", "word_count",
    "is_procedural", "text",
]

# Single source of truth for per-1,000-word rate columns: rate name -> the raw hit
# column it is derived from. Shared with :mod:`analysis.viz` so both modules emit
# identically named rates from the same formula.
RATE_TO_HITCOL: Dict[str, str] = {
    "comity_per_1k": "comity_hits",
    "formal_courtesy_per_1k": "formal_courtesy_hits",
    "gratitude_praise_per_1k": "gratitude_praise_hits",
    "cooperation_per_1k": "cooperation_hits",
    "hostility_per_1k": "hostility_hits",
    "misconduct_per_1k": "misconduct_hits",
    "ideological_label_per_1k": "ideological_label_hits",
    "profanity_per_1k": "profanity_hits",
    "profanity_mild_per_1k": "profanity_mild_hits",
    "profanity_strong_per_1k": "profanity_strong_hits",
    "profanity_slurs_per_1k": "profanity_slurs_hits",
    "outgroup_ref_per_1k": "outgroup_refs",
    "democrat_party_pej_per_1k": "democrat_party_pej",
    "directed_comity_per_1k": "directed_comity_hits",
    "directed_hostility_per_1k": "directed_hostility_hits",
    "directed_misconduct_per_1k": "directed_misconduct_hits",
}

CONTEXT_RATE_TO_COUNT: Dict[str, str] = {
    "outgroup_comity_contexts_per_100_refs": "outgroup_comity_contexts",
    "outgroup_hostility_contexts_per_100_refs": "outgroup_hostility_contexts",
    "outgroup_misconduct_contexts_per_100_refs": "outgroup_misconduct_contexts",
}


def _select_turn_files(turns_dir: Path) -> List[Path]:
    """Return all turn files, with bulk GovInfo files before manifest-based files.

    A partial bulk file must never suppress fuller manifest coverage. GovInfo rows are
    unioned during scoring and deduplicated by ``turn_id``; bulk-first ordering means
    the bulk representation wins when both paths contain the same turn.
    """
    files = sorted(turns_dir.glob("*.parquet"))
    return sorted(
        files,
        key=lambda f: (
            0 if f.name.startswith("hein_") else
            1 if f.name.startswith("govinfo_bulk_") else
            2,
            f.name,
        ),
    )


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
    acc: Dict[Tuple[int, str, str], Dict[str, float]] = defaultdict(
        lambda: {k: 0.0 for k in _SUM_KEYS}
    )
    coverage: Dict[Tuple[str, int, str], Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )

    files = _select_turn_files(turns_dir)
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
                key = (int(congresses[i]), chambers[i] or "other", party)
                a = acc[key]
                a["turns"] += 1
                a["n_words"] += s["n_words"]
                for k in (
                    "comity_hits", "formal_courtesy_hits", "gratitude_praise_hits",
                    "cooperation_hits", "hostility_hits", "misconduct_hits",
                    "ideological_label_hits", "profanity_mild",
                    "profanity_strong", "profanity_slurs", "profanity_hits",
                    "outgroup_refs", "democrat_party_pej",
                    "directed_comity_hits", "directed_hostility_hits",
                    "directed_misconduct_hits",
                    "outgroup_comity_contexts", "outgroup_hostility_contexts",
                    "outgroup_misconduct_contexts",
                ):
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

    df = _finalize(acc)
    out_metrics = out_dir / "metrics"
    out_metrics.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_metrics / "civility_metrics.parquet", index=False)
    df.to_csv(out_metrics / "civility_metrics.csv", index=False)
    _write_coverage(coverage, out_dir)
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


def _finalize(acc: Dict[Tuple[int, str, str], Dict[str, float]]) -> pd.DataFrame:
    rows: List[dict] = []
    for (congress, chamber, party), a in sorted(acc.items()):
        words = a["n_words"] or 1.0
        row = {
            "congress": congress,
            "year": year_from_congress(congress),  # Congress N convenes in this year
            "chamber": chamber,
            "party": party,
            "turns": int(a["turns"]),
            "words": int(a["n_words"]),
            # raw sums (kept so viz can re-aggregate across chambers correctly)
            "comity_hits": int(a["comity_hits"]),
            "formal_courtesy_hits": int(a["formal_courtesy_hits"]),
            "gratitude_praise_hits": int(a["gratitude_praise_hits"]),
            "cooperation_hits": int(a["cooperation_hits"]),
            "hostility_hits": int(a["hostility_hits"]),
            "misconduct_hits": int(a["misconduct_hits"]),
            "ideological_label_hits": int(a["ideological_label_hits"]),
            "profanity_hits": int(a["profanity_hits"]),
            "profanity_mild_hits": int(a["profanity_mild"]),
            "profanity_strong_hits": int(a["profanity_strong"]),
            "profanity_slurs_hits": int(a["profanity_slurs"]),
            "outgroup_refs": int(a["outgroup_refs"]),
            "democrat_party_pej": int(a["democrat_party_pej"]),
            "directed_comity_hits": int(a["directed_comity_hits"]),
            "directed_hostility_hits": int(a["directed_hostility_hits"]),
            "directed_misconduct_hits": int(a["directed_misconduct_hits"]),
            "outgroup_comity_contexts": int(a["outgroup_comity_contexts"]),
            "outgroup_hostility_contexts": int(a["outgroup_hostility_contexts"]),
            "outgroup_misconduct_contexts": int(a["outgroup_misconduct_contexts"]),
        }
        # convenience rates at this (congress, chamber, party) granularity, derived from
        # the raw hit columns via the shared RATE_TO_HITCOL map (same names/formula as viz)
        for rate, col in RATE_TO_HITCOL.items():
            row[rate] = 1000.0 * row[col] / words
        refs = row["outgroup_refs"]
        for rate, col in CONTEXT_RATE_TO_COUNT.items():
            row[rate] = 100.0 * row[col] / refs if refs else 0.0
        row["mean_sentiment"] = (
            a["sentiment_sum"] / a["sentiment_n"] if a["sentiment_n"] else None
        )
        row["mean_neg_share"] = (
            a["neg_share_sum"] / a["sentiment_n"] if a["sentiment_n"] else None
        )
        rows.append(row)
    return pd.DataFrame(rows)
