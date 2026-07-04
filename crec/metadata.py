"""Parse GovInfo MODS XML for a CREC granule into a flat metadata dict."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional
from xml.etree import ElementTree as ET

try:  # defusedxml guards against entity-expansion ("billion laughs") DoS on remote XML.
    from defusedxml.ElementTree import fromstring as _safe_fromstring
except Exception:  # pragma: no cover - fallback if defusedxml is unavailable
    _safe_fromstring = ET.fromstring

LOG = logging.getLogger("crec.metadata")

MODS_NS = "http://www.loc.gov/mods/v3"


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_texts(elem: ET.Element, wanted: Iterable[str]) -> Dict[str, str]:
    """Single-pass collection of the first non-empty text for each wanted local name.

    Traverses ``elem`` exactly once instead of once per field, returning a map of
    ``localname -> first non-empty stripped text``.
    """
    want = set(wanted)
    found: Dict[str, str] = {}
    for node in elem.iter():
        if not want:
            break
        name = _localname(node.tag)
        if name in want and node.text and node.text.strip():
            found[name] = node.text.strip()
            want.discard(name)
    return found


def _granule_part(root: ET.Element) -> Optional[ET.Element]:
    """Return the <mods:relatedItem type="constituent"> for this granule, if any.

    CREC MODS files describe the whole daily issue plus a constituent
    ``relatedItem`` per granule. The constituent that carries a ``<granuleClass>``
    holds the granule-specific fields we care about.
    """
    for node in root.iter():
        if _localname(node.tag) == "relatedItem":
            for child in node.iter():
                if _localname(child.tag) == "granuleClass":
                    return node
    return None


def parse_members(scope: ET.Element) -> List[Dict[str, str]]:
    """Extract congMember entries (speakers/voters) from a MODS scope element."""
    members: List[Dict[str, str]] = []
    for node in scope.iter():
        if _localname(node.tag) != "congMember":
            continue
        # bioGuideId, chamber, congress, party, role, state
        member: Dict[str, str] = dict(node.attrib)
        # Preferred display name: first-name-first authority form.
        names = {}
        for name in node.iter():
            if _localname(name.tag) == "name":
                ntype = name.attrib.get("type", "")
                if name.text and name.text.strip():
                    names[ntype] = name.text.strip()
        member["name"] = (
            names.get("authority-fnf")
            or names.get("authority-lnf")
            or names.get("parsed")
            or ""
        )
        members.append(member)
    return members


def parse_mods(xml_bytes: bytes) -> Dict[str, Any]:
    """Parse MODS XML bytes into a flat, JSON-serializable metadata dict."""
    meta: Dict[str, Any] = {}
    try:
        root = _safe_fromstring(xml_bytes)
    except ET.ParseError as exc:
        LOG.warning("MODS parse error: %s", exc)
        return {"mods_parse_error": str(exc)}

    granule = _granule_part(root) or root

    # Granule-scoped fields in one traversal.
    g = _first_texts(
        granule,
        ("granuleClass", "subGranuleClass", "searchTitle", "title", "pagePrefix", "chamber"),
    )
    meta["granuleClass"] = g.get("granuleClass")
    meta["subGranuleClass"] = g.get("subGranuleClass")
    meta["title"] = g.get("searchTitle") or g.get("title")
    meta["pagePrefix"] = g.get("pagePrefix")
    meta["chamber"] = g.get("chamber")

    # Root-scoped issue-level fields in one traversal.
    r = _first_texts(root, ("congress", "session", "volume", "issue", "dateIssued"))
    meta["congress"] = r.get("congress")
    meta["session"] = r.get("session")
    meta["volume"] = r.get("volume")
    meta["issue"] = r.get("issue")
    meta["dateIssued"] = r.get("dateIssued")

    # Congressional Record citation (e.g. "170 Cong. Rec. H5").
    for node in granule.iter():
        if _localname(node.tag) == "identifier" and (
            node.attrib.get("type") == "congressional record citation"
        ):
            if node.text and node.text.strip():
                meta["citation"] = node.text.strip()
            break

    members = parse_members(granule)
    meta["members"] = members
    meta["member_names"] = sorted({m["name"] for m in members if m.get("name")})
    meta["bioguide_ids"] = sorted(
        {m["bioGuideId"] for m in members if m.get("bioGuideId")}
    )
    return meta

