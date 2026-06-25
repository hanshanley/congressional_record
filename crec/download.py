"""Download granule transcript text + MODS metadata and write to disk."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

from bs4 import BeautifulSoup

from .api import GovInfoClient, GovInfoError
from .metadata import parse_mods

LOG = logging.getLogger("crec.download")


def html_to_text(html: str) -> str:
    """Extract the plain-text body of a CREC granule HTML rendition.

    CREC HTML wraps the transcript in a ``<pre>`` block; fall back to full text
    extraction if that structure is missing.
    """
    soup = BeautifulSoup(html, "html.parser")
    pre = soup.find("pre")
    text = pre.get_text() if pre else soup.get_text()
    # Normalize whitespace: strip trailing spaces, collapse >2 blank lines.
    lines = [ln.rstrip() for ln in text.splitlines()]
    text = "\n".join(lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text + "\n"


def granule_paths(out_dir: Path, package_id: str, granule_id: str) -> Dict[str, Path]:
    """Compute output file paths for a granule, partitioned by year/package."""
    year = package_id.split("-")[1] if "-" in package_id else "unknown"
    base = out_dir / "raw" / year / package_id
    return {
        "dir": base,
        "txt": base / f"{granule_id}.txt",
        "mods": base / f"{granule_id}.mods.xml",
    }


def download_granule(
    client: GovInfoClient,
    package_id: str,
    granule: Dict[str, Any],
    out_dir: Path,
    overwrite: bool = False,
) -> Optional[Dict[str, Any]]:
    """Download one granule's text + MODS, write files, return a manifest row.

    Returns ``None`` if the granule was skipped (already present and not
    overwriting). Raises nothing fatal for a single bad granule -- callers should
    treat exceptions as per-granule failures.
    """
    granule_id = granule["granuleId"]
    paths = granule_paths(out_dir, package_id, granule_id)

    if not overwrite and paths["txt"].exists() and paths["mods"].exists():
        return None  # already downloaded

    paths["dir"].mkdir(parents=True, exist_ok=True)

    htm_url = client.url(
        f"/packages/{package_id}/granules/{granule_id}/htm"
    )
    mods_url = client.url(
        f"/packages/{package_id}/granules/{granule_id}/mods"
    )

    html = client.get_text(htm_url)
    mods_bytes = client.get_bytes(mods_url)

    text = html_to_text(html)
    paths["txt"].write_text(text, encoding="utf-8")
    paths["mods"].write_bytes(mods_bytes)

    meta = parse_mods(mods_bytes)
    row: Dict[str, Any] = {
        "granuleId": granule_id,
        "packageId": package_id,
        "granuleClass": granule.get("granuleClass") or meta.get("granuleClass"),
        "title": granule.get("title") or meta.get("title"),
        "dateIssued": granule.get("dateIssued") or meta.get("dateIssued"),
        "congress": meta.get("congress"),
        "session": meta.get("session"),
        "chamber": meta.get("chamber"),
        "citation": meta.get("citation"),
        "member_names": meta.get("member_names", []),
        "bioguide_ids": meta.get("bioguide_ids", []),
        "char_count": len(text),
        "txt_path": str(paths["txt"].relative_to(out_dir)),
        "mods_path": str(paths["mods"].relative_to(out_dir)),
    }
    return row
