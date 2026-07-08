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
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

from analysis.ingest.schema import (
    congress_from_year,
    normalize_chamber,
    write_turns_parquet,
)
from analysis.normalize.parties import normalize_party
from crec.metadata import parse_mods

LOG = logging.getLogger("analysis.ingest.govinfo")

# A surname token (allows internal apostrophes/hyphens, e.g. O'BRIEN, RUIZ-... and
# mixed case like McCONNELL); 1-3 space-separated tokens cover multi-word surnames
# like "VAN HOLLEN" or "WASSERMAN SCHULTZ". No '.' so a token can't absorb the
# sentence-ending period and spill into the next word.
_SURNAME = r"[A-Z][A-Za-z'\u2019-]+(?:\s+[A-Z][A-Za-z'\u2019-]+){0,2}"

# Start-of-turn speaker markers at the beginning of a line.
_SPEAKER_RE = re.compile(
    r"(?m)^\s{0,4}("
    rf"(?:Mr|Mrs|Ms|Miss)\.\s+{_SURNAME}(?:\s+of\s+[A-Z][a-zA-Z]+)?"
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
    """Congress number from an ISO date, or 0 if the year can't be parsed."""
    try:
        year = int(date_str[:4])
    except (TypeError, ValueError):
        return 0
    return congress_from_year(year)


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
    """Surname (UPPER) from a matched speaker marker.

    Captures multi-word surnames ("Mr. VAN HOLLEN of Maryland" -> "VAN HOLLEN")
    by taking everything after the honorific up to an ``of <State>`` clause.
    """
    m = re.match(rf"(?:Mr|Mrs|Ms|Miss)\.\s+({_SURNAME})", marker.strip())
    return m.group(1).upper() if m else ""


def _index_members(members: List[Dict[str, str]]) -> Dict[str, Dict[str, str]]:
    """Index normalized members by surname AND by last surname token for lookup.

    Members are dicts with keys ``party``/``bioguide``/``name``/``state``. Indexing
    under both the full surname ("VAN HOLLEN") and its last token ("HOLLEN") lets a
    marker match members whose stored name is first-name-first ("Chris Van Hollen").
    """
    idx: Dict[str, Dict[str, str]] = {}
    for m in members:
        sn = _surname(m.get("name", ""))
        if not sn:
            continue
        idx.setdefault(sn, m)
        last = sn.split()[-1]
        idx.setdefault(last, m)
    return idx


def _match_member(marker: str, index: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """Resolve a speaker marker to a member via full then last-token surname."""
    sn = _speaker_surname(marker)
    if not sn:
        return {}
    return index.get(sn) or index.get(sn.split()[-1], {})


def build_turns(
    text: str,
    members: List[Dict[str, str]],
    gid: str,
    date: str,
    congress: int,
    chamber: str,
) -> Iterator[Dict[str, Any]]:
    """Segment a granule's (header-stripped) text into unified turn dicts.

    Shared by both GovInfo ingest paths (manifest-based and bulk day-zip) so
    segmentation and party attribution stay identical. ``members`` is a list of
    normalized dicts with keys ``party``/``bioguide``/``name``/``state``.
    """
    index = _index_members(members)
    sole = members[0] if len(members) == 1 else None
    for i, (marker, body) in enumerate(_segment(text)):
        if not body:
            continue
        procedural = bool(marker) and bool(_PROCEDURAL_SPEAKER.match(marker))
        info: Dict[str, str] = {}
        if marker and not procedural:
            info = _match_member(marker, index)
        # Attribute to the sole member only for a real (non-empty) marker; never
        # attribute the pre-first-marker preamble (boilerplate) to a member.
        if not info and sole is not None and marker and not procedural:
            info = sole
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


def normalize_members(raw_members: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Normalize raw MODS congMember dicts to the shared member schema.

    Maps the raw MODS attribute keys (``bioGuideId``) to our lowercase keys so both
    ingest paths feed :func:`build_turns` identical member dicts.
    """
    out: List[Dict[str, str]] = []
    for m in raw_members:
        out.append(
            {
                "party": m.get("party", ""),
                "bioguide": m.get("bioGuideId", ""),
                "name": m.get("name", ""),
                "state": m.get("state", ""),
            }
        )
    return out


def _members_from_mods(mods_bytes: bytes) -> List[Dict[str, str]]:
    """Normalized member list from a granule (or package-constituent) MODS blob."""
    return normalize_members(parse_mods(mods_bytes).get("members", []))


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
    text = _strip_header(txt_path.read_text(encoding="utf-8", errors="replace"))
    members = _members_from_mods(mods_path.read_bytes()) if mods_path.exists() else []

    date = (row.get("dateIssued") or "").strip()
    congress = _congress_from_date(date)
    if congress <= 0:  # unparseable date -> skip rather than form a spurious congress-0 group
        return
    chamber = normalize_chamber(row.get("granuleClass") or row.get("chamber"))
    yield from build_turns(text, members, row["granuleId"], date, congress, chamber)


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
        if c <= 0:  # unparseable date -> skip
            continue
        by_congress.setdefault(c, []).append(row)

    turns_dir = out_dir / "turns"
    total = 0
    for congress, rows in sorted(by_congress.items()):
        def gen(rows=rows) -> Iterator[Dict[str, Any]]:
            for r in rows:
                yield from iter_granule_turns(r, data_dir)

        out_path = turns_dir / f"govinfo_{congress:03d}.parquet"
        n = write_turns_parquet(out_path, gen())
        total += n
        LOG.info("GovInfo congress %03d: %d turns -> %s", congress, n, out_path.name)
    return total
