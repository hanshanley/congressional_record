"""Compact daily aggregates used to refresh the current Congress."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import pyarrow.parquet as pq

from analysis.ingest.schema import year_from_congress
from analysis.score.registry import HEADLINE_METRICS
from analysis.score.scorers import Scorers


DAILY_COLUMNS = [
    "date",
    "congress",
    "chamber",
    "party",
    "words",
    *(metric.raw_count for metric in HEADLINE_METRICS),
]
_TURN_COLUMNS = [
    "turn_id",
    "date",
    "congress",
    "chamber",
    "party",
    "is_procedural",
    "text",
]


def aggregate_turn_files(paths: Iterable[Path]) -> pd.DataFrame:
    """Score GovInfo turns into one compact row per date/chamber/party."""
    totals = defaultdict(lambda: {column: 0 for column in DAILY_COLUMNS[4:]})
    seen: set[str] = set()
    scorers = Scorers(use_sentiment=False)

    for path in sorted(paths):
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=10_000, columns=_TURN_COLUMNS):
            rows = batch.to_pydict()
            for index, text in enumerate(rows["text"]):
                turn_id = rows["turn_id"][index]
                if turn_id in seen:
                    continue
                seen.add(turn_id)
                date = str(rows["date"][index] or "")[:10]
                chamber = rows["chamber"][index]
                party = rows["party"][index]
                if (
                    not date
                    or rows["is_procedural"][index]
                    or chamber not in {"house", "senate"}
                    or party not in {"D", "R"}
                ):
                    continue
                scores = scorers.score_turn(text or "", party)
                key = (
                    date,
                    int(rows["congress"][index]),
                    chamber,
                    party,
                )
                bucket = totals[key]
                bucket["words"] += int(scores["n_words"])
                for metric in HEADLINE_METRICS:
                    bucket[metric.raw_count] += int(scores[metric.score_key])

    records = [
        {
            "date": date,
            "congress": congress,
            "chamber": chamber,
            "party": party,
            **counts,
        }
        for (date, congress, chamber, party), counts in sorted(totals.items())
    ]
    return pd.DataFrame(records, columns=DAILY_COLUMNS)


def replace_daily_window(
    existing: Optional[pd.DataFrame],
    fresh: pd.DataFrame,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Atomically replace every aggregate row in a recomputed date window."""
    if existing is None or existing.empty:
        retained = pd.DataFrame(columns=DAILY_COLUMNS)
    else:
        dates = existing["date"].astype(str).str[:10]
        retained = existing[(dates < start) | (dates > end)]
    combined = pd.concat([retained, fresh], ignore_index=True)
    if combined.empty:
        return pd.DataFrame(columns=DAILY_COLUMNS)
    return (
        combined.drop_duplicates(
            ["date", "congress", "chamber", "party"], keep="last"
        )
        .sort_values(["date", "chamber", "party"])
        .reset_index(drop=True)
    )


def load_daily(path: Path) -> Optional[pd.DataFrame]:
    return pd.read_parquet(path) if path.exists() else None


def save_daily(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = frame[DAILY_COLUMNS].sort_values(
        ["date", "chamber", "party"]
    ).reset_index(drop=True)
    if path.exists() and ordered.equals(pd.read_parquet(path)):
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    ordered.to_parquet(temporary, index=False)
    temporary.replace(path)


def merge_long_run_payload(base: dict, daily: pd.DataFrame) -> dict:
    """Replace Congress-years represented by daily rows in a long-run payload."""
    if daily.empty:
        return base

    daily = daily.copy()
    daily["year"] = daily["congress"].map(year_from_congress)
    hit_columns = [metric.raw_count for metric in HEADLINE_METRICS]
    chamber = daily.groupby(
        ["year", "party", "chamber"], as_index=False
    )[["words", *hit_columns]].sum()
    for metric in HEADLINE_METRICS:
        chamber[metric.rate] = (
            metric.scale
            * chamber[metric.raw_count]
            / chamber["words"].where(chamber["words"] > 0)
        ).fillna(0.0)

    replaced_years = set(chamber["year"].astype(int))
    old_chamber = pd.DataFrame(base["chamber_series"])
    old_chamber = old_chamber[~old_chamber["year"].isin(replaced_years)]
    chamber_columns = [
        "year",
        "party",
        "chamber",
        "words",
        *hit_columns,
        *(metric.rate for metric in HEADLINE_METRICS),
    ]
    merged_chamber = pd.concat(
        [old_chamber[chamber_columns], chamber[chamber_columns]],
        ignore_index=True,
    ).sort_values(["year", "party", "chamber"])

    aggregate = merged_chamber.groupby(
        ["year", "party"], as_index=False
    )[["words", *hit_columns]].sum()
    for metric in HEADLINE_METRICS:
        aggregate[metric.rate] = (
            metric.scale
            * aggregate[metric.raw_count]
            / aggregate["words"].where(aggregate["words"] > 0)
        ).fillna(0.0)
    aggregate_columns = [
        "year",
        "party",
        "words",
        *hit_columns,
        *(metric.rate for metric in HEADLINE_METRICS),
    ]

    payload = dict(base)
    payload["series"] = aggregate[aggregate_columns].to_dict("records")
    payload["chamber_series"] = merged_chamber[chamber_columns].to_dict("records")
    payload["first_year"] = int(aggregate["year"].min())
    payload["last_year"] = int(aggregate["year"].max())
    return payload
