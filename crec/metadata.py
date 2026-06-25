"""Parse GovInfo MODS XML for a CREC granule into a flat metadata dict."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

LOG = logging.getLogger("crec.metadata")

MODS_NS = "http://www.loc.gov/mods/v3"


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(elem: ET.Element, localname: str) -> Optional[str]:
    """Depth-first search for the first descendant with the given local name."""
    for node in elem.iter():
        if _localname(node.tag) == localname and node.text and node.text.strip():
            return node.text.strip()
    return None


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
        member: Dict[str, str] = {
            k: v for k, v in node.attrib.items()
        }  # bioGuideId, chamber, congress, party, role, state
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
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        LOG.warning("MODS parse error: %s", exc)
        return {"mods_parse_error": str(exc)}

    granule = _granule_part(root) or root

    meta["granuleClass"] = _find_text(granule, "granuleClass")
    meta["subGranuleClass"] = _find_text(granule, "subGranuleClass")
    meta["title"] = _find_text(granule, "searchTitle") or _find_text(granule, "title")
    meta["congress"] = _find_text(root, "congress")
    meta["session"] = _find_text(root, "session")
    meta["volume"] = _find_text(root, "volume")
    meta["issue"] = _find_text(root, "issue")
    meta["dateIssued"] = _find_text(root, "dateIssued")
    meta["pagePrefix"] = _find_text(granule, "pagePrefix")
    meta["chamber"] = _find_text(granule, "chamber")

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
