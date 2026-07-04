"""Render civility time-series charts from the aggregated metrics table.

Re-aggregates the ``(congress, chamber, party)`` metrics up to ``(congress, party)``
by summing raw hit counts and words (so rates stay word-weighted), then draws the
key trends. Figures go to ``<out_dir>/reports/figures``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

LOG = logging.getLogger("analysis.viz")

_PARTY_COLOR = {"D": "#1f77b4", "R": "#d62728", "I": "#2ca02c", "other": "#7f7f7f"}
_HIT_COLS = [
    "comity_hits", "hostility_hits", "profanity_hits", "profanity_slurs_hits",
    "outgroup_refs", "democrat_party_pej", "directed_comity_hits",
    "directed_hostility_hits", "words",
]


def _by_congress_party(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby(["congress", "year", "party"], as_index=False)[_HIT_COLS].sum()
    w = g["words"].replace(0, 1)
    g["comity_per_1k"] = 1000 * g["comity_hits"] / w
    g["hostility_per_1k"] = 1000 * g["hostility_hits"] / w
    g["profanity_per_1k"] = 1000 * g["profanity_hits"] / w
    g["slurs_per_1k"] = 1000 * g["profanity_slurs_hits"] / w
    g["outgroup_ref_per_1k"] = 1000 * g["outgroup_refs"] / w
    g["democrat_party_pej_per_1k"] = 1000 * g["democrat_party_pej"] / w
    g["directed_comity_per_1k"] = 1000 * g["directed_comity_hits"] / w
    g["directed_hostility_per_1k"] = 1000 * g["directed_hostility_hits"] / w
    return g


def _line_by_party(ax, g: pd.DataFrame, col: str, title: str, ylabel: str) -> None:
    for party in ["D", "R"]:
        sub = g[g.party == party].sort_values("year")
        if sub.empty:
            continue
        ax.plot(sub["year"], sub[col], marker="o", ms=3, lw=1.4,
                color=_PARTY_COLOR[party], label={"D": "Democrats", "R": "Republicans"}[party])
    ax.set_title(title, fontsize=11)
    ax.set_xlabel("Year")
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.3)
    ax.axvline(2017, color="k", ls=":", lw=0.8, alpha=0.6)  # hein->GovInfo source boundary


def render(metrics_path: Path, out_dir: Path) -> List[Path]:
    df = pd.read_parquet(metrics_path)
    g = _by_congress_party(df)
    figs_dir = out_dir / "reports" / "figures"
    figs_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    panels = [
        ("comity_per_1k", "Comity / deference phrases", "hits per 1,000 words"),
        ("hostility_per_1k", "Hostility / attack language", "hits per 1,000 words"),
        ("directed_hostility_per_1k", "Hostility directed at the other party", "hits per 1,000 words"),
        ("outgroup_ref_per_1k", "References to the other party", "refs per 1,000 words"),
        ("profanity_per_1k", "Profanity", "hits per 1,000 words"),
        ("democrat_party_pej_per_1k", '"Democrat party" pejorative', "hits per 1,000 words"),
    ]

    # Combined small-multiples overview.
    fig, axes = plt.subplots(2, 3, figsize=(16, 9))
    for ax, (col, title, ylab) in zip(axes.flat, panels):
        _line_by_party(ax, g, col, title, ylab)
    axes.flat[0].legend(loc="best", fontsize=9)
    fig.suptitle("Decline of comity between the parties — U.S. Congressional Record", fontsize=14)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    overview = figs_dir / "overview.png"
    fig.savefig(overview, dpi=120)
    plt.close(fig)
    written.append(overview)

    # Individual full-size charts.
    for col, title, ylab in panels:
        fig, ax = plt.subplots(figsize=(10, 5.5))
        _line_by_party(ax, g, col, title, ylab)
        ax.legend(loc="best")
        fig.tight_layout()
        p = figs_dir / f"{col}.png"
        fig.savefig(p, dpi=120)
        plt.close(fig)
        written.append(p)

    # D->R vs R->D directed-hostility asymmetry on one axis.
    fig, ax = plt.subplots(figsize=(10, 5.5))
    _line_by_party(ax, g, "directed_hostility_per_1k",
                   "Directed hostility asymmetry (D toward R vs R toward D)", "hits per 1,000 words")
    ax.legend(loc="best")
    fig.tight_layout()
    p = figs_dir / "directed_asymmetry.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    written.append(p)

    # Also write the re-aggregated per-(congress,party) table.
    tbl_dir = out_dir / "reports" / "tables"
    tbl_dir.mkdir(parents=True, exist_ok=True)
    g.to_csv(tbl_dir / "metrics_by_congress_party.csv", index=False)

    LOG.info("wrote %d figures -> %s", len(written), figs_dir)
    return written
