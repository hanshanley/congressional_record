"""Canonical legislative bill records and member-level activity metrics.

The source adapters normalize Congress.gov JSON and GovInfo Bill Status XML into
one compact row per H.R. or S. bill.  Bioguide IDs are the join key to the
speaker table; names are display metadata only.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

import pandas as pd

BILL_TYPES = frozenset({"HR", "S"})
BILL_SCHEMA_VERSION = "2026-08-v1"

BILL_COLUMNS = [
    "bill_id",
    "source",
    "source_url",
    "source_updated_at",
    "congress",
    "bill_type",
    "bill_number",
    "title",
    "origin_chamber",
    "introduced_date",
    "sponsor_bioguide",
    "sponsor_name",
    "sponsor_party",
    "sponsor_state",
    "is_by_request",
    "passed_house",
    "passed_house_date",
    "passed_senate",
    "passed_senate_date",
    "passed_any_chamber",
    "became_law",
    "law_type",
    "law_number",
    "became_law_date",
    "matched_action_codes",
    "schema_version",
]

_BOOL_COLUMNS = [
    "is_by_request",
    "passed_house",
    "passed_senate",
    "passed_any_chamber",
    "became_law",
]

# Official Bill Status action codes from the GPO user guide.
_HOUSE_PASSAGE_CODES = frozenset({"8000"})
_SENATE_PASSAGE_CODES = frozenset({"17000"})
_BECAME_LAW_CODES = frozenset({"36000"})

# Compatibility fallback for Congress.gov records that omit actionCode. These
# deliberately match measure-level passage language, not arbitrary motions that
# happen to contain the word "passed".
_HOUSE_PASSAGE_TEXT = re.compile(
    r"^(?:passed(?:/agreed to)? in house|passed house)\b", re.IGNORECASE
)
_SENATE_PASSAGE_TEXT = re.compile(
    r"^(?:passed(?:/agreed to)? in senate|passed senate)\b", re.IGNORECASE
)
_BECAME_LAW_TEXT = re.compile(
    r"^became (?:public|private) law\b", re.IGNORECASE
)
_LAW_NUMBER = re.compile(r"^(?P<congress>[1-9]\d*)-(?P<number>[1-9]\d*)$")
_LAW_TYPES = frozenset({"public law", "private law"})


def bill_id(congress: int, bill_type: str, bill_number: int | str) -> str:
    """Return the stable source-independent bill identifier."""
    normalized = str(bill_type).strip().upper()
    if normalized not in BILL_TYPES:
        raise ValueError(f"unsupported bill type: {bill_type!r}")
    number = int(bill_number)
    if int(congress) <= 0 or number <= 0:
        raise ValueError("congress and bill number must be positive")
    return f"{int(congress)}-{normalized.lower()}-{number}"


def _date(action: Mapping[str, object]) -> str:
    return str(action.get("action_date") or action.get("actionDate") or "")[:10]


def _code(action: Mapping[str, object]) -> str:
    return str(action.get("action_code") or action.get("actionCode") or "").strip()


def _text(action: Mapping[str, object]) -> str:
    return str(action.get("text") or "").strip()


def action_milestones(actions: Sequence[Mapping[str, object]]) -> dict:
    """Extract passage/enactment milestones from normalized or source actions."""
    house_dates: list[str] = []
    senate_dates: list[str] = []
    law_dates: list[str] = []
    matched_codes: set[str] = set()

    for action in actions:
        code = _code(action)
        text = _text(action)
        date = _date(action)
        house = code in _HOUSE_PASSAGE_CODES or bool(_HOUSE_PASSAGE_TEXT.match(text))
        senate = code in _SENATE_PASSAGE_CODES or bool(_SENATE_PASSAGE_TEXT.match(text))
        law = code in _BECAME_LAW_CODES or bool(_BECAME_LAW_TEXT.match(text))
        if house:
            if date:
                house_dates.append(date)
            if code:
                matched_codes.add(code)
        if senate:
            if date:
                senate_dates.append(date)
            if code:
                matched_codes.add(code)
        if law:
            if date:
                law_dates.append(date)
            if code:
                matched_codes.add(code)

    passed_house = bool(house_dates) or any(
        _code(a) in _HOUSE_PASSAGE_CODES or _HOUSE_PASSAGE_TEXT.match(_text(a))
        for a in actions
    )
    passed_senate = bool(senate_dates) or any(
        _code(a) in _SENATE_PASSAGE_CODES or _SENATE_PASSAGE_TEXT.match(_text(a))
        for a in actions
    )
    return {
        "passed_house": passed_house,
        "passed_house_date": min(house_dates, default=""),
        "passed_senate": passed_senate,
        "passed_senate_date": min(senate_dates, default=""),
        "passed_any_chamber": passed_house or passed_senate,
        "became_law_date": min(law_dates, default=""),
        "matched_action_codes": json.dumps(sorted(matched_codes)),
    }


def _select_law(
    laws: Sequence[Mapping[str, object]], congress: int
) -> Mapping[str, str]:
    fallback: Mapping[str, str] = {}
    for item in laws:
        if not isinstance(item, Mapping):
            continue
        law_type = str(item.get("type") or "").strip()
        law_number = str(item.get("number") or "").strip()
        match = _LAW_NUMBER.fullmatch(law_number)
        if law_type.casefold() not in _LAW_TYPES or match is None:
            continue
        citation = {"type": law_type, "number": law_number}
        if int(match.group("congress")) == int(congress):
            return citation
        if not fallback:
            fallback = citation
    return fallback


def canonical_bill(
    *,
    source: str,
    source_url: str,
    source_updated_at: str,
    congress: int,
    bill_type: str,
    bill_number: int | str,
    title: str,
    origin_chamber: str,
    introduced_date: str,
    sponsors: Sequence[Mapping[str, object]],
    actions: Sequence[Mapping[str, object]],
    laws: Sequence[Mapping[str, object]],
) -> dict:
    """Build one canonical H.R./S. bill row from source-neutral fields."""
    normalized_type = str(bill_type).strip().upper()
    identity = bill_id(congress, normalized_type, bill_number)
    sponsor = sponsors[0] if sponsors else {}
    milestones = action_milestones(actions)
    law = _select_law(laws, congress)
    became_law = bool(laws)
    chamber = str(origin_chamber or "").strip().lower()
    chamber = {"h": "house", "s": "senate"}.get(chamber, chamber)
    return {
        "bill_id": identity,
        "source": str(source),
        "source_url": str(source_url),
        "source_updated_at": str(source_updated_at),
        "congress": int(congress),
        "bill_type": normalized_type,
        "bill_number": int(bill_number),
        "title": str(title or ""),
        "origin_chamber": chamber,
        "introduced_date": str(introduced_date or "")[:10],
        "sponsor_bioguide": str(
            sponsor.get("bioguide_id") or sponsor.get("bioguideId") or ""
        ),
        "sponsor_name": str(
            sponsor.get("full_name")
            or sponsor.get("fullName")
            or sponsor.get("name")
            or ""
        ),
        "sponsor_party": str(sponsor.get("party") or ""),
        "sponsor_state": str(sponsor.get("state") or ""),
        "is_by_request": str(
            sponsor.get("is_by_request") or sponsor.get("isByRequest") or ""
        ).strip().upper()
        in {"Y", "YES", "TRUE", "1"},
        **milestones,
        "became_law": became_law,
        "law_type": str(law.get("type") or ""),
        "law_number": str(law.get("number") or ""),
        "schema_version": BILL_SCHEMA_VERSION,
    }


def empty_bills() -> pd.DataFrame:
    """Return an empty canonical bill frame with stable column dtypes."""
    frame = pd.DataFrame(columns=BILL_COLUMNS)
    frame["congress"] = frame["congress"].astype("int64")
    frame["bill_number"] = frame["bill_number"].astype("int64")
    for column in _BOOL_COLUMNS:
        frame[column] = frame[column].astype("bool")
    return frame


def normalize_bills(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate, type, de-duplicate, and deterministically order bill rows."""
    if frame is None or frame.empty:
        return empty_bills()
    missing = set(BILL_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"bill frame missing columns: {sorted(missing)}")
    out = frame[BILL_COLUMNS].copy()
    out["congress"] = pd.to_numeric(out["congress"], errors="raise").astype("int64")
    out["bill_number"] = pd.to_numeric(out["bill_number"], errors="raise").astype("int64")
    out["origin_chamber"] = (
        out["origin_chamber"].fillna("").astype(str).str.strip().str.lower()
        .replace({"h": "house", "s": "senate"})
    )
    if not set(out["bill_type"]).issubset(BILL_TYPES):
        raise ValueError("canonical bill table may contain only HR and S measures")
    expected_ids = [
        bill_id(c, t, n)
        for c, t, n in zip(out["congress"], out["bill_type"], out["bill_number"])
    ]
    if list(out["bill_id"]) != expected_ids:
        raise ValueError("bill_id does not match congress/type/number")
    for column in _BOOL_COLUMNS:
        out[column] = out[column].fillna(False).astype("bool")
    if (out["became_law"] & ~out["passed_any_chamber"]).any():
        raise ValueError("an enacted bill must have passed at least one chamber")
    out = out.drop_duplicates("bill_id", keep="last")
    return out.sort_values(["congress", "bill_type", "bill_number"]).reset_index(drop=True)


