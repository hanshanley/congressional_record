"""Deterministic sampling and reporting for disclosed model-assisted validation."""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import pyarrow.parquet as pq

from analysis.inputs import select_turn_files
from analysis.score.scorers import Scorers

LOG = logging.getLogger("analysis.validate")

RUBRIC_VERSION = "2026-07-v1"
PASSAGE_CHARS = 1200

_ERAS = (
    (1873, 1900, "1873-1900"),
    (1901, 1945, "1901-1945"),
    (1946, 1980, "1946-1980"),
    (1981, 1993, "1981-1993"),
    (1994, 2016, "1994-2016"),
    (2017, 2026, "2017-2026"),
)

_SIGNALS = {
    "formal_courtesy": "formal_courtesy_hits",
    "gratitude_praise": "gratitude_praise_hits",
    "cooperation": "cooperation_hits",
    "personal_attack": "hostility_hits",
    "misconduct_allegation": "misconduct_hits",
    "profanity": "profanity_hits",
    "identity_slur": "profanity_slurs",
    "outparty_target": "outgroup_refs",
}

_READ_COLS = [
    "turn_id", "source", "date", "congress", "chamber", "speaker_name", "party",
    "is_procedural", "text",
]


def _era(year: int) -> str:
    for start, end, label in _ERAS:
        if start <= year <= end:
            return label
    return "outside"


def _year(row: Dict[str, object]) -> int:
    date = str(row.get("date") or "")
    if len(date) >= 4 and date[:4].isdigit():
        return int(date[:4])
    return 1789 + 2 * (int(row["congress"]) - 1)


def _source_family(source: str) -> str:
    return "hein" if source.startswith("hein_") else source


def _lexicon_regex(lexicons: List[object]) -> re.Pattern:
    singles = sorted(
        {term for lexicon in lexicons for term in lexicon.singles},
        key=len, reverse=True,
    )
    parts = []
    if singles:
        parts.append(r"\b(?:" + "|".join(re.escape(term) for term in singles) + r")\b")
    parts.extend(
        lexicon.phrase_re.pattern
        for lexicon in lexicons
        if lexicon.phrase_re is not None
    )
    return re.compile("|".join(parts))


def _signal_trigger_regexes(scorer: Scorers) -> Dict[str, re.Pattern]:
    idiom_pattern = (
        scorer.outgroup_idiom.phrase_re.pattern
        if scorer.outgroup_idiom.phrase_re is not None else r"(?!x)x"
    )
    outparty = re.compile(
        r"\brepublicans?\b|\bgop\b|\bdemocrats?\b|"
        r"\bdemocratic\s+(?:party|colleagues?|members?|caucus|leadership|side|conference)\b|"
        + idiom_pattern,
    )
    return {
        "formal_courtesy": _lexicon_regex([scorer.formal_courtesy]),
        "gratitude_praise": _lexicon_regex([scorer.gratitude_praise]),
        "cooperation": _lexicon_regex([scorer.cooperation]),
        "personal_attack": _lexicon_regex([scorer.hostility]),
        "misconduct_allegation": _lexicon_regex([scorer.misconduct]),
        "profanity": _lexicon_regex([scorer.profanity["mild"], scorer.profanity["strong"]]),
        "identity_slur": _lexicon_regex([scorer.profanity["slurs"]]),
        "outparty_target": outparty,
    }


def _priority(turn_id: str, stratum: str) -> int:
    digest = hashlib.blake2b(
        f"{RUBRIC_VERSION}|{stratum}|{turn_id}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big")


def _reservoir_accepts(
    reservoirs: Dict[str, List[Tuple[int, str, dict]]],
    stratum: str,
    turn_id: str,
    quota: int,
) -> tuple[bool, int]:
    priority = _priority(turn_id, stratum)
    candidate = (-priority, turn_id)
    heap = reservoirs[stratum]
    accepted = len(heap) < quota or candidate > heap[0][:2]
    return accepted, priority


def _reservoir_add(
    reservoirs: Dict[str, List[Tuple[int, str, dict]]],
    stratum: str,
    row: dict,
    quota: int,
    priority: int,
) -> None:
    # Negative priority makes heap[0] the currently worst (largest original hash).
    item = (-priority, row["turn_id"], row)
    heap = reservoirs[stratum]
    if len(heap) < quota:
        heapq.heappush(heap, item)
    elif item[0] > heap[0][0]:
        heapq.heapreplace(heap, item)


