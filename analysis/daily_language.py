"""Compact daily aggregates used to refresh the current Congress."""

from __future__ import annotations

from collections import defaultdict
import json
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
    scorers = Scorers()

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
                scores = scorers.score_turn(text or "")
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
    """Load daily aggregates; measures added after a row was scored load as nulls."""
    if not path.exists():
        return None
    frame = pd.read_parquet(path)
    for column in DAILY_COLUMNS:
        if column not in frame:
            frame[column] = pd.array([pd.NA] * len(frame), dtype="Int64")
    return frame[DAILY_COLUMNS]


def restrict_to_complete_metrics(payload: dict) -> dict:
    """Keep only current headline measures that every long-run row reports.

    A measure added to the codebook appears once the historical rebuild has scored the
    whole series; until then it is omitted rather than drawn with missing years.
    """
    rows = [*payload.get("series", []), *payload.get("chamber_series", [])]
    complete = [
        metric for metric in HEADLINE_METRICS
        if rows and all(
            row.get(metric.raw_count) is not None and row.get(metric.rate) is not None
            for row in rows
        )
    ]
    # Rows keep only their keys and the published measures, so retired fields are dropped.
    keep = {"year", "party", "chamber", "words"}
    keep.update(field for metric in complete for field in (metric.raw_count, metric.rate))
    result = dict(payload)
    for name in ("series", "chamber_series"):
        result[name] = [
            {key: value for key, value in row.items() if key in keep}
            for row in payload.get(name, [])
        ]
    result["metrics"] = {
        metric.rate: {
            "rate": metric.rate,
            "hits": metric.raw_count,
            "label": metric.title,
            "units": metric.units,
            "polarity": metric.polarity,
        }
        for metric in complete
    }
    return result


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
        return restrict_to_complete_metrics(base)

    daily = daily.copy()
    daily["year"] = daily["congress"].map(year_from_congress)
    # Only measures scored on every daily row are merged; the rest stay absent so
    # restrict_to_complete_metrics() can tell they are not yet available.
    complete = tuple(
        metric for metric in HEADLINE_METRICS
        if metric.raw_count in daily and not daily[metric.raw_count].isna().any()
    )
    hit_columns = [metric.raw_count for metric in complete]
    chamber = daily.groupby(
        ["year", "party", "chamber"], as_index=False
    )[["words", *hit_columns]].sum()
    for metric in complete:
        chamber[metric.rate] = (
            metric.scale
            * chamber[metric.raw_count]
            / chamber["words"].where(chamber["words"] > 0)
        ).fillna(0.0)

    replaced_years = set(chamber["year"].astype(int))
    chamber_columns = [
        "year",
        "party",
        "chamber",
        "words",
        *hit_columns,
        *(metric.rate for metric in complete),
    ]
    current_aggregate = chamber.groupby(
        ["year", "party"], as_index=False
    )[["words", *hit_columns]].sum()
    for metric in complete:
        current_aggregate[metric.rate] = (
            metric.scale
            * current_aggregate[metric.raw_count]
            / current_aggregate["words"].where(current_aggregate["words"] > 0)
        ).fillna(0.0)
    aggregate_columns = [
        "year",
        "party",
        "words",
        *hit_columns,
        *(metric.rate for metric in complete),
    ]
    payload = dict(base)
    current_series = json.loads(
        current_aggregate[aggregate_columns].to_json(orient="records")
    )
    current_chamber_series = json.loads(
        chamber[chamber_columns].to_json(orient="records")
    )
    payload["series"] = sorted(
        [
            row for row in base["series"]
            if int(row["year"]) not in replaced_years
        ]
        + current_series,
        key=lambda row: (row["year"], row["party"]),
    )
    payload["chamber_series"] = sorted(
        [
            row for row in base["chamber_series"]
            if int(row["year"]) not in replaced_years
        ]
        + current_chamber_series,
        key=lambda row: (row["year"], row["party"], row["chamber"]),
    )
    payload["first_year"] = min(row["year"] for row in payload["series"])
    payload["last_year"] = max(row["year"] for row in payload["series"])
    return restrict_to_complete_metrics(payload)