def merge_bills(
    existing: Optional[pd.DataFrame], fresh: pd.DataFrame
) -> pd.DataFrame:
    """Merge bill records idempotently, with fresh source rows winning."""
    if existing is None or existing.empty:
        return normalize_bills(fresh)
    if fresh is None or fresh.empty:
        return normalize_bills(existing)
    combined = pd.concat([existing, fresh], ignore_index=True)
    combined = combined.drop_duplicates("bill_id", keep="last")
    return normalize_bills(combined)


def load_bills(path: Path) -> Optional[pd.DataFrame]:
    """Load a partition directory or one canonical Parquet file."""
    if path.is_dir():
        parts = sorted(path.glob("congress_*.parquet"))
        if not parts:
            return None
        return normalize_bills(
            pd.concat([pd.read_parquet(part) for part in parts], ignore_index=True)
        )
    if path.exists():
        return normalize_bills(pd.read_parquet(path))
    return None


def save_bills(frame: pd.DataFrame, path: Path) -> list[Path]:
    """Write changed Congress partitions and return the files replaced."""
    normalized = normalize_bills(frame)
    path.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for congress, group in normalized.groupby("congress", sort=True):
        part = path / f"congress_{int(congress):03d}.parquet"
        ordered = group.reset_index(drop=True)
        if part.exists() and ordered.equals(normalize_bills(pd.read_parquet(part))):
            continue
        tmp = part.with_suffix(".parquet.tmp")
        ordered.to_parquet(tmp, index=False)
        tmp.replace(part)
        written.append(part)
    return written


