"""Publication charts for the static congressional activity website."""

from __future__ import annotations

from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
import pandas as pd

from analysis.speakers import LANGUAGE_METRICS

from . import charts, theme


SOURCE = (
    "Source: Congressional Record via GovInfo CREC / Stanford Hein. "
    "House and Senate attributed, non-procedural floor remarks only; rates per "
    "100,000 spoken words. Quoted profanity and slurs are excluded."
)


def _save(fig, out_path: Path | str, *, top: float = 0.94, bottom: float = 0.07) -> Path:
    note_lines = theme.source_note(fig, SOURCE)
    bottom = max(bottom, min(0.12, 0.035 + 0.025 * (note_lines - 1)))
    fig.tight_layout(rect=(0, bottom, 1, top))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=170, bbox_inches="tight")
    plt.close(fig)
    return out_path


def _header_fraction(height: float, inches: float) -> float:
    """Figure fraction below a header of fixed physical height, whatever the panel count."""
    return 1 - inches / height


def language_trends(
    series: pd.DataFrame,
    out_path: Path | str,
    *,
    scope_label: str,
    granularity: str,
    metrics: dict | None = None,
) -> Path:
    """Render one Democratic/Republican trend panel per language measure."""
    metrics = LANGUAGE_METRICS if metrics is None else metrics
    theme.apply()
    height = 4.2 * len(metrics) + 0.4
    top = _header_fraction(height, 0.12)
    fig, axes = plt.subplots(
        len(metrics), 1, figsize=(11, height), sharex=True, squeeze=False,
    )
    axes = axes[:, 0]
    fig.suptitle(
        f"Language indicators over time — {scope_label}",
        fontsize=19,
        fontweight=plt.rcParams["axes.titleweight"],
        y=_header_fraction(height, 0.08),
    )
    if series.empty or "period" not in series:
        for ax, metric in zip(axes, metrics.values()):
            charts.style_axes(
                ax,
                metric["label"],
                "",
                "Hits per 100,000 words",
                subtitle="No attributed House or Senate floor remarks in this scope.",
            )
            ax.text(
                0.5, 0.5, "No floor-language data", transform=ax.transAxes,
                ha="center", va="center", color=theme.active_color("MUTED"),
            )
        axes[-1].set_xlabel("Period")
        return _save(fig, out_path, top=top)
    parsed_periods = pd.to_datetime(
        series["period"] + ("-01" if granularity == "month" else "-01-01")
    )
    plotted = series.assign(_date=parsed_periods)
    for ax, metric in zip(axes, metrics.values()):
        for party, linestyle, marker in (("D", "-", "o"), ("R", "--", "s")):
            sub = plotted[plotted["party"] == party].sort_values("_date")
            if sub.empty:
                continue
            charts.line(
                ax,
                sub["_date"],
                sub[metric["rate"]],
                color=theme.PARTY_COLORS[party],
                label=theme.PARTY_LABELS[party],
                linestyle=linestyle,
                marker=marker,
                linewidth=2.3,
                markersize=4,
            )
        charts.fit_time_axis(ax)
        charts.style_axes(
            ax,
            metric["label"],
            "",
            "Hits per 100,000 words",
            subtitle=metric["definition"],
        )
        ax.set_ylim(bottom=0)
        ax.legend(loc="upper left", frameon=False, ncol=2)
    if granularity == "month":
        axes[-1].xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))
        axes[-1].set_xlabel("Month")
    else:
        axes[-1].xaxis.set_major_locator(mdates.YearLocator(4))
        axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
        axes[-1].set_xlabel("Year")
    return _save(fig, out_path, top=top)


def language_members(
    rankings: dict[str, pd.DataFrame],
    out_path: Path | str,
    *,
    scope_label: str,
    min_words: int,
    metrics: dict | None = None,
) -> Path:
    """Render one horizontal member-rate panel per language measure."""
    metrics = LANGUAGE_METRICS if metrics is None else metrics
    theme.apply()
    height = 4.5 * len(metrics) + 0.7
    fig, axes = plt.subplots(len(metrics), 1, figsize=(11, height), squeeze=False)
    axes = axes[:, 0]
    fig.suptitle(
        f"Highest language-indicator rates — {scope_label}",
        fontsize=19,
        fontweight=plt.rcParams["axes.titleweight"],
        y=_header_fraction(height, 0.08),
    )
    for ax, (key, metric) in zip(axes, metrics.items()):
        frame = rankings.get(key, pd.DataFrame()).iloc[::-1]
        if frame.empty:
            charts.style_axes(
                ax,
                metric["label"],
                "Hits per 100,000 words",
                "",
                subtitle=f"No member cleared the {min_words:,}-word threshold.",
            )
            ax.text(
                0.5, 0.5, "No eligible members", transform=ax.transAxes,
                ha="center", va="center", color=theme.active_color("MUTED"),
            )
            continue
        labels = [
            f"{name} ({party or 'Other'}, {str(chamber).title()})"
            for name, party, chamber in zip(
                frame["speaker_name"], frame["party"], frame["chamber"]
            )
        ]
        colors = [theme.PARTY_COLORS.get(party, theme.MUTED) for party in frame["party"]]
        bars = ax.barh(labels, frame[metric["rate"]], color=colors, height=0.72)
        ax.bar_label(bars, fmt="%.1f", padding=4, fontsize=9, color=theme.active_color("TEXT"))
        charts.style_axes(
            ax,
            metric["label"],
            "Hits per 100,000 words",
            "",
            subtitle=metric["definition"],
        )
        ax.grid(axis="x", linestyle="-", linewidth=0.5)
        ax.grid(axis="y", visible=False)
        ax.set_xlim(left=0)
    present_parties = {
        str(party)
        for frame in rankings.values()
        if not frame.empty
        for party in frame["party"].dropna().unique()
    }
    legend = [
        Patch(facecolor=theme.PARTY_COLORS[party], label=theme.PARTY_LABELS[party])
        for party in ("D", "R", "I")
        if party in present_parties
    ]
    if legend:
        fig.legend(handles=legend, loc="upper center",
                   bbox_to_anchor=(0.5, _header_fraction(height, 0.45)),
                   frameon=False, ncol=len(legend))
    return _save(fig, out_path, top=_header_fraction(height, 0.5))