def _passage(text: str, center: int) -> tuple[str, int, int]:
    if len(text) <= PASSAGE_CHARS:
        return text, 0, len(text)
    start = max(0, min(center - PASSAGE_CHARS // 2, len(text) - PASSAGE_CHARS))
    end = start + PASSAGE_CHARS
    return text[start:end], start, end


def _base_row(row: Dict[str, object], stratum: str, center: int) -> dict:
    text = str(row.get("text") or "")
    passage, start, end = _passage(text, center)
    return {
        "turn_id": str(row["turn_id"]),
        "source": str(row["source"]),
        "source_family": _source_family(str(row["source"])),
        "date": str(row.get("date") or ""),
        "congress": int(row["congress"]),
        "year": _year(row),
        "era": _era(_year(row)),
        "chamber": str(row["chamber"]),
        "speaker_name": str(row.get("speaker_name") or ""),
        "party": str(row["party"]),
        "is_procedural": bool(row["is_procedural"]),
        "_sampling_stratum": stratum,
        "passage_start": start,
        "passage_end": end,
        "passage": passage,
        "passage_sha256": hashlib.sha256(passage.encode()).hexdigest(),
    }


def build_validation_sample(
    turns_dir: Path,
    out_dir: Path,
    signal_quota: int = 3,
    random_quota: int = 4,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Scan real turns and create blinded passages plus hidden production features."""
    scorer = Scorers()
    signal_triggers = _signal_trigger_regexes(scorer)
    trigger = re.compile("|".join(regex.pattern for regex in signal_triggers.values()))
    reservoirs: Dict[str, List[Tuple[int, str, dict]]] = defaultdict(list)
    seen_govinfo_ids: set[str] = set()

    for path in select_turn_files(turns_dir):
        is_govinfo = path.name.startswith("govinfo")
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=20_000, columns=_READ_COLS):
            data = batch.to_pydict()
            for i, turn_id in enumerate(data["turn_id"]):
                if is_govinfo:
                    if turn_id in seen_govinfo_ids:
                        continue
                    seen_govinfo_ids.add(turn_id)
                chamber = data["chamber"][i]
                party = data["party"][i]
                if chamber not in {"house", "senate"} or party not in {"D", "R"}:
                    continue
                row = {column: data[column][i] for column in _READ_COLS}
                year = _year(row)
                base = (
                    f"{_era(year)}|{_source_family(str(row['source']))}|"
                    f"{chamber}|{party}"
                )
                random_stratum = f"{base}|random"
                text = str(row.get("text") or "")
                accepted, random_priority = _reservoir_accepts(
                    reservoirs, random_stratum, str(turn_id), random_quota
                )
                if accepted:
                    random_center = random_priority % max(1, len(text))
                    _reservoir_add(
                        reservoirs,
                        random_stratum,
                        _base_row(row, random_stratum, random_center),
                        random_quota,
                        random_priority,
                    )
                if row["is_procedural"]:
                    procedural_stratum = f"{base}|procedural"
                    accepted, procedural_priority = _reservoir_accepts(
                        reservoirs, procedural_stratum, str(turn_id), random_quota
                    )
                    if accepted:
                        procedural_center = procedural_priority % max(1, len(text))
                        _reservoir_add(
                            reservoirs,
                            procedural_stratum,
                            _base_row(row, procedural_stratum, procedural_center),
                            random_quota,
                            procedural_priority,
                        )

                match = trigger.search(text.lower())
                if not match:
                    continue
                accepted_spans = scorer.signal_spans(text, str(party))
                for signal in _SIGNALS:
                    spans = accepted_spans[signal]
                    if not spans:
                        continue
                    stratum = f"{base}|{signal}"
                    accepted, signal_priority = _reservoir_accepts(
                        reservoirs, stratum, str(turn_id), signal_quota
                    )
                    if not accepted:
                        continue
                    center = spans[signal_priority % len(spans)][0]
                    sampled = _base_row(row, stratum, center)
                    if scorer.score_turn(sampled["passage"], str(party))[_SIGNALS[signal]] <= 0:
                        raise AssertionError(
                            f"accepted {signal} span missing from passage for {turn_id}"
                        )
                    _reservoir_add(
                        reservoirs, stratum, sampled, signal_quota, signal_priority
                    )

    sampled_rows = [
        item[2]
        for stratum in sorted(reservoirs)
        for item in sorted(reservoirs[stratum], reverse=True)
    ]
    # A turn can legitimately appear in two signal strata; sample_id keeps annotations distinct.
    production_rows = []
    for index, row in enumerate(sampled_rows, start=1):
        sampling_stratum = row.pop("_sampling_stratum")
        identity = (
            f"{row['turn_id']}|{sampling_stratum}|{row['passage_sha256']}"
        ).encode()
        row["sample_id"] = "VAL-" + hashlib.blake2b(
            identity, digest_size=10
        ).hexdigest().upper()
        # Annotators see the bounded passage, so validation predictions must be scored
        # on that exact same text rather than on unseen parts of the full turn.
        features = scorer.score_turn(row["passage"], row["party"])
        production_rows.append({
            "sample_id": row["sample_id"],
            "turn_id": row["turn_id"],
            "passage_sha256": row["passage_sha256"],
            "sampling_stratum": sampling_stratum,
            **features,
        })
    blinded = pd.DataFrame(sampled_rows)

    production = pd.DataFrame(production_rows)

    out_dir.mkdir(parents=True, exist_ok=True)
    blinded.to_parquet(out_dir / "validation_sample_blinded.parquet", index=False)
    blinded.to_csv(out_dir / "validation_sample_blinded.csv", index=False)
    production.to_parquet(out_dir / "production_features_hidden.parquet", index=False)
    manifest = {
        "rubric_version": RUBRIC_VERSION,
        "real_source_only": True,
        "rows": len(blinded),
        "signal_quota": signal_quota,
        "random_quota": random_quota,
        "passage_chars": PASSAGE_CHARS,
        "source_files": [path.name for path in sorted(turns_dir.glob("*.parquet"))],
    }
    (out_dir / "sample_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    LOG.info("wrote %d real validation passages", len(blinded))
    return blinded, production


ANNOTATION_FIELDS = (
    "target_exists", "outparty_target_exists", "target_party",
    "formulaic_address", "procedural_deference",
    "gratitude_praise", "bipartisan_cooperation", "personal_attack",
    "misconduct_allegation", "ideological_label", "profanity", "identity_slur",
    "quoted_or_read_in", "ambiguous",
)
_YES_NO_UNCERTAIN = {"yes", "no", "uncertain"}
_ANNOTATION_VALUES = {
    **{
        field: _YES_NO_UNCERTAIN
        for field in ANNOTATION_FIELDS
        if field not in {"target_party", "ambiguous"}
    },
    "target_party": {"d", "r", "i", "other", "none", "uncertain"},
    "ambiguous": {"yes", "no"},
}
_CONFIDENCE_VALUES = {"low", "medium", "high"}


def _sample_id_set(frame: pd.DataFrame, name: str) -> set[str]:
    if "sample_id" not in frame:
        raise ValueError(f"{name} is missing sample_id")
    if frame["sample_id"].isna().any() or frame["sample_id"].astype(str).str.strip().eq("").any():
        raise ValueError(f"{name} contains blank sample_id values")
    if frame["sample_id"].duplicated().any():
        raise ValueError(f"{name} contains duplicate sample_id values")
    return set(frame["sample_id"].astype(str))


def _require_matching_identity(**frames: pd.DataFrame) -> None:
    """Fail if matching sample IDs refer to different real source passages."""
    _require_same_sample_ids(**frames)
    identity_columns = ("turn_id", "passage_sha256")
    for name, frame in frames.items():
        missing = [column for column in identity_columns if column not in frame]
        if missing:
            raise ValueError(f"{name} is missing validation identity columns: {missing}")
    reference_name, reference = next(iter(frames.items()))
    expected = reference.set_index("sample_id")[list(identity_columns)].astype(str).sort_index()
    for name, frame in frames.items():
        actual = frame.set_index("sample_id")[list(identity_columns)].astype(str).sort_index()
        if not actual.equals(expected):
            raise ValueError(
                f"validation identity mismatch: {name} vs {reference_name}"
            )


def _require_same_sample_ids(**frames: pd.DataFrame) -> None:
    sets = {name: _sample_id_set(frame, name) for name, frame in frames.items()}
    expected_name, expected = next(iter(sets.items()))
    for name, values in sets.items():
        if values != expected:
            missing = sorted(expected - values)[:5]
            extra = sorted(values - expected)[:5]
            raise ValueError(
                f"sample_id mismatch: {name} vs {expected_name}; "
                f"missing={missing}, extra={extra}"
            )


def write_annotation_batches(
    sample_path: Path, out_dir: Path, batch_size: int = 50
) -> List[Path]:
    """Split blinded passages into stable CSV batches for independent annotation passes."""
    sample = pd.read_parquet(sample_path) if sample_path.suffix == ".parquet" else pd.read_csv(sample_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for start in range(0, len(sample), batch_size):
        batch = sample.iloc[start:start + batch_size].copy()
        for field in ANNOTATION_FIELDS:
            batch[field] = ""
        batch["confidence"] = ""
        batch["rationale"] = ""
        path = out_dir / f"batch_{start // batch_size + 1:03d}.csv"
        batch.to_csv(path, index=False)
        written.append(path)
    return written


def read_annotation_pass(pass_dir: Path) -> pd.DataFrame:
    """Load and validate one completed annotation pass."""
    files = sorted(pass_dir.glob("batch_*.csv"))
    if not files:
        raise FileNotFoundError(f"no annotation batches in {pass_dir}")
    frame = pd.concat((pd.read_csv(path) for path in files), ignore_index=True)
    _sample_id_set(frame, str(pass_dir))
    identity_missing = [
        column for column in ("turn_id", "passage_sha256") if column not in frame
    ]
    if identity_missing:
        raise ValueError(
            f"missing validation identity columns in {pass_dir}: {identity_missing}"
        )
    required = [*ANNOTATION_FIELDS, "confidence", "rationale"]
    missing = [field for field in required if field not in frame]
    if missing:
        raise ValueError(f"missing annotation fields in {pass_dir}: {missing}")
    for field, allowed in _ANNOTATION_VALUES.items():
        normalized = frame[field].fillna("").astype(str).str.strip().str.lower()
        invalid = sorted(set(normalized) - allowed)
        if invalid:
            raise ValueError(f"invalid {field} values in {pass_dir}: {invalid}")
        frame[field] = normalized
    confidence = frame["confidence"].fillna("").astype(str).str.strip().str.lower()
    invalid_confidence = sorted(set(confidence) - _CONFIDENCE_VALUES)
    if invalid_confidence:
        raise ValueError(
            f"invalid confidence values in {pass_dir}: {invalid_confidence}"
        )
    frame["confidence"] = confidence
    if frame["rationale"].fillna("").astype(str).str.strip().eq("").any():
        raise ValueError(f"blank rationale values in {pass_dir}")
    return frame


def validation_report(pass_a: pd.DataFrame, pass_b: pd.DataFrame) -> pd.DataFrame:
    """Compute transparent raw agreement for two independent annotation passes."""
    _require_same_sample_ids(pass_a=pass_a, pass_b=pass_b)
    if all(
        column in frame
        for frame in (pass_a, pass_b)
        for column in ("turn_id", "passage_sha256")
    ):
        _require_matching_identity(pass_a=pass_a, pass_b=pass_b)
    merged = pass_a.merge(pass_b, on="sample_id", suffixes=("_a", "_b"), validate="one_to_one")
    rows = []
    for field in ANNOTATION_FIELDS:
        left, right = f"{field}_a", f"{field}_b"
        if left not in merged or right not in merged:
            continue
        comparable = (
            merged[left].notna() & merged[right].notna()
            & merged[left].astype(str).str.strip().ne("")
            & merged[right].astype(str).str.strip().ne("")
        )
        rows.append({
            "field": field,
            "n": int(comparable.sum()),
            "raw_agreement": (
                float((merged.loc[comparable, left] == merged.loc[comparable, right]).mean())
                if comparable.any() else None
            ),
        })
    return pd.DataFrame(rows)


_PRODUCTION_MAP = {
    "outparty_target_exists": "outgroup_refs",
    "formulaic_address": "formal_courtesy_hits",
    "gratitude_praise": "gratitude_praise_hits",
    "bipartisan_cooperation": "cooperation_hits",
    "personal_attack": "hostility_hits",
    "misconduct_allegation": "misconduct_hits",
    "ideological_label": "ideological_label_hits",
    "profanity": "profanity_hits",
    "identity_slur": "profanity_slurs",
}


def adjudication_input(pass_a: pd.DataFrame, pass_b: pd.DataFrame) -> pd.DataFrame:
    """Return only samples with at least one substantive disagreement."""
    _require_same_sample_ids(pass_a=pass_a, pass_b=pass_b)
    merged = pass_a.merge(pass_b, on="sample_id", suffixes=("_a", "_b"), validate="one_to_one")
    disagreement = pd.Series(False, index=merged.index)
    for field in ANNOTATION_FIELDS:
        disagreement |= merged[f"{field}_a"].fillna("") != merged[f"{field}_b"].fillna("")
    return merged[disagreement].copy()


def precision_recall_report(
    final_annotations: pd.DataFrame,
    production_features: pd.DataFrame,
    sample_metadata: pd.DataFrame,
) -> pd.DataFrame:
    """Compare adjudicated model labels with production detectors by era/source."""
    _require_same_sample_ids(
        final_annotations=final_annotations,
        production_features=production_features,
        sample_metadata=sample_metadata,
    )
    merged = (
        final_annotations.merge(production_features, on="sample_id", validate="one_to_one")
        .merge(
            sample_metadata[["sample_id", "era", "source_family"]],
            on="sample_id", validate="one_to_one",
        )
    )
    rows = []
    for field, production_col in _PRODUCTION_MAP.items():
        groups = [(("all", "all"), merged)]
        groups.extend(merged.groupby(["era", "source_family"], dropna=False))
        for (era, source), group in groups:
            labels = group[field].astype(str).str.lower()
            valid = labels.isin(["yes", "no"])
            truth = labels[valid].eq("yes")
            predicted = group.loc[valid, production_col].fillna(0).gt(0)
            tp = int((truth & predicted).sum())
            fp = int((~truth & predicted).sum())
            fn = int((truth & ~predicted).sum())
            rows.append({
                "field": field, "era": era, "source_family": source,
                "n_coded": int(valid.sum()), "tp": tp, "fp": fp, "fn": fn,
                "precision": tp / (tp + fp) if tp + fp else None,
                "recall": tp / (tp + fn) if tp + fn else None,
            })
    return pd.DataFrame(rows)


def finalize_annotations(
    pass_a: pd.DataFrame,
    pass_b: pd.DataFrame,
    adjudicated: pd.DataFrame,
    production_features: pd.DataFrame,
    sample_metadata: pd.DataFrame,
    out_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Combine agreements with adjudicated disagreements and write validation reports."""
    _require_matching_identity(
        pass_a=pass_a,
        pass_b=pass_b,
        sample_metadata=sample_metadata,
    )
    _require_matching_identity(
        production_features=production_features,
        sample_metadata=sample_metadata,
    )
    agreement = validation_report(pass_a, pass_b)
    merged = pass_a.merge(pass_b, on="sample_id", suffixes=("_a", "_b"), validate="one_to_one")
    expected_adjudication = set(adjudication_input(pass_a, pass_b)["sample_id"].astype(str))
    actual_adjudication = _sample_id_set(adjudicated, "adjudicated")
    if actual_adjudication != expected_adjudication:
        missing = sorted(expected_adjudication - actual_adjudication)[:5]
        extra = sorted(actual_adjudication - expected_adjudication)[:5]
        raise ValueError(
            f"adjudication sample_id mismatch; missing={missing}, extra={extra}"
        )
    adjudicated = adjudicated.set_index("sample_id")
    final_rows = []
    for row in merged.itertuples(index=False):
        sample_id = row.sample_id
        final = {"sample_id": sample_id}
        had_disagreement = False
        for field in ANNOTATION_FIELDS:
            left = getattr(row, f"{field}_a")
            right = getattr(row, f"{field}_b")
            if str(left) == str(right) and pd.notna(left):
                final[field] = left
            else:
                had_disagreement = True
                if sample_id not in adjudicated.index:
                    raise ValueError(f"missing adjudication for {sample_id}:{field}")
                final[field] = adjudicated.loc[sample_id, field]
        if had_disagreement:
            final["confidence"] = adjudicated.loc[sample_id, "confidence"]
            final["rationale"] = adjudicated.loc[sample_id, "rationale"]
        else:
            final["confidence"] = getattr(row, "confidence_a", "")
            final["rationale"] = getattr(row, "rationale_a", "")
        final_rows.append(final)
    final_labels = pd.DataFrame(final_rows)
    accuracy = precision_recall_report(
        final_labels, production_features, sample_metadata
    )
    provenance_columns = [
        "sample_id", "turn_id", "source", "source_family", "date", "congress",
        "year", "era", "chamber", "speaker_name", "party", "passage_sha256",
    ]
    missing_provenance = [column for column in provenance_columns if column not in sample_metadata]
    if missing_provenance:
        raise ValueError(f"sample metadata missing provenance columns: {missing_provenance}")
    final_annotations = sample_metadata[provenance_columns].merge(
        final_labels, on="sample_id", validate="one_to_one"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    agreement.to_csv(out_dir / "agreement.csv", index=False)
    final_annotations.to_parquet(out_dir / "adjudicated_annotations.parquet", index=False)
    final_annotations.to_csv(out_dir / "adjudicated_annotations.csv", index=False)
    accuracy.to_csv(out_dir / "precision_recall.csv", index=False)
    return final_annotations, agreement, accuracy
