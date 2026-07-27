#!/usr/bin/env python3
"""build_site.py -- render the static site from the committed speaker table.

Reads ``data/site/speaker_daily/`` (small, committed) and writes a
self-contained static site to ``site/``:

    site/index.html                 leaderboard + trend, styled like the figures
    site/data/leaderboard.json      ranked members, for embedding in a blog
    site/data/timeseries.json       chamber-level rate over time
    site/data/meta.json             build stamp, thresholds, coverage
    site/figures/*.png              charts matching the project theme

Nothing here touches the 6 GB turn corpus, so it runs in seconds in CI.

Examples
--------
    python scripts/build_site.py
    python scripts/build_site.py --congress 119 --top 20
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import matplotlib

matplotlib.use("Agg")

from matplotlib.ticker import MaxNLocator  # noqa: E402
import pandas as pd  # noqa: E402

from analysis.plotting import charts, theme  # noqa: E402
from analysis.speakers import leaderboard, load_daily, timeseries  # noqa: E402

LOG = logging.getLogger("build_site")

DAILY_PATH = ROOT / "data" / "site" / "speaker_daily"
SITE_DIR = ROOT / "site"

CAVEATS = [
    "Counts come from a deliberately narrow, hand-curated profanity list, not a broad "
    "word list, so ordinary words are never counted as swearing.",
    "Passages the Record marks as quotations are excluded \u2014 a member reading someone "
    "else's words is not swearing. Those hits are counted separately and shown below.",
    "Only remarks attributable to a specific member by Bioguide ID are counted. "
    "Procedural speech by the Chair and presiding officers is excluded.",
    "Members below the word threshold are omitted entirely: a rate computed on a few "
    "hundred words of floor time is noise, not a ranking.",
    "The Congressional Record is a lightly edited transcript. It is not a verbatim "
    "record, and members may revise remarks.",
]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--daily", type=Path, default=DAILY_PATH, help="Speaker daily table.")
    p.add_argument("--out", type=Path, default=SITE_DIR, help="Site output directory.")
    p.add_argument("--congress", type=int, default=None, help="Restrict to one Congress.")
    p.add_argument("--top", type=int, default=25, help="Leaderboard length.")
    p.add_argument(
        "--min-words",
        type=int,
        default=25_000,
        help="Minimum attributed words to appear on the leaderboard (default: 25000).",
    )
    return p.parse_args(argv)


def _chart_leaderboard(board: pd.DataFrame, figs: Path) -> Path:
    fig, ax = charts.new_figure(figsize=(11, max(4.0, 0.42 * len(board) + 2)))
    ordered = board.iloc[::-1]
    colors = [theme.PARTY_COLORS.get(p, theme.MUTED) for p in ordered["party"]]
    ax.barh(ordered["speaker_name"], ordered["profanity_per_100k"], color=colors, height=0.72)
    charts.style_axes(
        ax,
        "Most profane members of Congress",
        "Profanity per 100,000 spoken words",
        "",
        subtitle=f"Quotations excluded; members with under {int(board.attrs.get('min_words', 0)):,} "
                 "attributed words omitted",
    )
    ax.grid(axis="x", linestyle="-", linewidth=0.5)
    ax.grid(axis="y", visible=False)
    return charts.finish(fig, ax, figs / "leaderboard.png",
                         source=board.attrs.get("source_note", ""), legend=False)


def _chart_trend(series: pd.DataFrame, figs: Path) -> Path:
    fig, ax = charts.new_figure(figsize=(11, 5.5))
    # Chamber-only chart: use politically neutral palette colours rather than the
    # party blue/red, which would wrongly imply a partisan reading.
    chamber_hue = {"house": theme.GREEN, "senate": theme.GOLD}
    for chamber in ("house", "senate"):
        sub = series[series["chamber"] == chamber].sort_values("period")
        if sub.empty:
            continue
        style = theme.CHAMBER_STYLE[chamber]
        charts.line(
            ax, sub["period"].astype(int), sub["profanity_per_100k"],
            color=chamber_hue[chamber],
            label=theme.CHAMBER_LABELS[chamber],
            linestyle=style["linestyle"], marker=style["marker"],
            linewidth=style["linewidth"], markersize=style["markersize"],
        )
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    charts.style_axes(
        ax, "Profanity on the floor over time", "Year",
        "Hits per 100,000 spoken words",
        subtitle="Attributed, non-procedural remarks; quotations excluded",
    )
    return charts.finish(fig, ax, figs / "trend.png",
                         source=series.attrs.get("source_note", ""))


def _render_html(board: pd.DataFrame, series: pd.DataFrame, meta: dict) -> str:
    rows = []
    for row in board.itertuples():
        quoted = (
            f'<span class="muted">{row.profanity_quoted_hits}</span>'
            if row.profanity_quoted_hits else '<span class="muted">0</span>'
        )
        rows.append(
            "<tr>"
            f"<td class='rank'>{row.rank}</td>"
            f"<td>{html.escape(str(row.speaker_name))}</td>"
            f"<td>{html.escape(str(row.party))}</td>"
            f"<td>{html.escape(str(row.state))}</td>"
            f"<td>{html.escape(str(row.chamber).title())}</td>"
            f"<td class='num'>{row.profanity_per_100k:.1f}</td>"
            f"<td class='num'>{row.profanity_hits:,}</td>"
            f"<td class='num'>{quoted}</td>"
            f"<td class='num'>{row.words:,}</td>"
            "</tr>"
        )
    caveats = "".join(f"<li>{html.escape(c)}</li>" for c in CAVEATS)
    scope = f"Congress {meta['congress']}" if meta.get("congress") else "all Congresses"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Profanity in the Congressional Record</title>
<meta name="description" content="Which members of Congress swear most on the floor,
measured from the Congressional Record and updated automatically.">
<style>
  :root {{ --bg:{theme.BG}; --text:{theme.TEXT}; --muted:{theme.MUTED};
           --grid:{theme.GRID}; --blue:{theme.BLUE}; --accent:{theme.ACCENT}; }}
  body {{ background:var(--bg); color:var(--text); font-family:Georgia,'Times New Roman',serif;
          margin:0 auto; padding:2.5rem 1.25rem 4rem; max-width:60rem; line-height:1.55; }}
  h1 {{ font-size:2.1rem; margin:0 0 .35rem; }}
  h2 {{ font-size:1.35rem; margin:2.5rem 0 .75rem; }}
  .sub {{ color:var(--muted); margin:0 0 2rem; }}
  table {{ border-collapse:collapse; width:100%; font-size:.95rem; }}
  th, td {{ padding:.5rem .6rem; border-bottom:1px solid var(--grid); text-align:left; }}
  th {{ font-weight:bold; border-bottom:2px solid var(--grid); }}
  td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  td.rank {{ color:var(--muted); width:2.5rem; }}
  .muted {{ color:var(--muted); }}
  img {{ width:100%; height:auto; margin:1rem 0; }}
  ul {{ padding-left:1.1rem; }}
  li {{ margin:.4rem 0; }}
  footer {{ margin-top:3rem; color:var(--muted); font-size:.85rem; font-style:italic; }}
  code {{ background:#EFEDE8; padding:.1rem .3rem; }}
</style>
</head>
<body>
<h1>Profanity in the Congressional Record</h1>
<p class="sub">Who swears most on the floor of the U.S. Congress, measured from the
official record &mdash; {scope}. Rebuilt automatically; last updated
{html.escape(meta['generated_utc'])}.</p>

<img src="figures/leaderboard.png" alt="Bar chart ranking members by profanity per 100,000 spoken words">

<h2>The leaderboard</h2>
<table>
<thead><tr>
  <th>#</th><th>Member</th><th>Party</th><th>State</th><th>Chamber</th>
  <th class="num">Per 100k words</th><th class="num">Hits</th>
  <th class="num">Quoted (excl.)</th><th class="num">Words</th>
</tr></thead>
<tbody>{''.join(rows)}</tbody>
</table>
<p class="muted">Ranked by rate, not raw count, so prolific speakers are not penalised.
Minimum {meta['min_words']:,} attributed words. {meta['eligible']:,} of
{meta['members']:,} members met the threshold.</p>

<h2>Over time</h2>
<img src="figures/trend.png" alt="Line chart of floor profanity rate over time by chamber">

<h2>How this is measured &mdash; and what it does not mean</h2>
<ul>{caveats}</ul>

<h2>Data</h2>
<p>Machine-readable output: <a href="data/leaderboard.json">leaderboard.json</a>,
<a href="data/timeseries.json">timeseries.json</a>, <a href="data/meta.json">meta.json</a>.</p>

<footer>Source: U.S. Congressional Record via GovInfo and the Stanford Hein corpus.
Generated from {meta['rows']:,} member-days spanning {html.escape(meta['first_date'])}
to {html.escape(meta['last_date'])}.</footer>
</body>
</html>
"""


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    daily = load_daily(args.daily)
    if daily is None or daily.empty:
        LOG.error("no speaker table at %s; run scripts/update.py first", args.daily)
        return 1

    scoped = daily if args.congress is None else daily[daily["congress"] == args.congress]
    if scoped.empty:
        LOG.error("no rows for congress %s", args.congress)
        return 1

    board = leaderboard(scoped, min_words=args.min_words, top=args.top)
    if board.empty:
        LOG.error("no member cleared the %d-word threshold", args.min_words)
        return 1
    series = timeseries(daily)

    source_note = (
        "Source: U.S. Congressional Record (GovInfo CREC / Stanford Hein). "
        "Attributed non-procedural remarks; quotations excluded."
    )
    board.attrs.update(min_words=args.min_words, source_note=source_note)
    series.attrs.update(source_note=source_note)

    out: Path = args.out
    figs = out / "figures"
    data = out / "data"
    figs.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)

    _chart_leaderboard(board, figs)
    _chart_trend(series, figs)

    meta = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "congress": args.congress,
        "min_words": args.min_words,
        "top": args.top,
        "members": int(scoped["bioguide"].nunique()),
        "eligible": int(len(board)),
        "rows": int(len(scoped)),
        "first_date": str(scoped["date"].min()),
        "last_date": str(scoped["date"].max()),
        "caveats": CAVEATS,
    }

    (data / "leaderboard.json").write_text(
        board.to_json(orient="records", indent=2), encoding="utf-8"
    )
    (data / "timeseries.json").write_text(
        series.to_json(orient="records", indent=2), encoding="utf-8"
    )
    (data / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    (out / "index.html").write_text(_render_html(board, series, meta), encoding="utf-8")

    LOG.info("site written to %s (%d ranked members)", out, len(board))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
