"""Render civility time-series charts in the shared Substack style.

Re-aggregates the ``(congress, chamber, party)`` metrics up to ``(congress, party)``
by summing raw hit counts and words (so rates stay word-weighted), then draws the
key trends using :mod:`analysis.plotting`. Figures go to ``<out_dir>/reports/figures``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import pandas as pd

from analysis.aggregate import RATE_TO_HITCOL
from analysis.plotting import charts, theme

LOG = logging.getLogger("analysis.viz")

SOURCE_NOTE = (
    "Sources: Gentzkow-Shapiro-Taddy hein corpus (1873-2017) + GovInfo CREC (2017-present). "
    "Rates per 1,000 words; dotted line marks the hein\u2192GovInfo source boundary (2017)."
)
SOURCE_BOUNDARY_YEAR = 2017

# Top of the tight-layout rect, leaving headroom for the figure suptitle. A two-line
# suptitle (chamber overview) needs a little more room than a one-line one.
_RECT_TOP_1LINE = 0.97
_RECT_TOP_2LINE = 0.95

_HIT_COLS = [
    "comity_hits", "hostility_hits", "profanity_hits", "profanity_slurs_hits",
    "outgroup_refs", "democrat_party_pej", "directed_comity_hits",
    "directed_hostility_hits", "words",
]


def _add_rates(g: pd.DataFrame) -> pd.DataFrame:
    """Add per-1,000-word rate columns, derived from the same RATE_TO_HITCOL map the
    aggregate uses, so rate names/formula never diverge between the two modules."""
    w = g["words"].replace(0, 1)
    for rate, col in RATE_TO_HITCOL.items():
        if col in g.columns:
            g[rate] = 1000 * g[col] / w
    return g


def _by_congress_party(df: pd.DataFrame) -> pd.DataFrame:
    return _add_rates(df.groupby(["congress", "year", "party"], as_index=False)[_HIT_COLS].sum())


def _by_year_chamber_party(df: pd.DataFrame) -> pd.DataFrame:
    return _add_rates(
        df.groupby(["congress", "year", "chamber", "party"], as_index=False)[_HIT_COLS].sum()
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
    charts.marker_line(ax, SOURCE_BOUNDARY_YEAR)
    if label_ends and ends:
        ax.margins(x=0.13)  # headroom for the end-of-line party labels
        _place_end_labels(ax, ends)


# Chamber -> line style (party keeps its colour); lets one axis show party x chamber.
_CHAMBER_STYLE = {"house": "-", "senate": "--"}
_CHAMBER_LABEL = {"house": "House", "senate": "Senate"}


def _plot_by_chamber_party(ax, g: pd.DataFrame, col: str, parties=("D", "R"),
                           chambers=("house", "senate")) -> None:
    """Four series: party -> colour, chamber -> solid (House) / dashed (Senate)."""
    for party in parties:
        for chamber in chambers:
            sub = g[(g.party == party) & (g.chamber == chamber)].sort_values("year")
            if sub.empty:
                continue
            charts.line(ax, sub["year"], sub[col], color=theme.PARTY_COLORS[party],
                        label=f"{theme.PARTY_LABELS[party]} — {_CHAMBER_LABEL[chamber]}",
                        linestyle=_CHAMBER_STYLE[chamber], linewidth=2.0, markersize=3)
    charts.marker_line(ax, SOURCE_BOUNDARY_YEAR)


# (column, title, y-label) for each per-party panel/figure.
_PANELS = [
    ("comity_per_1k", "Comity / deference phrases", "hits per 1,000 words"),
    ("hostility_per_1k", "Hostility / attack language", "hits per 1,000 words"),
    ("directed_hostility_per_1k", "Hostility directed at the other party", "hits per 1,000 words"),
    ("outgroup_ref_per_1k", "References to the other party", "refs per 1,000 words"),
    ("profanity_per_1k", "Profanity", "hits per 1,000 words"),
    ("democrat_party_pej_per_1k", '"Democrat party" pejorative', "hits per 1,000 words"),
]


def _grid_overview(g: pd.DataFrame, figs_dir: Path, plot_fn, suptitle: str,
                   out_name: str, *, legend_fontsize: int, rect_top: float) -> Path:
    """Render a 2x3 small-multiples overview (one panel per metric in ``_PANELS``).

    ``plot_fn(ax, g, col)`` draws the series for one metric; the two overviews (by party,
    and by party x chamber) differ only in that callback, the suptitle, and spacing.
    """
    theme.apply()
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 3, figsize=(16, 9))
    for ax, (col, title, ylab) in zip(axes.flat, _PANELS):
        plot_fn(ax, g, col)
        charts.style_axes(ax, title, "Year", ylab)
    axes.flat[0].legend(loc="best", frameon=False, labelcolor=theme.TEXT, fontsize=legend_fontsize)
    fig.suptitle(suptitle, fontweight="bold")
    theme.source_note(fig, SOURCE_NOTE)
    fig.tight_layout(rect=(0, 0.03, 1, rect_top))
    out = figs_dir / out_name
    fig.savefig(out, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out


def _overview(g: pd.DataFrame, figs_dir: Path) -> Path:
    return _grid_overview(
        g, figs_dir, _plot_by_party,
        "The decline of comity between the parties \u2014 U.S. Congressional Record, 1873\u20132026",
        "overview.png", legend_fontsize=9, rect_top=_RECT_TOP_1LINE,
    )


def _asymmetry(g: pd.DataFrame, figs_dir: Path) -> Path:
    """Genuine D vs R asymmetry: (Democrats' - Republicans') directed hostility per 1k."""
    piv = g.pivot_table(index="year", columns="party", values="directed_hostility_per_1k")
    fig, ax = charts.new_figure(figsize=(10, 5.5))
    if {"D", "R"}.issubset(piv.columns):
        piv = piv.dropna(subset=["D", "R"]).sort_index()
        diff = piv["D"] - piv["R"]
        ax.fill_between(diff.index, 0, diff.clip(lower=0), color=theme.BLUE, alpha=0.5,
                        label="Democrats more hostile")
        ax.fill_between(diff.index, 0, diff.clip(upper=0), color=theme.ACCENT, alpha=0.5,
                        label="Republicans more hostile")
        charts.line(ax, diff.index, diff.values, color=theme.TEXT, label="D \u2212 R", linewidth=1.6)
    charts.marker_line(ax, SOURCE_BOUNDARY_YEAR)
    ax.axhline(0, color=theme.MUTED, linewidth=0.8)
    charts.style_axes(
        ax,
        "Directed-hostility asymmetry between the parties",
        "Year",
        "D \u2212 R directed hostility (per 1,000 words)",
        subtitle="Above zero: Democrats attack the other side more; below: Republicans do",
    )
    return charts.finish(fig, ax, figs_dir / "directed_asymmetry.png", source=SOURCE_NOTE)


def _overview_by_chamber(gc: pd.DataFrame, figs_dir: Path) -> Path:
    """Six-panel overview with party x chamber (colour=party, solid=House/dashed=Senate)."""
    return _grid_overview(
        gc, figs_dir, _plot_by_chamber_party,
        "Civility by party and chamber \u2014 U.S. Congressional Record, 1873\u20132026\n"
        "colour = party (blue D / red R), solid = House, dashed = Senate",
        "overview_by_chamber.png", legend_fontsize=8, rect_top=_RECT_TOP_2LINE,
    )


def render(metrics_path: Path, out_dir: Path) -> List[Path]:
    df = pd.read_parquet(metrics_path)
    g = _by_congress_party(df)
    gc = _by_year_chamber_party(df)
    figs_dir = out_dir / "reports" / "figures"
    figs_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = [_overview(g, figs_dir), _overview_by_chamber(gc, figs_dir)]

    for col, title, ylab in _PANELS:
        # overall (by party): clean markerless lines with direct end-of-line party labels
        fig, ax = charts.new_figure(figsize=(10, 5.5))
        _plot_by_party(ax, g, col, label_ends=True, marker=None, linewidth=2.6)
        charts.style_axes(ax, title, "Year", ylab,
                          subtitle="U.S. House & Senate combined, Democrats vs Republicans")
        written.append(charts.finish(fig, ax, figs_dir / f"{col}.png",
                                      source=SOURCE_NOTE, legend=False))

    # Per-metric party x chamber breakdowns for the three headline measures.
    for col, title, ylab in _PANELS[:3]:
        fig, ax = charts.new_figure(figsize=(10, 5.5))
        _plot_by_chamber_party(ax, gc, col)
        charts.style_axes(ax, f"{title} \u2014 by party & chamber", "Year", ylab,
                          subtitle="solid = House, dashed = Senate")
        written.append(charts.finish(fig, ax, figs_dir / f"{col}_by_chamber.png", source=SOURCE_NOTE))

    written.append(_asymmetry(g, figs_dir))

    tbl_dir = out_dir / "reports" / "tables"
    tbl_dir.mkdir(parents=True, exist_ok=True)
    g.to_csv(tbl_dir / "metrics_by_congress_party.csv", index=False)
    gc.to_csv(tbl_dir / "metrics_by_congress_chamber_party.csv", index=False)

    LOG.info("wrote %d figures -> %s", len(written), figs_dir)
    return written
