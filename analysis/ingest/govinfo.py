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
import os
import tempfile
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

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
_STATE_NAME = r"[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2}"
_MEMBER_MARKER = (
    rf"(?:Mr|Mrs|Ms|Miss)\.\s+{_SURNAME}(?:\s+of\s+{_STATE_NAME})?"
)
_PROCEDURAL_MARKER = (
    r"The\s+(?:SPEAKER(?:\s+pro\s+tempore)?|PRESIDING\s+OFFICER|"
    r"(?:VICE\s+)?PRESIDENT(?:\s+pro\s+tempore)?|"
    r"ACTING\s+PRESIDENT(?:\s+pro\s+tempore)?|CHIEF\s+JUSTICE|CLERK|"
    r"Acting\s+CHAIR|CHAIR(?:MAN|WOMAN)?)"
    rf"(?:\s+\({_MEMBER_MARKER}\))?"
)

# Start-of-turn speaker markers at the beginning of a line.
_SPEAKER_RE = re.compile(
    rf"(?m)^[ \t]*({_MEMBER_MARKER}|{_PROCEDURAL_MARKER})\.\s",
)
_PROCEDURAL_SPEAKER = re.compile(r"^(the\s+)?(speaker|presiding|president|acting|chief|clerk|chair)", re.IGNORECASE)

# Standard Record formulas that introduce printed bills, amendments, exhibits, or
# other material that was inserted into the Record rather than spoken on the floor.
_INSERTED_MATERIAL_RE = re.compile(
    r"(?im)^[ \t]*(?:"
    r"The Clerk (?:read|reported)\b"
    r"|There being no objection,[\s\S]{0,300}?\bordered\s+to\s+be\s+printed\s+in\s+the\s+Record\b"
    r"|Pursuant to the adoption of House Resolution\b"
    r"|The text of\b[\s\S]{0,300}?\bis as follows:"
    r")"
)

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


def _congress_from_row(row: Dict[str, Any]) -> int:
    """Prefer explicit GovInfo Congress metadata; infer from date only as fallback."""
    try:
        congress = int(row.get("congress") or 0)
    except (TypeError, ValueError):
        congress = 0
    if congress > 0:
        return congress
    return _congress_from_date((row.get("dateIssued") or "").strip())


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


_STATE_CODES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE",
    "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM", "NEW YORK": "NY", "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK", "OREGON": "OR",
    "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
    "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "DISTRICT OF COLUMBIA": "DC", "PUERTO RICO": "PR", "GUAM": "GU",
    "AMERICAN SAMOA": "AS", "VIRGIN ISLANDS": "VI",
    "NORTHERN MARIANA ISLANDS": "MP",
}


def _speaker_state(marker: str) -> str:
    """Two-letter state/territory code from an ``of <State>`` marker clause."""
    m = re.search(r"\s+of\s+(.+)$", marker.strip(), re.IGNORECASE)
    if not m:
        return ""
    raw = " ".join(m.group(1).split()).upper()
    return _STATE_CODES.get(raw, raw if len(raw) == 2 else "")


