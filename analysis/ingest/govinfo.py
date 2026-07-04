"""Ingest downloaded GovInfo CREC granules into unified turn parquet.

Unlike hein (already speaker-segmented), a CREC granule is a page-section that may
contain several speaker turns. We:

1. read the manifest row + the saved ``.txt`` transcript and ``.mods.xml``,
2. parse MODS for the granule's members (name + party + bioguide),
3. split the transcript at speaker markers ("Mr. SMITH.", "Ms. PELOSI of California.",
   "The SPEAKER pro tempore.") into turns,
4. attribute each turn's party by matching the speaker surname to a granule member.

Segmentation is heuristic; procedural/chair turns are flagged and get party "other".
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

# Reuse the hein parquet writer + schema and the crec MODS parser.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from analysis.ingest.hein import _ARROW_SCHEMA, _write_parquet  # noqa: E402
from analysis.ingest.schema import normalize_chamber  # noqa: E402
from analysis.normalize.parties import normalize_party  # noqa: E402
from crec.metadata import parse_mods  # noqa: E402

LOG = logging.getLogger("analysis.ingest.govinfo")

# Start-of-turn speaker markers at the beginning of a line.
_SPEAKER_RE = re.compile(
    r"(?m)^\s{0,4}("
    r"(?:Mr|Mrs|Ms|Miss)\.\s+[A-Z][A-Za-z.'\u2019-]+(?:\s+of\s+[A-Z][a-zA-Z]+)?"
    r"|The\s+(?:SPEAKER(?:\s+pro\s+tempore)?|PRESIDING\s+OFFICER|(?:VICE\s+)?PRESIDENT"
    r"|ACTING\s+PRESIDENT(?:\s+pro\s+tempore)?|CHIEF\s+JUSTICE|CLERK|Acting\s+CHAIR|CHAIR(?:MAN|WOMAN)?)"
    r")\.\s",
)
_PROCEDURAL_SPEAKER = re.compile(r"^(the\s+)?(speaker|presiding|president|acting|chief|clerk|chair)", re.IGNORECASE)

# Standard CREC transcript header lines to drop before segmenting.
_HEADER_LINE = re.compile(
    r"^\s*(\[(Congressional Record|House|Senate|Extensions? of Remarks|Pages?|Page)\b.*\]"
    r"|From the Congressional Record Online.*)\s*$",
    re.IGNORECASE,
)


def _congress_from_date(date_str: str) -> int:
    """Derive the Congress number from an ISO date (reliable for CREC, unlike MODS).

    Congress N runs [1789+2(N-1), +~2yrs]. Good to the year; the Jan-1/2 edge of a
    new congress is negligible for aggregation.
    """
    try:
        year = int(date_str[:4])
    except (TypeError, ValueError):
        return 0
    return (year - 1789) // 2 + 1


def _strip_header(text: str) -> str:
    lines = text.splitlines()
    i = 0
    # Drop leading header/boilerplate lines and blank lines up to the first real content.
    while i < len(lines) and (not lines[i].strip() or _HEADER_LINE.match(lines[i])):
        i += 1
    return "\n".join(lines[i:]).strip()


def _surname(name: str) -> str:
    """Best-effort surname (UPPER) from a member display name."""
    name = (name or "").strip()
    if "," in name:  # "Smith, Jane Q."
        return name.split(",", 1)[0].strip().upper()
    parts = name.split()
    return parts[-1].strip().upper() if parts else ""


def _speaker_surname(marker: str) -> str:
    """Surname (UPPER) from a matched speaker marker like 'Mr. SMITH of Texas'."""
    m = re.match(r"(?:Mr|Mrs|Ms|Miss)\.\s+([A-Z][A-Za-z.'\u2019-]+)", marker.strip())
    return m.group(1).upper() if m else ""


def _members_index(mods_bytes: bytes) -> Dict[str, Dict[str, str]]:
    """surname(UPPER) -> {party, bioguide, name, state} from MODS congMembers."""
    meta = parse_mods(mods_bytes)
    idx: Dict[str, Dict[str, str]] = {}
    for m in meta.get("members", []):
        sn = _surname(m.get("name", ""))
        if sn:
            idx[sn] = {
                "party": m.get("party", ""),
                "bioguide": m.get("bioGuideId", ""),
                "name": m.get("name", ""),
                "state": m.get("state", ""),
            }
    return idx


def _segment(text: str) -> List[Tuple[str, str]]:
    """Split transcript into (speaker_marker, body) turns.

    Any preamble before the first marker is returned with an empty speaker.
    """
    marks = list(_SPEAKER_RE.finditer(text))
    if not marks:
        return [("", text.strip())]
    segments: List[Tuple[str, str]] = []
    pre = text[: marks[0].start()].strip()
    if pre:
        segments.append(("", pre))
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[start:end].strip()
        segments.append((m.group(1).strip(), body))
    return segments


def iter_granule_turns(row: Dict[str, Any], data_dir: Path) -> Iterator[Dict[str, Any]]:
    txt_path = data_dir / row["txt_path"]
    mods_path = data_dir / row["mods_path"]
    if not txt_path.exists():
        return
    text = txt_path.read_text(encoding="utf-8", errors="replace")
    text = _strip_header(text)
    members = _members_index(mods_path.read_bytes()) if mods_path.exists() else {}
    sole = next(iter(members.values())) if len(members) == 1 else None

    date = (row.get("dateIssued") or "").strip()
    congress = _congress_from_date(date)
    chamber = normalize_chamber(row.get("granuleClass") or row.get("chamber"))
    gid = row["granuleId"]

    for i, (marker, body) in enumerate(_segment(text)):
        if not body:
            continue
        procedural = bool(marker) and bool(_PROCEDURAL_SPEAKER.match(marker))
        info: Dict[str, str] = {}
        if marker and not procedural:
            info = members.get(_speaker_surname(marker), {})
        if not info and sole is not None and not procedural:
            info = sole  # single-member granule: attribute to that member
        party = normalize_party(info.get("party")) if info else "other"
        yield {
            "turn_id": f"crec:{gid}#{i}",
            "source": "govinfo",
            "date": date,
            "congress": congress,
            "chamber": chamber,
            "speaker_name": marker or info.get("name", ""),
            "speaker_id": "",
            "bioguide": info.get("bioguide", ""),
            "party": party,
            "state": info.get("state", ""),
            "word_count": len(body.split()),
            "is_procedural": procedural,
            "text": body,
        }


def _iter_manifest(manifest_path: Path) -> Iterator[Dict[str, Any]]:
    with manifest_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def ingest_govinfo(manifest_path: Path, data_dir: Path, out_dir: Path) -> int:
    """Ingest GovInfo turns, partitioned by congress, into out_dir/turns/govinfo_<congress>.parquet."""
    if not manifest_path.exists():
        LOG.warning("no manifest at %s; skipping GovInfo ingest", manifest_path)
        return 0

    # Group manifest rows by congress (derived from date) so each parquet is one congress.
    by_congress: Dict[int, List[Dict[str, Any]]] = {}
    for row in _iter_manifest(manifest_path):
        c = _congress_from_date((row.get("dateIssued") or "").strip())
        by_congress.setdefault(c, []).append(row)

    turns_dir = out_dir / "turns"
    total = 0
    for congress, rows in sorted(by_congress.items()):
        def gen() -> Iterator[Dict[str, Any]]:
            for r in rows:
                yield from iter_granule_turns(r, data_dir)

        out_path = turns_dir / f"govinfo_{congress:03d}.parquet"
        n = _write_parquet(out_path, gen())
        total += n
        LOG.info("GovInfo congress %03d: %d turns -> %s", congress, n, out_path.name)
    return total
