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
    "grid.color": GRID,
    "grid.alpha": 0.6,
    "grid.linewidth": 0.5,
    "font.family": "serif",
    "font.size": 12,
    "axes.titlesize": 16,
    "axes.labelsize": 13,
    "figure.titlesize": 18,
    "legend.framealpha": 0.0,
    "legend.fontsize": 11,
    "axes.spines.top": False,
    "axes.spines.right": False,
}


def apply() -> None:
    """Apply the shared Substack theme to matplotlib's global rcParams."""
    plt.rcParams.update(RC_PARAMS)


def source_note(fig, text: str, x: float = 0.01, y: float = 0.01, ha: str = "left") -> None:
    """Add the standard italic, muted source note used across the portfolio figures."""
    fig.text(x, y, text, ha=ha, fontsize=8, color=MUTED, style="italic")
