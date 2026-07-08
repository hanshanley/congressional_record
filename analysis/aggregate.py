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
    "sentiment_sum", "sentiment_n",
]

_READ_COLS = ["congress", "chamber", "party", "is_procedural", "text"]


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
                    a["sentiment_sum"] += s["sentiment"]
                    a["sentiment_n"] += 1
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
        per1k = lambda x: 1000.0 * x / words  # noqa: E731
        rows.append(
            {
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
                # convenience rates at this (congress, chamber, party) granularity
                "comity_per_1k": per1k(a["comity_hits"]),
                "hostility_per_1k": per1k(a["hostility_hits"]),
                "profanity_per_1k": per1k(a["profanity_hits"]),
                "profanity_mild_per_1k": per1k(a["profanity_mild"]),
                "profanity_strong_per_1k": per1k(a["profanity_strong"]),
                "profanity_slurs_per_1k": per1k(a["profanity_slurs"]),
                "outgroup_ref_per_1k": per1k(a["outgroup_refs"]),
                "democrat_party_pej_per_1k": per1k(a["democrat_party_pej"]),
                "directed_comity_per_1k": per1k(a["directed_comity_hits"]),
                "directed_hostility_per_1k": per1k(a["directed_hostility_hits"]),
                "mean_sentiment": (
                    a["sentiment_sum"] / a["sentiment_n"] if a["sentiment_n"] else None
                ),
            }
        )
    return pd.DataFrame(rows)
