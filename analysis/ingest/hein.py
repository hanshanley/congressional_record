"""Ingest the Stanford *hein* corpus (bound + daily) into unified turn parquet.

Each hein "speech" is already one speaker turn. We join three pipe-delimited,
latin-1 files per congress:

* ``NNN_SpeakerMap.txt``  speech_id -> speaker id / party / state (matched members only)
* ``descr_NNN.txt``       speech_id -> chamber / date / speaker / word_count (all speeches)
* ``speeches_NNN.txt``    speech_id -> speech text

Coverage dedup: bound covers congresses 043-111, daily 097-114. To avoid double
counting we take **bound for 043-096 and daily for 097-114**, yielding a continuous
043-114 with no overlap.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from analysis.ingest.schema import TURN_COLUMNS, normalize_chamber
from analysis.normalize.parties import normalize_party

LOG = logging.getLogger("analysis.ingest.hein")

_ENC = "latin-1"  # OCR text is not valid UTF-8; latin-1 never raises.

# Speakers that denote procedural/chair roles rather than a member's substantive remarks.
_PROCEDURAL_RE = re.compile(
    r"^\s*(the\s+)?(vice\s+president|president\s+pro\s+tempore|presiding\s+officer|"
    r"speaker(\s+pro\s+tempore)?|acting\s+president|chief\s+clerk|the\s+clerk|clerk|"
    r"chairman|chairwoman|the\s+chair)\b",
    re.IGNORECASE,
)


def _fmt_date(raw: str) -> str:
    raw = (raw or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def _read_delimited(z: zipfile.ZipFile, member: str) -> Iterator[list[str]]:
    """Yield field lists for a pipe-delimited hein file (skipping the header)."""
    with z.open(member) as fh:
        first = True
        for raw in fh:
            if first:  # header
                first = False
                continue
            line = raw.decode(_ENC).rstrip("\n").rstrip("\r")
            if not line:
                continue
            yield line.split("|")


def _member(edition: str, congress: str, kind: str) -> str:
    root = "hein-bound" if edition == "bound" else "hein-daily"
    if kind == "speakermap":
        return f"{root}/{congress}_SpeakerMap.txt"
    return f"{root}/{kind}_{congress}.txt"


def _load_speakermap_idx(z: zipfile.ZipFile, edition: str, congress: str) -> Dict[str, Dict[str, str]]:
    idx: Dict[str, Dict[str, str]] = {}
    member = _member(edition, congress, "speakermap")
    # speakerid|speech_id|lastname|firstname|chamber|state|gender|party|district|nonvoting
    for f in _read_delimited(z, member):
        if len(f) < 8:
            continue
        speech_id = f[1]
        idx[speech_id] = {
            "speakerid": f[0],
            "lastname": f[2],
            "firstname": f[3],
            "chamber": f[4],
            "state": f[5],
            "party": f[7],
        }
    return idx


def _load_descr_idx(z: zipfile.ZipFile, edition: str, congress: str) -> Dict[str, Dict[str, str]]:
    idx: Dict[str, Dict[str, str]] = {}
    member = _member(edition, congress, "descr")
    # speech_id|chamber|date|number_within_file|speaker|first_name|last_name|state|gender|line_start|line_end|file|char_count|word_count
    for f in _read_delimited(z, member):
        if len(f) < 14:
            continue
        idx[f[0]] = {
            "chamber": f[1],
            "date": f[2],
            "speaker": f[4],
            "state": f[7],
            "word_count": f[13],
        }
    return idx


def _is_procedural(speaker: str, party: str) -> bool:
    if _PROCEDURAL_RE.match(speaker or ""):
        return True
    return False


def iter_congress_turns(zip_path: Path, edition: str, congress: str) -> Iterator[Dict[str, Any]]:
    """Yield unified turn dicts for one congress from a hein zip."""
    source = "hein_bound" if edition == "bound" else "hein_daily"
    cong_int = int(congress)
    with zipfile.ZipFile(zip_path) as z:
        speakers = _load_speakermap_idx(z, edition, congress)
        descr = _load_descr_idx(z, edition, congress)
        member = _member(edition, congress, "speeches")
        for f in _read_delimited(z, member):
            if len(f) < 2:
                # speech text may itself be empty; keep id with blank text
                if len(f) == 1:
                    sid, text = f[0], ""
                else:
                    continue
            else:
                sid, text = f[0], "|".join(f[1:])  # rejoin any stray delimiters in text
            d = descr.get(sid, {})
            sm = speakers.get(sid, {})
            speaker_name = d.get("speaker") or (
                f"{sm.get('firstname','')} {sm.get('lastname','')}".strip()
            )
            party = normalize_party(sm.get("party"))
            wc = d.get("word_count", "")
            try:
                word_count = int(wc)
            except (TypeError, ValueError):
                word_count = len(text.split())
            yield {
                "turn_id": f"{source}:{sid}",
                "source": source,
                "date": _fmt_date(d.get("date", "")),
                "congress": cong_int,
                "chamber": normalize_chamber(d.get("chamber") or sm.get("chamber")),
                "speaker_name": speaker_name,
                "speaker_id": sm.get("speakerid", ""),
                "bioguide": "",
                "party": party,
                "state": (sm.get("state") or d.get("state") or "").strip(),
                "word_count": word_count,
                "is_procedural": _is_procedural(speaker_name, party),
                "text": text,
            }


def available_congresses(zip_path: Path, edition: str) -> list[str]:
    root = "hein-bound" if edition == "bound" else "hein-daily"
    pat = re.compile(rf"^{root}/speeches_(\d{{3}})\.txt$")
    out = []
    with zipfile.ZipFile(zip_path) as z:
        for n in z.namelist():
            m = pat.match(n)
            if m:
                out.append(m.group(1))
    return sorted(out)


def plan_editions(bound_zip: Optional[Path], daily_zip: Optional[Path]) -> Dict[str, tuple[str, Path]]:
    """Return {congress: (edition, zip_path)} choosing daily>=097, bound otherwise."""
    plan: Dict[str, tuple[str, Path]] = {}
    if bound_zip and bound_zip.exists():
        for c in available_congresses(bound_zip, "bound"):
            if int(c) <= 96:
                plan[c] = ("bound", bound_zip)
    if daily_zip and daily_zip.exists():
        for c in available_congresses(daily_zip, "daily"):
            plan[c] = ("daily", daily_zip)  # daily wins for 097+
    # Fill any 097+ gaps (not in daily) from bound if present.
    if bound_zip and bound_zip.exists():
        for c in available_congresses(bound_zip, "bound"):
            plan.setdefault(c, ("bound", bound_zip))
    return dict(sorted(plan.items()))


def _write_parquet(path: Path, rows: Iterator[Dict[str, Any]], batch_size: int = 50_000) -> int:
    """Stream ``rows`` to a single parquet file in row-group batches. Returns count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    writer: Optional[pq.ParquetWriter] = None
    batch: list[Dict[str, Any]] = []
    total = 0

    def flush() -> None:
        nonlocal writer, batch
        if not batch:
            return
        table = pa.Table.from_pylist(batch, schema=_ARROW_SCHEMA)
        if writer is None:
            writer = pq.ParquetWriter(path, _ARROW_SCHEMA, compression="zstd")
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
        pq.write_table(pa.Table.from_pylist([], schema=_ARROW_SCHEMA), path, compression="zstd")
    return total


_ARROW_SCHEMA = pa.schema(
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


def ingest_hein(
    bound_zip: Optional[Path],
    daily_zip: Optional[Path],
    out_dir: Path,
    congresses: Optional[list[str]] = None,
) -> Dict[str, int]:
    """Ingest hein into ``out_dir/turns/hein_<congress>.parquet``; return counts."""
    plan = plan_editions(bound_zip, daily_zip)
    if congresses:
        want = {c.zfill(3) for c in congresses}
        plan = {c: v for c, v in plan.items() if c in want}
    turns_dir = out_dir / "turns"
    counts: Dict[str, int] = {}
    for congress, (edition, zip_path) in plan.items():
        out_path = turns_dir / f"hein_{congress}.parquet"
        n = _write_parquet(out_path, iter_congress_turns(zip_path, edition, congress))
        counts[congress] = n
        LOG.info("hein congress %s (%s): %d turns -> %s", congress, edition, n, out_path.name)
    return counts