def _index_members(members: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """Index members by surname and last surname token without discarding ambiguity.

    Members are dicts with keys ``party``/``bioguide``/``name``/``state``. Indexing
    under both the full surname ("VAN HOLLEN") and its last token ("HOLLEN") lets a
    marker match members whose stored name is first-name-first ("Chris Van Hollen").
    """
    idx: Dict[str, List[Dict[str, str]]] = {}
    for m in members:
        sn = _surname(m.get("name", ""))
        if not sn:
            continue
        idx.setdefault(sn, []).append(m)
        last = sn.split()[-1]
        if last != sn:
            idx.setdefault(last, []).append(m)
    return idx


def _match_member(marker: str, index: Dict[str, List[Dict[str, str]]]) -> Dict[str, str]:
    """Resolve a marker by surname plus state; reject ambiguous surname-only matches."""
    sn = _speaker_surname(marker)
    if not sn:
        return {}
    candidates = index.get(sn) or index.get(sn.split()[-1], [])
    state = _speaker_state(marker)
    if state:
        candidates = [m for m in candidates if (m.get("state") or "").upper() == state]
    # De-duplicate a member indexed under both full and last-token surname.
    unique = {
        (m.get("bioguide") or m.get("name") or str(id(m))): m for m in candidates
    }
    return next(iter(unique.values())) if len(unique) == 1 else {}


def _split_inserted_material(body: str) -> Tuple[str, str]:
    """Split spoken remarks from standardized material printed into the Record."""
    match = _INSERTED_MATERIAL_RE.search(body)
    if not match:
        return body, ""
    return body[:match.start()].rstrip(), body[match.start():].lstrip()


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
    for i, (marker, body) in enumerate(_segment(text)):
        if not body:
            continue
        procedural = bool(marker) and bool(_PROCEDURAL_SPEAKER.match(marker))
        inserted = ""
        if not procedural:
            body, inserted = _split_inserted_material(body)
        info: Dict[str, str] = {}
        if marker and not procedural:
            info = _match_member(marker, index)
        if body:
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
        if inserted:
            yield {
                "turn_id": f"crec:{gid}#{i}-inserted",
                "source": "govinfo",
                "date": date,
                "congress": congress,
                "chamber": chamber,
                "speaker_name": "Inserted material",
                "speaker_id": "",
                "bioguide": "",
                "party": "other",
                "state": "",
                "word_count": len(inserted.split()),
                "is_procedural": True,
                "text": inserted,
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
    congress = _congress_from_row(row)
    if congress <= 0:  # unparseable date -> skip rather than form a spurious congress-0 group
        return
    chamber = normalize_chamber(row.get("granuleClass") or row.get("chamber"))
    yield from build_turns(text, members, row["granuleId"], date, congress, chamber)


def _iter_manifest(
    manifest_path: Path, stats: Optional[Dict[str, int]] = None
) -> Iterator[Dict[str, Any]]:
    with manifest_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                if stats is not None:
                    stats["malformed_json"] = stats.get("malformed_json", 0) + 1
                continue


def ingest_govinfo(
    manifest_path: Path,
    data_dir: Path,
    out_dir: Path,
    max_rejected_rows: int = 0,
) -> int:
    """Ingest GovInfo turns, partitioned by congress, into out_dir/turns/govinfo_<congress>.parquet."""
    if not manifest_path.exists():
        LOG.warning("no manifest at %s; skipping GovInfo ingest", manifest_path)
        return 0

    # Group by explicit GovInfo Congress metadata, with date inference only as fallback.
    by_congress: Dict[int, List[Dict[str, Any]]] = {}
    stats: Dict[str, int] = {"manifest_rows": 0, "malformed_json": 0, "invalid_rows": 0}
    for row in _iter_manifest(manifest_path, stats):
        stats["manifest_rows"] += 1
        if not row.get("granuleId") or not row.get("txt_path"):
            stats["invalid_rows"] += 1
            continue
        c = _congress_from_row(row)
        if c <= 0:  # unparseable date -> skip
            stats["invalid_rows"] += 1
            continue
        if not (data_dir / row["txt_path"]).exists():
            stats["invalid_rows"] += 1
            continue
        by_congress.setdefault(c, []).append(row)

    rejected = stats["malformed_json"] + stats["invalid_rows"]
    if rejected > max_rejected_rows:
        raise ValueError(
            f"GovInfo manifest rejected {rejected} rows (limit {max_rejected_rows})"
        )

    turns_dir = out_dir / "turns"
    total = 0
    for congress, rows in sorted(by_congress.items()):
        def gen(rows=rows) -> Iterator[Dict[str, Any]]:
            for r in rows:
                yield from iter_granule_turns(r, data_dir)

        turns_dir.mkdir(parents=True, exist_ok=True)
        out_path = turns_dir / f"govinfo_{congress:03d}.parquet"
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".govinfo_{congress:03d}.", suffix=".parquet.tmp", dir=turns_dir
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            n = write_turns_parquet(tmp_path, gen())
            if n <= 0 and rows:
                raise ValueError(f"GovInfo congress {congress} produced no turns")
            os.replace(tmp_path, out_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        total += n
        LOG.info("GovInfo congress %03d: %d turns -> %s", congress, n, out_path.name)
    coverage_dir = out_dir / "coverage"
    coverage_dir.mkdir(parents=True, exist_ok=True)
    stats["turns_written"] = total
    (coverage_dir / "govinfo_manifest_ingest.json").write_text(
        json.dumps(stats, indent=2) + "\n", encoding="utf-8"
    )
    return total
