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
    "turns", "n_words", "comity_hits", "hostility_hits",
    "profanity_mild", "profanity_strong", "profanity_slurs", "profanity_hits",
    "outgroup_refs", "democrat_party_pej",
    "directed_comity_hits", "directed_hostility_hits",
    "sentiment_sum", "neg_share_sum", "sentiment_n",
]

_READ_COLS = ["congress", "chamber", "party", "is_procedural", "text"]

# Single source of truth for per-1,000-word rate columns: rate name -> the raw hit
# column it is derived from. Shared with :mod:`analysis.viz` so both modules emit
# identically named rates from the same formula.
RATE_TO_HITCOL: Dict[str, str] = {
    "comity_per_1k": "comity_hits",
    "hostility_per_1k": "hostility_hits",
    "profanity_per_1k": "profanity_hits",
    "profanity_mild_per_1k": "profanity_mild_hits",
    "profanity_strong_per_1k": "profanity_strong_hits",
    "profanity_slurs_per_1k": "profanity_slurs_hits",
    "outgroup_ref_per_1k": "outgroup_refs",
    "democrat_party_pej_per_1k": "democrat_party_pej",
    "directed_comity_per_1k": "directed_comity_hits",
    "directed_hostility_per_1k": "directed_hostility_hits",
}


def _select_turn_files(turns_dir: Path) -> List[Path]:
    """Choose one turn file per source-congress, preferring bulk GovInfo output.

    Both ``govinfo_<c>.parquet`` (manifest path) and ``govinfo_bulk_<c>.parquet``
    (day-zip path) can exist for the same congress with identical turn_ids; scoring
    both would double-count every 2017+ turn. Prefer the bulk file and drop the
    matching manifest-based one.
    """
    files = sorted(turns_dir.glob("*.parquet"))
    bulk_congresses = {
        f.stem[len("govinfo_bulk_"):] for f in files if f.name.startswith("govinfo_bulk_")
    }
    selected: List[Path] = []
    for f in files:
        if f.name.startswith("govinfo_") and not f.name.startswith("govinfo_bulk_"):
            congress = f.stem[len("govinfo_"):]
            if congress in bulk_congresses:
                LOG.info("skipping %s (superseded by govinfo_bulk_%s)", f.name, congress)
                continue
        selected.append(f)
    return selected


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

    files = _select_turn_files(turns_dir)
    if not files:
        raise FileNotFoundError(f"no turn parquet files in {turns_dir}")

    for fp in files:
        n = 0
        for d in _iter_batches(fp):
            texts = d["text"]
            parties = d["party"]
            congresses = d["congress"]
            chambers = d["chamber"]
            procs = d["is_procedural"]
            for i in range(len(texts)):
                if not include_procedural and procs[i]:
                    continue
                party = parties[i] or "other"
                s = scorers.score_turn(texts[i] or "", party)
                key = (int(congresses[i]), chambers[i] or "other", party)
                a = acc[key]
                a["turns"] += 1
                a["n_words"] += s["n_words"]
                for k in (
                    "comity_hits", "hostility_hits", "profanity_mild",
                    "profanity_strong", "profanity_slurs", "profanity_hits",
                    "outgroup_refs", "democrat_party_pej",
                    "directed_comity_hits", "directed_hostility_hits",
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
    LOG.info("wrote metrics: %d rows -> %s", len(df), out_metrics)
    return df


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
            "hostility_hits": int(a["hostility_hits"]),
            "profanity_hits": int(a["profanity_hits"]),
            "profanity_mild_hits": int(a["profanity_mild"]),
            "profanity_strong_hits": int(a["profanity_strong"]),
            "profanity_slurs_hits": int(a["profanity_slurs"]),
            "outgroup_refs": int(a["outgroup_refs"]),
            "democrat_party_pej": int(a["democrat_party_pej"]),
            "directed_comity_hits": int(a["directed_comity_hits"]),
            "directed_hostility_hits": int(a["directed_hostility_hits"]),
        }
        # convenience rates at this (congress, chamber, party) granularity, derived from
        # the raw hit columns via the shared RATE_TO_HITCOL map (same names/formula as viz)
        for rate, col in RATE_TO_HITCOL.items():
            row[rate] = 1000.0 * row[col] / words
        row["mean_sentiment"] = (
            a["sentiment_sum"] / a["sentiment_n"] if a["sentiment_n"] else None
        )
        row["mean_neg_share"] = (
            a["neg_share_sum"] / a["sentiment_n"] if a["sentiment_n"] else None
        )
        rows.append(row)
    return pd.DataFrame(rows)