def bill_member_totals(
    bills: pd.DataFrame, congress: Optional[int] = None
) -> pd.DataFrame:
    """Aggregate sponsored, passed, and enacted bills by sponsor Bioguide ID."""
    frame = normalize_bills(bills)
    if congress is not None:
        frame = frame[frame["congress"] == int(congress)]
    frame = frame[frame["sponsor_bioguide"].astype(str).str.strip().ne("")]
    if frame.empty:
        return pd.DataFrame(columns=[
            "bioguide", "speaker_name", "party", "state", "chamber",
            "bills_sponsored", "bills_passed", "bills_enacted",
            "passage_share", "enactment_share",
        ])
    grouped = frame.groupby("sponsor_bioguide", as_index=False).agg(
        speaker_name=("sponsor_name", "last"),
        party=("sponsor_party", "last"),
        state=("sponsor_state", "last"),
        chamber=("origin_chamber", "last"),
        bills_sponsored=("bill_id", "nunique"),
        bills_passed=("passed_any_chamber", "sum"),
        bills_enacted=("became_law", "sum"),
    ).rename(columns={"sponsor_bioguide": "bioguide"})
    grouped["bills_passed"] = grouped["bills_passed"].astype("int64")
    grouped["bills_enacted"] = grouped["bills_enacted"].astype("int64")
    grouped["passage_share"] = grouped["bills_passed"] / grouped["bills_sponsored"]
    grouped["enactment_share"] = grouped["bills_enacted"] / grouped["bills_sponsored"]
    return grouped


