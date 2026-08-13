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
    "100,000 spoken words. Profanity quotations are excluded."
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


def language_trends(
    series: pd.DataFrame,
    out_path: Path | str,
    *,
    scope_label: str,
    granularity: str,
) -> Path:
    """Render three Democratic/Republican trend panels for one Congress scope."""
    theme.apply()
    fig, axes = plt.subplots(3, 1, figsize=(11, 12.5), sharex=True)
    fig.suptitle(
        f"Language indicators over time — {scope_label}",
        fontsize=19,
        fontweight="bold",
        y=0.985,
    )
    if series.empty or "period" not in series:
        for ax, metric in zip(axes, LANGUAGE_METRICS.values()):
            charts.style_axes(
                ax,
                metric["label"],
                "",
                "Hits per 100,000 words",
                subtitle="No attributed House or Senate floor remarks in this scope.",
            )
            ax.text(
                0.5, 0.5, "No floor-language data", transform=ax.transAxes,
                ha="center", va="center", color=theme.MUTED,
            )
        axes[-1].set_xlabel("Period")
        return _save(fig, out_path, top=0.955        )
        parsed_periods = pd.to_datetime(
            series["period"] + ("-01" if granularity == "month" else "-01-01")
        )
        plotted = series.assign(_date=parsed_periods)
        for ax, metric in zip(axes, LANGUAGE_METRICS.values()):
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
    return _save(fig, out_path, top=0.955)


def language_members(
    rankings: dict[str, pd.DataFrame],
    out_path: Path | str,
    *,
    scope_label: str,
    min_words: int,
) -> Path:
    """Render three horizontal member-rate panels for one Congress scope."""
    theme.apply()
    fig, axes = plt.subplots(3, 1, figsize=(11, 13.5))
    fig.suptitle(
        f"Highest language-indicator rates — {scope_label}",
        fontsize=19,
        fontweight="bold",
        y=0.988,
    )
    for ax, (key, metric) in zip(axes, LANGUAGE_METRICS.items()):
        frame = rankings[key].iloc[::-1]
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
                ha="center", va="center", color=theme.MUTED,
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
        ax.bar_label(bars, fmt="%.1f", padding=4, fontsize=9, color=theme.TEXT)
        charts.style_axes(
            ax,
            metric["label"],
            "Hits per 100,000 words",
            "",
            subtitle=(
                f"{metric['definition']} Members below {min_words:,} words are omitted."
            ),
        )
        ax.grid(axis="x", linestyle="-", linewidth=0.5)
        ax.grid(axis="y", visible=False)
        ax.set_xlim(left=0)
    legend = [
        Patch(facecolor=theme.PARTY_COLORS["D"], label="Democrats"),
        Patch(facecolor=theme.PARTY_COLORS["R"], label="Republicans"),
        Patch(facecolor=theme.PARTY_COLORS["I"], label="Independents"),
    ]
    fig.legend(handles=legend, loc="upper center", bbox_to_anchor=(0.5, 0.963),
               frameon=False, ncol=3)
    return _save(fig, out_path, top=0.94)
