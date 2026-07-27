"""Reusable Substack-style chart helpers.

Small, composable helpers that apply the shared :mod:`analysis.plotting.theme`
conventions (o-markers with a background-coloured edge, y-only grid, bold two-line
titles, borderless legend, italic source note, dpi=200) so every figure in the
project looks consistent with minimal boilerplate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import matplotlib.pyplot as plt

from . import theme


def new_figure(figsize: Tuple[float, float] = (11, 6)):
    """Create a themed ``(fig, ax)``. Call after (or it will call) ``theme.apply``."""
    theme.apply()
    return plt.subplots(figsize=figsize)


def style_axes(ax, title: str, xlabel: str, ylabel: str, subtitle: str | None = None) -> None:
    """Apply the standard title/label/grid styling to an axis.

    Renders a two-tier header: a bold title with a muted sub-title beneath it (the
    ``uk_decline`` house convention), rather than a single newline-joined string.
    """
    ax.set_title(title, fontweight="bold", pad=28 if subtitle else 14)
    if subtitle:
        ax.text(0.5, 1.015, subtitle, transform=ax.transAxes, ha="center", va="bottom",
                fontsize=11, color=theme.MUTED)
    ax.set_xlabel(xlabel, labelpad=2)
    ax.set_ylabel(ylabel, labelpad=2)
    ax.grid(axis="y", linestyle="-", linewidth=0.5)
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", pad=2)


def line(ax, xs, ys, color: str, label: str | None = None, linewidth: float = 2.2,
         markersize: float = 4, linestyle: str = "-", marker: str | None = "o") -> None:
    """Draw one Substack-style series. ``marker=None`` gives a clean, markerless line."""
    ax.plot(xs, ys, color=color, linewidth=linewidth, marker=marker, markersize=markersize,
            markeredgecolor=theme.BG, markeredgewidth=0.8, label=label, linestyle=linestyle)


def end_label(ax, x, y, text: str, color: str, **kwargs) -> None:
    """Label a series at its end point (delegates to :func:`theme.end_label`)."""
    theme.end_label(ax, x, y, text, color, **kwargs)


def marker_line(ax, x: float, color: str | None = None, style: str = ":") -> None:
    """Vertical reference marker (e.g. a data-source boundary year).

    Not used by the publication figures, which no longer draw the Hein -> GovInfo
    boundary; retained for the calibration diagnostic, whose subject *is* that
    transition.
    """
    ax.axvline(x, color=color or theme.MUTED, linestyle=style, linewidth=0.9, alpha=0.7)


def finish(fig, ax, out_path: Path | str, source: str | None = None,
           legend: bool = True, dpi: int = 200) -> Path:
    """Add legend + source note, tight-layout, and save. Returns the output path."""
    if legend and ax.get_legend_handles_labels()[0]:
        ax.legend(loc="best", frameon=False, labelcolor=theme.TEXT)
    note_lines = theme.source_note(fig, source) if source else 0
    # Reserve bottom margin for the italic source note, growing with its line count
    # so a wrapped two-line note is not overlapped by the x-axis label.
    bottom = 0.0 if not note_lines else min(0.18, 0.03 + 0.035 * (note_lines - 1))
    fig.tight_layout(rect=(0, bottom, 1, 1))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return out_path