def speech_member_totals(
    daily: pd.DataFrame, congress: Optional[int] = None
) -> pd.DataFrame:
    """Aggregate the committed daily speaker table to one row per member."""
    frame = daily if congress is None else daily[daily["congress"] == int(congress)]
    # Extensions of Remarks are submitted for publication rather than spoken on
    # the floor. They remain in the underlying audit table but do not answer
    # "who talked most" and must not enter a spoken-word profanity denominator.
    frame = frame[frame["chamber"].isin(["house", "senate"])]
    if frame.empty:
        return pd.DataFrame(columns=[
            "bioguide", "speaker_name", "party", "state", "chamber", "turns",
            "words", "active_days", "profanity_hits", "profanity_quoted_hits",
            "hostility_hits", "misconduct_hits",
        ])
    return frame.groupby("bioguide", as_index=False).agg(
        speaker_name=("speaker_name", "last"),
        party=("party", "last"),
        state=("state", "last"),
        chamber=("chamber", "last"),
        turns=("turns", "sum"),
        words=("words", "sum"),
        active_days=("date", "nunique"),
        profanity_hits=("profanity_hits", "sum"),
        profanity_quoted_hits=("profanity_quoted_hits", "sum"),
        hostility_hits=("hostility_hits", "sum"),
        misconduct_hits=("misconduct_hits", "sum"),
    )


def member_activity(
    daily: pd.DataFrame,
    bills: pd.DataFrame,
    congress: int,
) -> pd.DataFrame:
    """Outer-join speech and legislative totals by Bioguide ID."""
    speech = speech_member_totals(daily, congress)
    legislative = bill_member_totals(bills, congress)
    joined = speech.merge(
        legislative,
        on="bioguide",
        how="outer",
        suffixes=("_speech", "_bill"),
    )
    for field in ("speaker_name", "party", "state", "chamber"):
        joined[field] = joined.get(f"{field}_speech").fillna(
            joined.get(f"{field}_bill")
        ).fillna("")
    joined = joined.drop(columns=[
        column
        for column in joined.columns
        if column.endswith("_speech") or column.endswith("_bill")
    ])
    count_columns = [
        "turns", "words", "active_days", "profanity_hits",
        "profanity_quoted_hits", "hostility_hits", "misconduct_hits",
        "bills_sponsored", "bills_passed", "bills_enacted",
    ]
    for column in count_columns:
        if column not in joined:
            joined[column] = 0
        joined[column] = (
            pd.to_numeric(joined[column], errors="coerce").fillna(0).astype("int64")
        )
    for column in ("passage_share", "enactment_share"):
        if column not in joined:
            joined[column] = 0.0
        joined[column] = pd.to_numeric(
            joined[column], errors="coerce"
        ).fillna(0.0)
    for metric in ("profanity", "hostility", "misconduct"):
        joined[f"{metric}_per_100k"] = (
            100_000
            * joined[f"{metric}_hits"]
            / joined["words"].where(joined["words"] > 0)
        ).fillna(0.0)
    return joined.sort_values("bioguide").reset_index(drop=True)


def activity_leaderboards(
    activity: pd.DataFrame,
    *,
    top: int = 25,
    min_words: int = 25_000,
) -> dict[str, pd.DataFrame]:
    """Build the five deterministic dashboard leaderboards."""
    def ranked(
        frame: pd.DataFrame, columns: Iterable[str], ascending: Sequence[bool]
    ) -> pd.DataFrame:
        ordered = frame.sort_values(
            [*columns, "bioguide"], ascending=[*ascending, True]
        ).head(top).reset_index(drop=True)
        ordered.insert(0, "rank", range(1, len(ordered) + 1))
        return ordered

    legislative = activity[activity["bills_sponsored"] > 0]
    profanity = activity[activity["words"] >= int(min_words)]
    return {
        "speech": ranked(activity[activity["words"] > 0], ["words", "turns"], [False, False]),
        "sponsored": ranked(
            legislative, ["bills_sponsored", "bills_passed"], [False, False]
        ),
        "passed": ranked(
            legislative[legislative["bills_passed"] > 0],
            ["bills_passed", "bills_sponsored"],
            [False, False],
        ),
        "enacted": ranked(
            legislative[legislative["bills_enacted"] > 0],
            ["bills_enacted", "bills_sponsored"],
            [False, False],
        ),
        "profanity": ranked(
            profanity, ["profanity_per_100k", "profanity_hits"], [False, False]
        ),
    }
