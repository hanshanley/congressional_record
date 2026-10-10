"""Render courtesy, profanity, and slur time-series charts in the shared style.

Re-aggregates the ``(congress, chamber, party)`` metrics up to ``(congress, party)``
by summing raw hit counts and words (so rates stay word-weighted), then draws the
key trends using :mod:`analysis.plotting`. Figures go to ``<out_dir>/figures``.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import json
from datetime import date
from pathlib import Path
from typing import List

import pandas as pd

from analysis.aggregate import RATE_TO_HITCOL
from analysis.ingest.schema import congress_from_year, year_from_congress
from analysis.plotting import charts, theme
from analysis.score.registry import CHAMBER_METRICS, HEADLINE_METRICS, METRICS

LOG = logging.getLogger("analysis.viz")

SOURCE_NOTE = (
    "Sources: Stanford Hein (1873-2017) + GovInfo CREC (2017-present)."
)
# The year the primary source switches from Hein to GovInfo. No longer drawn on the
# charts, but still reported in the source note's date ranges and used by the
# calibration diagnostic, which exists specifically to quantify the transition.
SOURCE_BOUNDARY_YEAR = 2017

# Top of the tight-layout rect, leaving headroom for the figure suptitle. A two-line
# suptitle (chamber overview) needs a little more room than a one-line one.
_RECT_TOP_1LINE = 0.97
_RECT_TOP_2LINE = 0.94

_HIT_COLS = list(dict.fromkeys([*RATE_TO_HITCOL.values(), "words"]))


def _available_hit_cols(df: pd.DataFrame) -> List[str]:
    """Raw count columns available in a metrics frame (supports older fixtures)."""
    return [col for col in _HIT_COLS if col in df.columns]


def _add_rates(g: pd.DataFrame) -> pd.DataFrame:
    """Add per-1,000-word rate columns, derived from the same RATE_TO_HITCOL map the
    aggregate uses, so rate names/formula never diverge between the two modules."""
    w = g["words"].replace(0, 1)
    for rate, col in RATE_TO_HITCOL.items():
        if col in g.columns:
            g[rate] = 1000 * g[col] / w
    return g


def _by_congress_party(df: pd.DataFrame) -> pd.DataFrame:
    return _add_rates(
        df.groupby(["congress", "year", "party"], as_index=False)[_available_hit_cols(df)].sum()
    )


def _by_year_chamber_party(df: pd.DataFrame) -> pd.DataFrame:
    return _add_rates(
        df.groupby(
            ["congress", "year", "chamber", "party"], as_index=False
        )[_available_hit_cols(df)].sum()
    )


def _place_end_labels(ax, ends) -> None:
    """Draw end-of-line party labels, nudged apart vertically when series converge."""
    ymin, ymax = ax.get_ylim()
    gap = (ymax - ymin) * 0.055
    ordered = sorted(ends, key=lambda e: e[2])  # by end y-value, low -> high
    ys = [e[2] for e in ordered]
    for i in range(1, len(ys)):
        if ys[i] - ys[i - 1] < gap:
            ys[i] = ys[i - 1] + gap
    for (party, x, _), y in zip(ordered, ys):
        charts.end_label(ax, x, y, theme.PARTY_LABELS[party], theme.PARTY_COLORS[party])


def _plot_by_party(ax, g: pd.DataFrame, col: str, parties=("D", "R"), *,
                   label_ends: bool = False, **line_kw) -> None:
    ends = []
    for party in parties:
        sub = g[g.party == party].sort_values("year")
        if sub.empty:
            continue
        charts.line(ax, sub["year"], sub[col], color=theme.PARTY_COLORS[party],
                    label=theme.PARTY_LABELS[party], **line_kw)
        ends.append((party, sub["year"].iloc[-1], float(sub[col].iloc[-1])))
    charts.fit_time_axis(ax, right_padding=0.13 if label_ends else 0.0)
    if label_ends and ends:
        _place_end_labels(ax, ends)


def _load_provenance(metrics_path: Path, *, first_year: int | None = None) -> tuple[int, str]:
    """Build plot provenance from aggregate metadata, with a legacy fallback."""
    metadata_path = metrics_path.parent.parent / "coverage" / "source_metadata.json"
    metadata = (
        json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata_path.exists() else {}
    )
    boundary = int(metadata.get("primary_boundary_year") or SOURCE_BOUNDARY_YEAR)
    sources = metadata.get("sources", [])
    # Hein comes in two editions (bound 1873-1980, daily 1981-2017) and GovInfo also holds
    # 1994-2016 overlap rows for calibration, so ranges span every edition and follow the
    # primary rule (Hein through Congress 114, GovInfo from 115). Metadata years are each
    # Congress's convening year, so a range ends when its last Congress ends.
    hein = [item for item in sources if item["source"].startswith("hein_")]
    govinfo = [item for item in sources if item["source"].startswith("govinfo")]
    hein_start = min((year_from_congress(i["min_congress"]) for i in hein), default=1873)
    hein_last = min(114, max((i["max_congress"] for i in hein), default=114))
    govinfo_last = max((i["max_congress"] for i in govinfo), default=None)
    govinfo_end = (
        "present"
        if govinfo_last is None or govinfo_last >= congress_from_year(date.today().year)
        else str(year_from_congress(govinfo_last + 1))
    )
    hein_end = year_from_congress(hein_last + 1)
    govinfo_start = boundary
    if first_year is not None:
        hein_start = max(hein_start, first_year)
        govinfo_start = max(govinfo_start, first_year)
    source_labels = []
    if hein_start < hein_end:
        source_labels.append(f"Stanford Hein ({hein_start}-{hein_end})")
    source_labels.append(f"GovInfo CREC ({govinfo_start}-{govinfo_end})")
    note = "Sources: " + " + ".join(source_labels) + "."
    return boundary, note


# (column, title, y-label) for each per-party panel/figure.
_PANELS = [(metric.rate, metric.title, metric.units) for metric in HEADLINE_METRICS]

_SUPPLEMENTAL_PANELS = [
    (metric.rate, metric.title, metric.units)
    for metric in METRICS if not metric.headline
]

_CHAMBER_PANELS = [(metric.rate, metric.title, metric.units) for metric in CHAMBER_METRICS]


def _grid_overview(g: pd.DataFrame, figs_dir: Path, plot_fn, suptitle: str,
                   out_name: str, *, legend_fontsize: int, rect_top: float,
                   panels: List[tuple] = _PANELS, source_note: str | None = None,
                   last50: bool = False) -> Path:
    """Render a one-row small-multiples overview (one panel per metric in ``_PANELS``).

    ``plot_fn(ax, g, col)`` draws the series for one metric; the two overviews (by party,
    and by party x chamber) differ only in that callback, the suptitle, and spacing.
    """
    theme.apply()
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(
        1, len(panels), figsize=(5.4 * len(panels) + 0.4, 5.2), squeeze=False
    )
    for ax, (col, title, ylab) in zip(axes.flat, panels):
        plot_fn(ax, g, col)
        charts.style_axes(ax, title, "Year", ylab)
        if last50:
            _set_recent_year_ticks(ax, g)
            ax.tick_params(axis="x", labelsize=9)
    axes.flat[0].legend(loc="best", frameon=False, labelcolor=theme.TEXT, fontsize=legend_fontsize)
    fig.suptitle(suptitle, fontweight="bold")
    theme.source_note(fig, SOURCE_NOTE if source_note is None else source_note)
    fig.tight_layout(rect=(0, 0.03, 1, rect_top))
    out = figs_dir / out_name
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out


def _span(g: pd.DataFrame) -> str:
    return f"{int(g['year'].min())}\u2013present"


def _set_recent_year_ticks(ax, frame: pd.DataFrame) -> None:
    first_year = int(frame["year"].min())
    last_year = int(frame["year"].max())
    ticks = list(range(5 * ((first_year + 4) // 5), last_year + 1, 5))
    if last_year not in ticks:
        ticks.append(last_year)
    ax.set_xticks(ticks)


def _overview(g: pd.DataFrame, figs_dir: Path, panels: List[tuple] = _PANELS, *,
              suffix: str = "", source_note: str | None = None) -> Path:
    return _grid_overview(
        g, figs_dir, _plot_by_party,
        f"Floor language in the U.S. Congressional Record, {_span(g)}",
        f"overview{suffix}.png", legend_fontsize=9, rect_top=_RECT_TOP_1LINE,
        panels=panels, source_note=source_note, last50=bool(suffix),
    )


def _overview_for_chamber(gc: pd.DataFrame, figs_dir: Path, chamber: str,
                          panels: List[tuple] = _PANELS, *, suffix: str = "",
                          source_note: str | None = None) -> Path:
    """Headline-measure overview of one chamber, Democrats vs Republicans.

    Splitting the chambers into separate figures replaces the previous combined
    version, which crammed four series into every panel and made the House and
    Senate lines fight for the same space.
    """
    label = theme.CHAMBER_LABELS[chamber]
    sub = gc[gc["chamber"] == chamber]
    return _grid_overview(
        sub, figs_dir, _plot_by_party,
        f"Floor language in the U.S. {label} \u2014 Congressional Record, {_span(sub)}\n"
        "Democrats vs Republicans",
        f"overview_{chamber}{suffix}.png", legend_fontsize=9, rect_top=_RECT_TOP_2LINE,
        panels=panels, source_note=source_note, last50=bool(suffix),
    )


def render(metrics_path: Path, out_dir: Path) -> List[Path]:
    global SOURCE_BOUNDARY_YEAR, SOURCE_NOTE
    SOURCE_BOUNDARY_YEAR, SOURCE_NOTE = _load_provenance(metrics_path)
    df = pd.read_parquet(metrics_path)
    df = df[df["chamber"].isin(["house", "senate"])].copy()
    g = _by_congress_party(df)
    gc = _by_year_chamber_party(df)
    # A measure is drawn only when the metrics table scored it; a codebook addition
    # appears after the historical rescore instead of as a flat line of false zeros.
    def present(panels: List[tuple]) -> List[tuple]:
        return [panel for panel in panels if RATE_TO_HITCOL[panel[0]] in df.columns]

    headline, supplemental, chamber_panels = (
        present(_PANELS), present(_SUPPLEMENTAL_PANELS), present(_CHAMBER_PANELS)
    )
    if not headline:
        raise ValueError(f"{metrics_path} has no headline measure columns")
    skipped = [col for col, _, _ in _PANELS if col not in {p[0] for p in headline}]
    if skipped:
        LOG.warning("not drawing measures missing from the metrics table: %s", skipped)
    figs_dir = out_dir / "figures"
    figs_dir.mkdir(parents=True, exist_ok=True)
    temp_figs = Path(tempfile.mkdtemp(prefix=".figures-", dir=figs_dir.parent))
    try:
        written: List[Path] = []
        scopes = [("", g, gc)]
        current_year = date.today().year
        # Inclusive calendar-year window: in 2026, 1977 through 2026.
        first_year = current_year - 49
        recent_g = g[g["year"].between(first_year, current_year)]
        recent_gc = gc[gc["year"].between(first_year, current_year)]
        if not recent_g.empty:
            scopes.append(("_last50", recent_g, recent_gc))

        for suffix, period_g, period_gc in scopes:
            source_note = (
                _load_provenance(metrics_path, first_year=int(period_g["year"].min()))[1]
                if suffix else SOURCE_NOTE
            )
            written.append(_overview(
                period_g, temp_figs, headline, suffix=suffix, source_note=source_note
            ))
            for chamber in ("house", "senate"):
                if not period_gc[period_gc["chamber"] == chamber].empty:
                    written.append(_overview_for_chamber(
                        period_gc, temp_figs, chamber, headline,
                        suffix=suffix, source_note=source_note,
                    ))

            for col, title, ylab in [*headline, *supplemental]:
                fig, ax = charts.new_figure(figsize=(10, 5.5))
                _plot_by_party(ax, period_g, col, label_ends=True, marker=None, linewidth=2.6)
                subtitle = "U.S. House & Senate combined, Democrats vs Republicans"
                if suffix:
                    subtitle += f" \u2014 {_span(period_g)}"
                charts.style_axes(ax, title, "Year", ylab, subtitle=subtitle)
                if suffix:
                    _set_recent_year_ticks(ax, period_g)
                written.append(charts.finish(
                    fig, ax, temp_figs / f"{col}{suffix}.png",
                    source=source_note, legend=False,
                ))

            for col, title, ylab in chamber_panels:
                for chamber in ("house", "senate"):
                    sub = period_gc[period_gc["chamber"] == chamber]
                    if sub.empty:
                        continue
                    label = theme.CHAMBER_LABELS[chamber]
                    fig, ax = charts.new_figure(figsize=(10, 5.5))
                    _plot_by_party(ax, sub, col, label_ends=True, marker=None, linewidth=2.6)
                    subtitle = f"U.S. {label}, Democrats vs Republicans"
                    if suffix:
                        subtitle += f" \u2014 {_span(sub)}"
                    charts.style_axes(ax, f"{title} \u2014 {label}", "Year", ylab,
                                      subtitle=subtitle)
                    if suffix:
                        _set_recent_year_ticks(ax, sub)
                    written.append(charts.finish(
                        fig, ax, temp_figs / f"{col}_{chamber}{suffix}.png",
                        source=source_note, legend=False,
                    ))

        data_root = metrics_path.parents[2]
        tbl_dir = data_root / "reports" / "tables"
        tbl_dir.mkdir(parents=True, exist_ok=True)
        g.to_csv(tbl_dir / "metrics_by_congress_party.csv", index=False)
        gc.to_csv(tbl_dir / "metrics_by_congress_chamber_party.csv", index=False)

        new_names = {path.name for path in written}
        for path in written:
            os.replace(path, figs_dir / path.name)
        for old_figure in figs_dir.glob("*.png"):
            if old_figure.name not in new_names:
                old_figure.unlink()
    finally:
        shutil.rmtree(temp_figs, ignore_errors=True)

    final_paths = [figs_dir / path.name for path in written]
    LOG.info("wrote %d figures -> %s", len(final_paths), figs_dir)
    return final_paths
