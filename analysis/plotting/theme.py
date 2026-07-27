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

import textwrap

import matplotlib.patheffects as pe
import matplotlib.pyplot as plt

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


def _hex_to_rgb(color: str) -> tuple[float, float, float]:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _rgb_to_hex(rgb) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02X}" for c in rgb)


def shade(color: str, amount: float = 0.4) -> str:
    """Darken ``color`` by mixing it ``amount`` of the way toward near-black.

    Used to separate chambers within a party: the darker variant keeps the party
    hue but gains contrast against the cream background, so two same-party lines
    are distinguishable even in a small multi-panel grid.
    """
    return _rgb_to_hex(c * (1 - amount) + 0.10 * amount for c in _hex_to_rgb(color))


def tint(color: str, amount: float = 0.4) -> str:
    """Lighten ``color`` by mixing it ``amount`` of the way toward white."""
    return _rgb_to_hex(c + (1.0 - c) * amount for c in _hex_to_rgb(color))


# Chamber styling. Chamber is encoded on three channels at once -- colour depth,
# dash pattern and marker shape -- because a dash pattern alone is not readable at
# small panel sizes, where same-party House and Senate lines were indistinguishable.
CHAMBER_STYLE = {
    "house": {
        "linestyle": "-",
        "marker": "o",
        "linewidth": 2.4,
        "markersize": 4.0,
        "depth": 0.0,          # party colour as-is
    },
    "senate": {
        "linestyle": (0, (5, 2)),   # long, widely spaced dashes
        "marker": "^",
        "linewidth": 1.7,
        "markersize": 4.4,
        "depth": 0.45,         # noticeably darker than the House line
    },
}
CHAMBER_LABELS = {"house": "House", "senate": "Senate"}


def chamber_color(party: str, chamber: str) -> str:
    """Party colour adjusted for chamber (House base, Senate darker)."""
    base = PARTY_COLORS.get(party, MUTED)
    depth = CHAMBER_STYLE.get(chamber, {}).get("depth", 0.0)
    return shade(base, depth) if depth else base


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


def source_note(fig, text: str, x: float = 0.01, y: float = 0.01, ha: str = "left",
                width: int = 118) -> int:
    """Add the standard italic, muted source note, wrapped to ``width`` characters.

    Figures are saved with ``bbox_inches="tight"``, so a single long note sets the
    saved width and leaves a band of empty space to the right of the axes. Wrapping
    keeps the note inside the plot's own width instead. Returns the line count so
    callers can reserve the right amount of bottom margin.
    """
    lines = textwrap.wrap(text, width=width) or [""]
    fig.text(x, y, "\n".join(lines), ha=ha, va="bottom", fontsize=8, color=MUTED,
             style="italic", linespacing=1.4)
    return len(lines)


def end_label(ax, x, y, text: str, color: str, *, fontsize: float = 10.5,
              pad: str = "  ") -> None:
    """Label a series at its end point, with a white halo for legibility.

    The signature ``uk_decline`` touch: direct end-of-line labels replace a legend box on
    single-series-per-party charts, so the eye maps colour to party without a lookup.
    """
    ax.text(x, y, f"{pad}{text}", fontsize=fontsize, fontweight="bold", color=color,
            va="center", ha="left", path_effects=white_stroke())
