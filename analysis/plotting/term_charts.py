"""One publication graph per most-used profanity family in attributed member speech."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import tempfile

import pandas as pd

from analysis.speakers import profanity_family_timeseries
from . import charts, theme

LOG = logging.getLogger(__name__)


def render(daily: pd.DataFrame, out_dir: Path, *, top: int = 5) -> dict:
    """Publish graphs and their exact counts; refuse incomplete stored term data."""
    payload = profanity_family_timeseries(daily, top=top)
    if not daily.empty:
        payload["updated_through"] = str(daily["date"].max())[:10]
    if not payload["terms"]:
        LOG.warning("no accepted D/R profanity families available for individual graphs")
    out_dir.mkdir(parents=True, exist_ok=True)
    coverage = payload["coverage"]
    first_year = coverage.get("first_date", "")[:4]
    last_year = coverage.get("last_date", "")[:4]
    written = set()
    rows = []
    with tempfile.TemporaryDirectory(prefix=".profanity-terms-", dir=out_dir.parent) as temporary:
        staging = Path(temporary)
        for item in payload["terms"]:
            filename = re.sub(r"[^a-z0-9]+", "_", item["term"].lower()).strip("_")
            if not filename:
                raise ValueError(f"invalid profanity-family filename: {item['term']!r}")
            item["filename"] = f"{filename}_per_100k.png"
            if item["filename"] in written:
                raise ValueError(f"duplicate profanity-family filename: {item['filename']}")
            series = pd.DataFrame(item["series"])
            fig, ax = charts.new_figure(figsize=(10, 5.5))
            ends = []
            for party in ("D", "R"):
                sub = series[series["party"] == party].sort_values("year")
                if sub.empty:
                    continue
                charts.line(
                    ax, sub["year"], sub["rate_per_100k"],
                    color=theme.PARTY_COLORS[party], label=theme.PARTY_LABELS[party],
                    marker=None, linestyle="--" if party == "R" else "-", linewidth=2.6,
                )
                end = sub.iloc[-1]
                ends.append((
                    int(end["year"]), float(end["rate_per_100k"]),
                    theme.PARTY_LABELS[party], theme.PARTY_COLORS[party],
                ))
            charts.fit_time_axis(ax, right_padding=0.13)
            first, last = int(series["year"].min()), int(series["year"].max())
            ticks = list(range(5 * ((first + 4) // 5), last + 1, 5))
            if last not in ticks:
                if ticks and last - ticks[-1] < 3:
                    ticks.pop()
                ticks.append(last)
            ax.set_xticks(ticks)
            ax.set_ylim(bottom=0)
            charts.place_end_labels(ax, ends)
            charts.style_axes(
                ax, f"{item['label']} \u2014 word family", "Year",
                "Hits per 100,000 spoken words",
                subtitle=f"U.S. House & Senate, Democrats vs Republicans \u2014 {first_year}\u2013{last_year}",
            )
            charts.finish(
                fig, ax, staging / item["filename"], legend=False,
                source=f"Source: GovInfo Congressional Record ({first_year}-{last_year}).",
            )
            written.add(item["filename"])
            rows.extend({"term": item["term"], **row} for row in item["series"])
        (staging / "index.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        pd.DataFrame(rows, columns=[
            "term", "year", "party", "words", "hits", "rate_per_100k",
        ]).to_csv(staging / "timeseries.csv", index=False)
        pd.DataFrame(
            [
                {"term": item["term"], **row}
                for item in payload.get("codebook", [])
                for row in item["series"]
            ],
            columns=["term", "year", "party", "words", "hits", "rate_per_100k"],
        ).to_csv(staging / "codebook_timeseries.csv", index=False)
        for path in staging.iterdir():
            os.replace(path, out_dir / path.name)
        for old in out_dir.glob("*_per_100k.png"):
            if old.name not in written:
                old.unlink()
    LOG.info("wrote %d individual profanity-family graphs to %s", len(written), out_dir)
    return payload
