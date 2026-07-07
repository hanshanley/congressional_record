"""Bulk GovInfo CREC ingest via whole-day package zips (no API rate limit).

Downloads each day's package zip from ``www.govinfo.gov/content/pkg/<pkg>.zip``
(not the rate-limited api.govinfo.gov), parses the package-level ``mods.xml`` for
every granule's class/chamber/party, extracts each granule's HTML transcript,
segments it into speaker turns, and writes unified turn parquet — then deletes the
zip to bound disk use. Downloads run in a thread pool; ingest is serialized.
"""

from __future__ import annotations

import concurrent.futures as cf
import io
import logging
import os
import subprocess
import threading
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from xml.etree import ElementTree as ET

import pyarrow as pa
import pyarrow.parquet as pq

from analysis.ingest.hein import _ARROW_SCHEMA
from analysis.ingest.schema import normalize_chamber
from analysis.ingest.govinfo import (
    _PROCEDURAL_SPEAKER,
    _congress_from_date,
    _segment,
    _speaker_surname,
    _strip_header,
    _surname,
)
from analysis.normalize.parties import normalize_party
from crec.download import html_to_text

LOG = logging.getLogger("analysis.ingest.govinfo_bulk")

CONTENT_URL = "https://www.govinfo.gov/content/pkg/{pkg}.zip"


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _members_of(scope: ET.Element) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for node in scope.iter():
        if _localname(node.tag) != "congMember":
            continue
        m = dict(node.attrib)
        names: Dict[str, str] = {}
        for nm in node.iter():
            if _localname(nm.tag) == "name" and nm.text and nm.text.strip():
                names[nm.attrib.get("type", "")] = nm.text.strip()
        m["name"] = names.get("authority-fnf") or names.get("parsed") or ""
        out.append(m)
    return out


def parse_package_mods(mods_bytes: bytes) -> Dict[str, Dict[str, Any]]:
    """Map granuleId -> {granuleClass, chamber, members[]} from a package mods.xml."""
    try:
        root = ET.fromstring(mods_bytes)
    except ET.ParseError as exc:
        LOG.warning("package mods parse error: %s", exc)
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for node in root.iter():
        if _localname(node.tag) != "relatedItem":
            continue
        if node.attrib.get("type") != "constituent":
            continue
        rid = node.attrib.get("ID", "")
        gid = rid[3:] if rid.startswith("id-") else rid
        if not gid:
            continue
        gclass = chamber = None
        for c in node.iter():
            ln = _localname(c.tag)
            if ln == "granuleClass" and c.text:
                gclass = c.text.strip()
            elif ln == "chamber" and c.text:
                chamber = c.text.strip()
        out[gid] = {
            "granuleClass": gclass,
            "chamber": chamber,
            "members": _members_of(node),
        }
    return out


def _turns_from_zip(zip_bytes: bytes, pkg: str) -> Iterator[Dict[str, Any]]:
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = z.namelist()
    mods_name = next((n for n in names if n.endswith("mods.xml")), None)
    if not mods_name:
        return
    gmap = parse_package_mods(z.read(mods_name))
    date = pkg.replace("CREC-", "")  # YYYY-MM-DD
    congress = _congress_from_date(date)
    htm_names = {n.rsplit("/", 1)[-1][:-4]: n for n in names if n.endswith(".htm")}

    for gid, info in gmap.items():
        htm = htm_names.get(gid)
        if not htm:
            continue
        try:
            html = z.read(htm).decode("utf-8", "replace")
        except KeyError:
            continue
        text = _strip_header(html_to_text(html))
        if not text:
            continue
        members = info.get("members", [])
        by_surname = {}
        for m in members:
            sn = _surname(m.get("name", ""))
            if sn:
                by_surname[sn] = m
        sole = members[0] if len(members) == 1 else None
        chamber = normalize_chamber(info.get("granuleClass") or info.get("chamber"))

        for i, (marker, body) in enumerate(_segment(text)):
            if not body:
                continue
            procedural = bool(marker) and bool(_PROCEDURAL_SPEAKER.match(marker))
            m = {}
            if marker and not procedural:
                m = by_surname.get(_speaker_surname(marker), {})
            if not m and sole is not None and not procedural:
                m = sole
            party = normalize_party(m.get("party")) if m else "other"
            yield {
                "turn_id": f"crec:{gid}#{i}",
                "source": "govinfo",
                "date": date,
                "congress": congress,
                "chamber": chamber,
                "speaker_name": marker or m.get("name", ""),
                "speaker_id": "",
                "bioguide": m.get("bioGuideId", ""),
                "party": party,
                "state": m.get("state", ""),
                "word_count": len(body.split()),
                "is_procedural": procedural,
                "text": body,
            }


def _download(pkg: str, dest: Path) -> Optional[Path]:
    """Download a package zip via curl (uses system CA certs, unlike urllib)."""
    url = CONTENT_URL.format(pkg=pkg)
    try:
        subprocess.run(
            ["curl", "-sL", "--retry", "5", "--retry-delay", "2", "-o", str(dest), url],
            check=True, timeout=180,
        )
        if not dest.exists() or dest.stat().st_size == 0 or not zipfile.is_zipfile(dest):
            if dest.exists():
                dest.unlink()
            return None
        return dest
    except Exception as exc:  # noqa: BLE001
        LOG.warning("download failed %s: %s", pkg, exc)
        if dest.exists():
            dest.unlink()
        return None


def run_bulk(
    pkg_list: List[str],
    bulk_dir: Path,
    out_dir: Path,
    workers: int = 12,
) -> int:
    """Download+ingest+delete each package; write turns per congress. Returns turn count."""
    bulk_dir.mkdir(parents=True, exist_ok=True)
    turns_dir = out_dir / "turns"
    turns_dir.mkdir(parents=True, exist_ok=True)

    # Parquet writers per congress (serialized via lock).
    writers: Dict[int, pq.ParquetWriter] = {}
    lock = threading.Lock()
    total = 0

    def get_writer(congress: int) -> pq.ParquetWriter:
        w = writers.get(congress)
        if w is None:
            p = turns_dir / f"govinfo_bulk_{congress:03d}.parquet"
            w = pq.ParquetWriter(p, _ARROW_SCHEMA, compression="zstd")
            writers[congress] = w
        return w

    def process(pkg: str) -> int:
        zp = bulk_dir / f"{pkg}.zip"
        if not (zp.exists() and zp.stat().st_size > 0 and zipfile.is_zipfile(zp)):
            if _download(pkg, zp) is None:
                return 0
        try:
            data = zp.read_bytes()
            rows_by_c: Dict[int, List[Dict[str, Any]]] = {}
            for t in _turns_from_zip(data, pkg):
                rows_by_c.setdefault(t["congress"], []).append(t)
            n = 0
            with lock:
                for c, rows in rows_by_c.items():
                    if not rows:
                        continue
                    get_writer(c).write_table(pa.Table.from_pylist(rows, schema=_ARROW_SCHEMA))
                    n += len(rows)
            return n
        finally:
            try:
                zp.unlink()  # delete zip to bound disk
            except OSError:
                pass

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process, p): p for p in pkg_list}
        done = 0
        for fut in cf.as_completed(futs):
            total += fut.result()
            done += 1
            if done % 50 == 0:
                LOG.info("processed %d/%d packages, %d turns", done, len(pkg_list), total)

    with lock:
        for w in writers.values():
            w.close()
    LOG.info("bulk ingest complete: %d packages, %d turns", len(pkg_list), total)
    return total
