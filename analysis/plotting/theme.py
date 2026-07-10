"""Substack-style plotting theme, matched to the ``uk_decline`` project figures.

Palette and rcParams mirror ``uk_decline``'s ``tuition/theme.py`` so figures across
the personal-projects portfolio share one look: cream background, serif font, muted
grid, borderless legend, italic source notes, bold titles. Call :func:`apply` once
before plotting.

Usage::

    from analysis.plotting import theme
    theme.apply()
    fig, ax = plt.subplots(figsize=(11, 6))
    ...
    theme.source_note(fig, "Source: ...")
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")  # headless: no display required
import matplotlib.patheffects as pe  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

# ── Shared Substack palette (identical to uk_decline/tuition/theme.py) ──────────
BG = "#F7F5F0"
CARD = "#EFEDE8"
TEXT = "#1A1A1A"
MUTED = "#6B6B6B"
ACCENT = "#C85A3D"   # terracotta
BLUE = "#3D6F8C"     # muted blue
GOLD = "#C2993E"
GREEN = "#4A7C59"
GRID = "#D6D3CC"

# Party colours drawn from the shared palette (muted, print-friendly).
PARTY_COLORS = {
    "D": BLUE,
    "R": ACCENT,
    "I": GREEN,
    "other": MUTED,
}
PARTY_LABELS = {"D": "Democrats", "R": "Republicans", "I": "Independents", "other": "Other"}

RC_PARAMS = {
    "figure.facecolor": BG,
    "axes.facecolor": BG,
    "savefig.facecolor": BG,
    "text.color": TEXT,
    "axes.labelcolor": TEXT,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.edgecolor": GRID,
    "axes.linewidth": 0.8,
    "grid.color": GRID,
    "grid.alpha": 0.6,
    "grid.linewidth": 0.5,
    "font.family": "serif",
    "font.size": 12,
    "axes.titlesize": 16,
    "axes.titleweight": "bold",
    "axes.labelsize": 13,
    "figure.titlesize": 18,
    "legend.framealpha": 0.0,
    "legend.fontsize": 11,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "xtick.major.size": 0,      # tickless axes for a cleaner, editorial look
    "ytick.major.size": 0,
    "text.parse_math": False,   # treat '$' literally (titles/labels are plain prose)
}

# A white "halo" so labels drawn over lines/fills stay legible.
_WHITE_STROKE = [pe.withStroke(linewidth=3.0, foreground="white")]


def white_stroke() -> list:
    """Path-effects list giving text a white outline (for labels drawn over data)."""
    return list(_WHITE_STROKE)


def apply() -> None:
    """Apply the shared Substack theme to matplotlib's global rcParams."""
    plt.rcParams.update(RC_PARAMS)


def source_note(fig, text: str, x: float = 0.01, y: float = 0.01, ha: str = "left") -> None:
    """Add the standard italic, muted source note used across the portfolio figures."""
    fig.text(x, y, text, ha=ha, fontsize=8, color=MUTED, style="italic")


def end_label(ax, x, y, text: str, color: str, *, fontsize: float = 10.5,
              pad: str = "  ") -> None:
    """Label a series at its end point, with a white halo for legibility.

    The signature ``uk_decline`` touch: direct end-of-line labels replace a legend box on
    single-series-per-party charts, so the eye maps colour to party without a lookup.
    """
    ax.text(x, y, f"{pad}{text}", fontsize=fontsize, fontweight="bold", color=color,
            va="center", ha="left", path_effects=white_stroke())
