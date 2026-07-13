"""Compare Hein and GovInfo metrics in their 1994-2016 overlap."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import List

import pandas as pd

from analysis.plotting import charts, theme
from analysis.score.registry import HEADLINE_METRICS, METRICS

LOG = logging.getLogger("analysis.calibrate")

CALIBRATION_METRICS = tuple(
    metric for metric in METRICS
    if metric in HEADLINE_METRICS or metric.denominator == "outgroup_refs"
)


def _source_family(source: str) -> str:
    return "hein" if source.startswith("hein_") else source


def _aggregate_sources(df: pd.DataFrame) -> pd.DataFrame:
    df = df[
        df["chamber"].isin(["house", "senate"])
        & df["party"].isin(["D", "R"])
    ].copy()
    df["source_family"] = df["source"].map(_source_family)
    raw_cols = sorted({
        metric.raw_count for metric in CALIBRATION_METRICS
    } | {"words", "outgroup_refs"})
    grouped = df.groupby(
        ["source_family", "congress", "year", "chamber", "party"], as_index=False
    )[raw_cols].sum()
    words = grouped["words"].where(grouped["words"] != 0)
    refs = grouped["outgroup_refs"].where(grouped["outgroup_refs"] != 0)
    for metric in CALIBRATION_METRICS:
        denominator = words if metric.denominator == "words" else refs
        grouped[metric.rate] = (
            metric.scale * grouped[metric.raw_count] / denominator
        ).fillna(0.0)
    return grouped


def paired_overlap(source_metrics: pd.DataFrame) -> pd.DataFrame:
    """Return one row per source-paired cell and metric."""
    grouped = _aggregate_sources(source_metrics)
    keys = ["congress", "year", "chamber", "party"]
    rows: List[dict] = []
    for metric in CALIBRATION_METRICS:
        pivot = grouped.pivot_table(
            index=keys, columns="source_family", values=metric.rate, aggfunc="first"
        )
        if not {"hein", "govinfo"}.issubset(pivot.columns):
            continue
        pivot = pivot.dropna(subset=["hein", "govinfo"]).reset_index()
        for row in pivot.itertuples(index=False):
            hein = float(row.hein)
            govinfo = float(row.govinfo)
            rows.append({
                **{key: getattr(row, key) for key in keys},
                "metric": metric.rate,
                "hein": hein,
                "govinfo": govinfo,
                "difference_govinfo_minus_hein": govinfo - hein,
                "ratio_hein_to_govinfo": hein / govinfo if govinfo > 0 else None,
            })
    return pd.DataFrame(rows)


def calibration_summary(pairs: pd.DataFrame) -> pd.DataFrame:
    """Summarize paired agreement and recommend only stable multiplicative adjustments."""
    rows = []
    for metric, group in pairs.groupby("metric"):
        positive = group[(group["hein"] > 0) & (group["govinfo"] > 0)].copy()
        log_ratios = (positive["hein"] / positive["govinfo"]).map(math.log)
        pearson = (
            group["hein"].corr(group["govinfo"], method="pearson")
            if group["hein"].nunique() > 1 and group["govinfo"].nunique() > 1
            else None
        )
        spearman = (
            group["hein"].rank().corr(group["govinfo"].rank(), method="pearson")
            if group["hein"].nunique() > 1 and group["govinfo"].nunique() > 1
            else None
        )
        log_iqr = (
            float(log_ratios.quantile(0.75) - log_ratios.quantile(0.25))
            if len(log_ratios) else None
        )
        multiplier = float(math.exp(log_ratios.median())) if len(log_ratios) else None
        stable = bool(
            len(group) >= 20
            and pd.notna(spearman)
            and spearman >= 0.70
            and log_iqr is not None
            and log_iqr <= math.log(1.5)
        )
        rows.append({
            "metric": metric,
            "paired_cells": int(len(group)),
            "positive_paired_cells": int(len(positive)),
            "pearson": pearson,
            "spearman": spearman,
            "mean_difference_govinfo_minus_hein": group[
                "difference_govinfo_minus_hein"
            ].mean(),
            "median_hein_to_govinfo_multiplier": multiplier,
            "log_ratio_iqr": log_iqr,
            "stable_adjustment": stable,
            "recommended_govinfo_to_hein_multiplier": multiplier if stable else None,
        })
    return pd.DataFrame(rows).sort_values("metric").reset_index(drop=True)


def _plot_pairs(pairs: pd.DataFrame, out_dir: Path) -> List[Path]:
    written = []
    for metric in CALIBRATION_METRICS:
        data = pairs[pairs["metric"].eq(metric.rate)]
        if data.empty:
            continue
        fig, ax = charts.new_figure(figsize=(6.5, 6))
        for party, color in (("D", theme.BLUE), ("R", theme.ACCENT)):
            sub = data[data["party"].eq(party)]
            ax.scatter(sub["hein"], sub["govinfo"], color=color, alpha=0.65, s=28,
                       label=theme.PARTY_LABELS[party])
        limit = max(float(data["hein"].max()), float(data["govinfo"].max()), 1e-9)
        ax.plot([0, limit], [0, limit], color=theme.MUTED, linestyle="--", linewidth=1)
        charts.style_axes(
            ax, f"Source overlap: {metric.title}", "Hein", "GovInfo",
            subtitle="Paired Congress x chamber x party cells, 1994-2016",
        )
        written.append(charts.finish(
            fig, ax, out_dir / f"{metric.rate}_paired.png",
            source="Real overlapping Stanford Hein and GovInfo Congressional Record text.",
        ))
    return written


def _calibrated_primary(
    primary_metrics: pd.DataFrame, summary: pd.DataFrame, boundary_year: int
) -> pd.DataFrame:
    """Create party-level raw and source-calibrated headline rates."""
    raw_cols = sorted({metric.raw_count for metric in HEADLINE_METRICS} | {"words"})
    grouped = primary_metrics[
        primary_metrics["chamber"].isin(["house", "senate"])
        & primary_metrics["party"].isin(["D", "R"])
    ].groupby(["congress", "year", "party"], as_index=False)[raw_cols].sum()
    words = grouped["words"].where(grouped["words"] != 0)
    multipliers = summary.set_index("metric")[
        "recommended_govinfo_to_hein_multiplier"
    ].to_dict()
    for metric in HEADLINE_METRICS:
        grouped[metric.rate] = (
            metric.scale * grouped[metric.raw_count] / words
        ).fillna(0.0)
        multiplier = multipliers.get(metric.rate)
        calibrated = grouped[metric.rate].copy()
        if pd.notna(multiplier):
            calibrated.loc[grouped["year"] >= boundary_year] *= float(multiplier)
        else:
            calibrated.loc[grouped["year"] >= boundary_year] = pd.NA
        grouped[f"{metric.rate}_source_calibrated"] = calibrated
    return grouped


def _plot_calibrated_overview(
    calibrated: pd.DataFrame, boundary_year: int, out_path: Path
) -> Path:
    import matplotlib.pyplot as plt

    theme.apply()
    fig, axes = plt.subplots(2, 3, figsize=(16, 9))
    for ax, metric in zip(axes.flat, HEADLINE_METRICS):
        column = f"{metric.rate}_source_calibrated"
        for party in ("D", "R"):
            sub = calibrated[calibrated["party"].eq(party)].sort_values("year")
            ax.scatter(
                sub["year"], sub[column], color=theme.PARTY_COLORS[party],
                alpha=0.28, s=18, linewidths=0,
            )
            smoothed = sub[column].rolling(5, center=True, min_periods=3).mean()
            charts.line(
                ax, sub["year"], smoothed, color=theme.PARTY_COLORS[party],
                label=theme.PARTY_LABELS[party], marker=None, linewidth=2.2,
            )
        charts.marker_line(ax, boundary_year)
        charts.style_axes(ax, metric.title, "Congress (convening year)", metric.units)
    axes.flat[0].legend(frameon=False, labelcolor=theme.TEXT)
    fig.suptitle("Source-calibrated congressional discourse components", fontweight="bold")
    theme.source_note(
        fig,
        "Dots are Congress values; lines are centered 5-Congress means. GovInfo is mapped to "
        "the Hein scale only for metrics passing the documented overlap-stability rule.",
    )
    fig.tight_layout(rect=(0, 0.04, 1, 0.97))
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return out_path


def run_calibration(metrics_path: Path, out_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    source_metrics = pd.read_parquet(metrics_path)
    pairs = paired_overlap(source_metrics)
    if pairs.empty:
        raise ValueError("no Hein/GovInfo overlap cells found; ingest 1994-2016 GovInfo first")
    summary = calibration_summary(pairs)
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs.to_parquet(out_dir / "paired_overlap.parquet", index=False)
    pairs.to_csv(out_dir / "paired_overlap.csv", index=False)
    summary.to_csv(out_dir / "calibration_summary.csv", index=False)
    (out_dir / "calibration_summary.json").write_text(
        json.dumps(summary.to_dict(orient="records"), indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    figures = out_dir / "figures"
    figures.mkdir(exist_ok=True)
    _plot_pairs(pairs, figures)
    primary_path = metrics_path.with_name("civility_metrics.parquet")
    metadata_path = metrics_path.parent.parent / "coverage" / "source_metadata.json"
    if primary_path.exists() and metadata_path.exists():
        boundary_year = int(
            json.loads(metadata_path.read_text(encoding="utf-8"))[
                "primary_boundary_year"
            ]
        )
        calibrated = _calibrated_primary(
            pd.read_parquet(primary_path), summary, boundary_year
        )
        calibrated.to_parquet(out_dir / "calibrated_primary_metrics.parquet", index=False)
        calibrated.to_csv(out_dir / "calibrated_primary_metrics.csv", index=False)
        _plot_calibrated_overview(
            calibrated, boundary_year, figures / "calibrated_headline_overview.png"
        )
    LOG.info("wrote %d paired cells and %d calibration rows", len(pairs), len(summary))
    return pairs, summary
