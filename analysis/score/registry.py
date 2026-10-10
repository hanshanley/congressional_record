"""Central registry for discourse metrics, labels, and plot eligibility.

The project measures three things in congressional floor speech: formulaic courtesy,
profanity, and ethnic slurs used in the United States. Every rate is hits per 1,000 words.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class MetricSpec:
    rate: str
    raw_count: str
    score_key: str
    family: str
    title: str
    polarity: Literal["positive", "negative"]
    headline: bool = False
    chamber_plot: bool = False
    scale: float = 1000
    units: str = "hits per 1,000 words"
    codebook_version: str = "2026-10-v5"

    def __post_init__(self) -> None:
        identifiers = (self.rate, self.raw_count, self.score_key, self.family)
        if any(not value or not value.replace("_", "").isalnum() for value in identifiers):
            raise ValueError(f"invalid metric identifier in {self!r}")
        if not self.rate.endswith("_per_1k") or self.scale != 1000:
            raise ValueError(f"{self.rate} must be a per-1,000-word rate")
        if self.polarity not in {"positive", "negative"}:
            raise ValueError(f"invalid polarity for {self.rate}: {self.polarity}")


METRICS = (
    MetricSpec(
        "formal_courtesy_per_1k", "formal_courtesy_hits", "formal_courtesy_hits",
        "courtesy", "Formulaic courtesy / deference", "positive",
        headline=True, chamber_plot=True,
    ),
    MetricSpec(
        "profanity_per_1k", "profanity_hits", "profanity_hits",
        "profanity", "Profanity", "negative", headline=True, chamber_plot=True,
    ),
    MetricSpec(
        "ethnic_slurs_per_1k", "ethnic_slur_hits", "ethnic_slur_hits",
        "ethnic_slur", "Ethnic slurs", "negative", headline=True, chamber_plot=True,
    ),
    MetricSpec(
        "profanity_mild_per_1k", "profanity_mild_hits", "profanity_mild",
        "profanity", "Mild profanity", "negative",
    ),
    MetricSpec(
        "profanity_strong_per_1k", "profanity_strong_hits", "profanity_strong",
        "profanity", "Strong profanity", "negative",
    ),
)

BY_RATE = {metric.rate: metric for metric in METRICS}
RATE_TO_RAW = {metric.rate: metric.raw_count for metric in METRICS}
SCORE_KEYS = tuple(dict.fromkeys(metric.score_key for metric in METRICS))
RAW_COUNTS = tuple(dict.fromkeys(metric.raw_count for metric in METRICS))
HEADLINE_METRICS = tuple(metric for metric in METRICS if metric.headline)
CHAMBER_METRICS = tuple(metric for metric in METRICS if metric.chamber_plot)
