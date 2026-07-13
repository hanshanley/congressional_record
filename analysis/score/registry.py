"""Central registry for discourse metrics, denominators, labels, and plot eligibility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Denominator = Literal["words", "outgroup_refs"]


@dataclass(frozen=True)
class MetricSpec:
    rate: str
    raw_count: str
    score_key: str
    denominator: Denominator
    scale: float
    family: str
    title: str
    units: str
    polarity: Literal["positive", "negative", "neutral"]
    headline: bool = False
    chamber_plot: bool = False
    target_required: bool = False
    codebook_version: str = "2026-07-v2"


METRICS = (
    MetricSpec(
        "formal_courtesy_per_1k", "formal_courtesy_hits", "formal_courtesy_hits",
        "words", 1000, "courtesy", "Formulaic courtesy / deference",
        "hits per 1,000 words", "positive", headline=True, chamber_plot=True,
    ),
    MetricSpec(
        "gratitude_praise_per_1k", "gratitude_praise_hits", "gratitude_praise_hits",
        "words", 1000, "courtesy", "Gratitude / praise",
        "hits per 1,000 words", "positive", headline=True,
    ),
    MetricSpec(
        "cooperation_per_1k", "cooperation_hits", "cooperation_hits",
        "words", 1000, "cooperation", "Bipartisan cooperation language",
        "hits per 1,000 words", "positive", headline=True, chamber_plot=True,
    ),
    MetricSpec(
        "hostility_per_1k", "hostility_hits", "hostility_hits",
        "words", 1000, "personal_attack", "Personal disrespect / attack language",
        "hits per 1,000 words", "negative", headline=True, chamber_plot=True,
    ),
    MetricSpec(
        "misconduct_per_1k", "misconduct_hits", "misconduct_hits",
        "words", 1000, "misconduct", "Misconduct allegation language",
        "hits per 1,000 words", "negative", headline=True, chamber_plot=True,
    ),
    MetricSpec(
        "profanity_per_1k", "profanity_hits", "profanity_hits",
        "words", 1000, "profanity", "High-precision profanity",
        "hits per 1,000 words", "negative", headline=True, chamber_plot=True,
    ),
    MetricSpec(
        "comity_per_1k", "comity_hits", "comity_hits",
        "words", 1000, "combined_comity", "All coded comity / deference phrases",
        "hits per 1,000 words", "positive",
    ),
    MetricSpec(
        "ideological_label_per_1k", "ideological_label_hits", "ideological_label_hits",
        "words", 1000, "ideology", "Ideological labels",
        "hits per 1,000 words", "neutral",
    ),
    MetricSpec(
        "profanity_mild_per_1k", "profanity_mild_hits", "profanity_mild",
        "words", 1000, "profanity", "Mild profanity",
        "hits per 1,000 words", "negative",
    ),
    MetricSpec(
        "profanity_strong_per_1k", "profanity_strong_hits", "profanity_strong",
        "words", 1000, "profanity", "Strong profanity",
        "hits per 1,000 words", "negative",
    ),
    MetricSpec(
        "profanity_slurs_per_1k", "profanity_slurs_hits", "profanity_slurs",
        "words", 1000, "identity_slur", "Identity slurs (context audit required)",
        "hits per 1,000 words", "negative",
    ),
    MetricSpec(
        "outgroup_ref_per_1k", "outgroup_refs", "outgroup_refs",
        "words", 1000, "target_reference", "References to the other party",
        "references per 1,000 words", "neutral", target_required=True,
    ),
    MetricSpec(
        "democrat_party_pej_per_1k", "democrat_party_pej", "democrat_party_pej",
        "words", 1000, "party_label", '"Democrat party" pejorative',
        "hits per 1,000 words", "negative", target_required=True,
    ),
    MetricSpec(
        "directed_comity_per_1k", "directed_comity_hits", "directed_comity_hits",
        "words", 1000, "target_context", "Comity near out-party references",
        "hits per 1,000 words", "positive", target_required=True,
    ),
    MetricSpec(
        "directed_hostility_per_1k", "directed_hostility_hits", "directed_hostility_hits",
        "words", 1000, "target_context", "Personal disrespect near out-party references",
        "hits per 1,000 words", "negative", target_required=True,
    ),
    MetricSpec(
        "directed_misconduct_per_1k", "directed_misconduct_hits",
        "directed_misconduct_hits", "words", 1000, "target_context",
        "Misconduct allegations near out-party references",
        "hits per 1,000 words", "negative", target_required=True,
    ),
    MetricSpec(
        "outgroup_comity_contexts_per_100_refs", "outgroup_comity_contexts",
        "outgroup_comity_contexts", "outgroup_refs", 100, "target_context",
        "Out-party references with nearby comity language",
        "contexts per 100 references", "positive", target_required=True,
    ),
    MetricSpec(
        "outgroup_hostility_contexts_per_100_refs", "outgroup_hostility_contexts",
        "outgroup_hostility_contexts", "outgroup_refs", 100, "target_context",
        "Out-party references with nearby personal disrespect",
        "contexts per 100 references", "negative", target_required=True,
    ),
    MetricSpec(
        "outgroup_misconduct_contexts_per_100_refs", "outgroup_misconduct_contexts",
        "outgroup_misconduct_contexts", "outgroup_refs", 100, "target_context",
        "Out-party references with nearby misconduct allegations",
        "contexts per 100 references", "negative", target_required=True,
    ),
)

BY_RATE = {metric.rate: metric for metric in METRICS}
RATE_TO_RAW = {metric.rate: metric.raw_count for metric in METRICS}
SCORE_KEYS = tuple(dict.fromkeys(metric.score_key for metric in METRICS))
RAW_COUNTS = tuple(dict.fromkeys(metric.raw_count for metric in METRICS))
HEADLINE_METRICS = tuple(metric for metric in METRICS if metric.headline)
CHAMBER_METRICS = tuple(metric for metric in METRICS if metric.chamber_plot)
