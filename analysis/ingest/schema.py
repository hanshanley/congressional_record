"""Unified speaker-turn schema shared by the hein and GovInfo ingesters.

One row = one contiguous speaking turn by one speaker. The two corpora are
normalized into this schema so downstream scoring/aggregation is source-agnostic.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Column order is authoritative for the parquet output.
TURN_COLUMNS: List[str] = [
    "turn_id",        # stable unique id  (e.g. "hein-daily:970000005" / "crec:<granuleId>#<n>")
    "source",         # hein_bound | hein_daily | govinfo
    "date",           # ISO date string YYYY-MM-DD (may be "" if unknown)
    "congress",       # int
    "chamber",        # house | senate | extensions | other
    "speaker_name",   # display name as printed
    "speaker_id",     # source speaker id (hein speakerid) if any, else ""
    "bioguide",       # bioguide id if known (GovInfo; hein usually "")
    "party",          # D | R | I | other  (normalized)
    "state",          # 2-letter state/territory or ""
    "word_count",     # int (recomputed from text when missing)
    "is_procedural",  # bool heuristic
    "text",           # normalized transcript text
]

CHAMBER_MAP = {
    "H": "house",
    "S": "senate",
    "HOUSE": "house",
    "SENATE": "senate",
    "EXTENSIONS": "extensions",
    "E": "extensions",
}


def normalize_chamber(raw: str | None) -> str:
    if not raw:
        return "other"
    return CHAMBER_MAP.get(str(raw).strip().upper(), "other")


def empty_turn() -> Dict[str, Any]:
    return {c: "" for c in TURN_COLUMNS}
