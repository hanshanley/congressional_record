"""Unified speaker-turn schema shared by the hein and GovInfo ingesters.

One row = one contiguous speaking turn by one speaker. The two corpora are
normalized into this schema so downstream scoring/aggregation is source-agnostic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq

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

# Authoritative Arrow schema for turn parquet (shared pipeline contract).
ARROW_SCHEMA = pa.schema(
    [
        ("turn_id", pa.string()),
        ("source", pa.string()),
        ("date", pa.string()),
        ("congress", pa.int32()),
        ("chamber", pa.string()),
        ("speaker_name", pa.string()),
        ("speaker_id", pa.string()),
        ("bioguide", pa.string()),
        ("party", pa.string()),
        ("state", pa.string()),
        ("word_count", pa.int64()),
        ("is_procedural", pa.bool_()),
        ("text", pa.string()),
    ]
)

CHAMBER_MAP = {
    "H": "house",
    "S": "senate",
    "HOUSE": "house",
    "SENATE": "senate",
    "EXTENSIONS": "extensions",
    "E": "extensions",
}

# Congress N convenes in this year (odd years from 1789).
_FIRST_CONGRESS_YEAR = 1789


def normalize_chamber(raw: str | None) -> str:
    if not raw:
        return "other"
    return CHAMBER_MAP.get(str(raw).strip().upper(), "other")


def congress_from_year(year: int) -> int:
    """Congress number containing ``year`` (e.g. 2025 -> 119)."""
    return (int(year) - _FIRST_CONGRESS_YEAR) // 2 + 1


def year_from_congress(congress: int) -> int:
    """First (convening) year of ``congress`` (e.g. 119 -> 2025)."""
    return _FIRST_CONGRESS_YEAR + 2 * (int(congress) - 1)


def empty_turn() -> Dict[str, Any]:
    return {c: "" for c in TURN_COLUMNS}


def write_turns_parquet(
    path: Path, rows: Iterator[Dict[str, Any]], batch_size: int = 50_000
) -> int:
    """Stream ``rows`` to one parquet file in row-group batches. Returns row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    writer: Optional[pq.ParquetWriter] = None
    batch: List[Dict[str, Any]] = []
    total = 0

    def flush() -> None:
        nonlocal writer, batch
        if not batch:
            return
        table = pa.Table.from_pylist(batch, schema=ARROW_SCHEMA)
        if writer is None:
            writer = pq.ParquetWriter(path, ARROW_SCHEMA, compression="zstd")
        writer.write_table(table)
        batch = []

    for r in rows:
        batch.append(r)
        total += 1
        if len(batch) >= batch_size:
            flush()
    flush()
    if writer is not None:
        writer.close()
    else:  # no rows -> write an empty table so downstream globs still work
        pq.write_table(pa.Table.from_pylist([], schema=ARROW_SCHEMA), path, compression="zstd")
    return total
