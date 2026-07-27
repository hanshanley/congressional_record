"""Render civility time-series charts in the shared Substack style.

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
from pathlib import Path
from typing import List

import pandas as pd

from analysis.aggregate import CONTEXT_RATE_TO_COUNT, RATE_TO_HITCOL
from analysis.plotting import charts, theme
from analysis.score.registry import CHAMBER_METRICS, HEADLINE_METRICS

LOG = logging.getLogger("analysis.viz")

SOURCE_NOTE = (
    "Sources: Stanford hein (1873-2017) + GovInfo CREC (2017-present). House/Senate only; "
    "Extensions excluded. Units shown on y-axis. GovInfo party "
    "coverage varies; see coverage/turn_coverage.csv."
)
# The year the primary source switches from Hein to GovInfo. No longer drawn on the
# charts, but still reported in the source note's date ranges and used by the
# calibration diagnostic, which exists specifically to quantify the transition.
SOURCE_BOUNDARY_YEAR = 2017

# Top of the tight-layout rect, leaving headroom for the figure suptitle. A two-line
# suptitle (chamber overview) needs a little more room than a one-line one.
_RECT_TOP_1LINE = 0.97
_RECT_TOP_2LINE = 0.95

_HIT_COLS = list(dict.fromkeys([
    *RATE_TO_HITCOL.values(), *CONTEXT_RATE_TO_COUNT.values(), "words"
]))


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
    refs = g["outgroup_refs"].where(g["outgroup_refs"] != 0) if "outgroup_refs" in g.columns else None
    if refs is not None:
        for rate, col in CONTEXT_RATE_TO_COUNT.items():
            if col in g.columns:
                g[rate] = (100 * g[col] / refs).fillna(0.0)
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
    if label_ends and ends:
        ax.margins(x=0.13)  # headroom for the end-of-line party labels
        _place_end_labels(ax, ends)


def _load_provenance(metrics_path: Path) -> tuple[int, str]:
    """Build plot provenance from aggregate metadata, with a legacy fallback."""
    metadata_path = metrics_path.parent.parent / "coverage" / "source_metadata.json"
    if not metadata_path.exists():
        return SOURCE_BOUNDARY_YEAR, SOURCE_NOTE
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    boundary = int(metadata.get("primary_boundary_year") or SOURCE_BOUNDARY_YEAR)
    sources = {item["source"]: item for item in metadata.get("sources", [])}
    hein = sources.get("hein_daily") or sources.get("hein_bound") or {}
    govinfo = sources.get("govinfo") or {}
    note = (
        f"Sources: Stanford Hein ({hein.get('min_year', 1873)}-{hein.get('max_year', 2017)}) "
        f"+ GovInfo CREC ({govinfo.get('min_year', boundary)}-{govinfo.get('max_year', 'present')}). "
        "House/Senate only; Extensions excluded. Units shown on y-axis. "
        "GovInfo party coverage varies; see coverage/turn_coverage.csv."
    )
    return boundary, note


# (column, title, y-label) for each per-party panel/figure.
_PANELS = [(metric.rate, metric.title, metric.units) for metric in HEADLINE_METRICS]

_SUPPLEMENTAL_PANELS = [
    ("comity_per_1k", "All coded comity / deference phrases", "hits per 1,000 words"),
    ("ideological_label_per_1k", "Ideological labels", "hits per 1,000 words"),
    ("outgroup_ref_per_1k", "References to the other party", "refs per 1,000 words"),
    (
        "outgroup_hostility_contexts_per_100_refs",
        "Out-party references with nearby personal disrespect",
        "contexts per 100 references",
    ),
    (
        "outgroup_misconduct_contexts_per_100_refs",
        "Out-party references with nearby misconduct allegations",
        "contexts per 100 references",
    ),
    (
        "outgroup_comity_contexts_per_100_refs",
        "Out-party references with nearby comity language",
        "contexts per 100 references",
    ),
    (
        "directed_hostility_per_1k",
        "Personal disrespect near out-party references",
        "hits per 1,000 words",
    ),
    (
        "directed_misconduct_per_1k",
        "Misconduct allegations near out-party references",
        "hits per 1,000 words",
    ),
    ("democrat_party_pej_per_1k", '"Democrat party" pejorative', "hits per 1,000 words"),
]

_CHAMBER_PANELS = [(metric.rate, metric.title, metric.units) for metric in CHAMBER_METRICS]


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
        "Congressional comity and conflict language \u2014 U.S. Congressional Record, 1873\u20132026",
        "overview.png", legend_fontsize=9, rect_top=_RECT_TOP_1LINE,
    )


def _asymmetry(gc: pd.DataFrame, figs_dir: Path) -> Path:
    """Fixed-weight House/Senate D-R difference in nearby disrespect."""
    piv = gc.pivot_table(
        index=["year", "chamber"], columns="party",
        values="directed_hostility_per_1k",
    )
    fig, ax = charts.new_figure(figsize=(10, 5.5))
    if {"D", "R"}.issubset(piv.columns):
        chamber_diff = (piv["D"] - piv["R"]).unstack("chamber")
        chamber_diff = chamber_diff.dropna(subset=["house", "senate"])
        diff = chamber_diff[["house", "senate"]].mean(axis=1)
        ax.fill_between(diff.index, 0, diff.clip(lower=0), color=theme.BLUE, alpha=0.5,
                        label="Higher Democratic rate")
        ax.fill_between(diff.index, 0, diff.clip(upper=0), color=theme.ACCENT, alpha=0.5,
                        label="Higher Republican rate")
        charts.line(ax, diff.index, diff.values, color=theme.TEXT, label="D \u2212 R", linewidth=1.6)
    ax.axhline(0, color=theme.MUTED, linewidth=0.8)
    charts.style_axes(
        ax,
        "Asymmetry in disrespect near out-party references",
        "Year",
        "D \u2212 R nearby-disrespect rate (per 1,000 words)",
        subtitle="Equal House/Senate weights; proximity does not prove direction",
    )
    return charts.finish(fig, ax, figs_dir / "directed_asymmetry.png", source=SOURCE_NOTE)


def _overview_for_chamber(gc: pd.DataFrame, figs_dir: Path, chamber: str) -> Path:
    """Six-panel overview of one chamber, Democrats vs Republicans.

    Splitting the chambers into separate figures replaces the previous combined
    version, which crammed four series into every panel and made the House and
    Senate lines fight for the same space.
    """
    label = theme.CHAMBER_LABELS[chamber]
    return _grid_overview(
        gc[gc["chamber"] == chamber], figs_dir, _plot_by_party,
        f"Civility in the U.S. {label} \u2014 Congressional Record, 1873\u20132026\n"
        "Democrats vs Republicans",
        f"overview_{chamber}.png", legend_fontsize=9, rect_top=_RECT_TOP_2LINE,
    )


def render(metrics_path: Path, out_dir: Path) -> List[Path]:
    global SOURCE_BOUNDARY_YEAR, SOURCE_NOTE
    SOURCE_BOUNDARY_YEAR, SOURCE_NOTE = _load_provenance(metrics_path)
    df = pd.read_parquet(metrics_path)
    df = df[df["chamber"].isin(["house", "senate"])].copy()
    g = _by_congress_party(df)
    gc = _by_year_chamber_party(df)
    figs_dir = out_dir / "figures"
    figs_dir.mkdir(parents=True, exist_ok=True)
    temp_figs = Path(tempfile.mkdtemp(prefix=".figures-", dir=figs_dir.parent))
    try:
        written: List[Path] = [
            _overview(g, temp_figs),
            _overview_for_chamber(gc, temp_figs, "house"),
            _overview_for_chamber(gc, temp_figs, "senate"),
        ]

        for col, title, ylab in [*_PANELS, *_SUPPLEMENTAL_PANELS]:
            # overall (by party): clean markerless lines with direct end-of-line party labels
            fig, ax = charts.new_figure(figsize=(10, 5.5))
            _plot_by_party(ax, g, col, label_ends=True, marker=None, linewidth=2.6)
            charts.style_axes(ax, title, "Year", ylab,
                              subtitle="U.S. House & Senate combined, Democrats vs Republicans")
            written.append(charts.finish(
                fig, ax, temp_figs / f"{col}.png", source=SOURCE_NOTE, legend=False
            ))

        # Per-chamber breakdowns for the headline measures: one figure each, so the
        # House and Senate series are never overplotted on the same axes.
        for col, title, ylab in _CHAMBER_PANELS:
            for chamber in ("house", "senate"):
                sub = gc[gc["chamber"] == chamber]
                if sub.empty:
                    continue
                label = theme.CHAMBER_LABELS[chamber]
                fig, ax = charts.new_figure(figsize=(10, 5.5))
                _plot_by_party(ax, sub, col, label_ends=True, marker=None, linewidth=2.6)
                charts.style_axes(ax, f"{title} \u2014 {label}", "Year", ylab,
                                  subtitle=f"U.S. {label}, Democrats vs Republicans")
                written.append(charts.finish(
                    fig, ax, temp_figs / f"{col}_{chamber}.png",
                    source=SOURCE_NOTE, legend=False,
                ))

        written.append(_asymmetry(gc, temp_figs))

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
