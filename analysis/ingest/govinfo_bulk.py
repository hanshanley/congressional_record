"""Bulk GovInfo CREC ingest via whole-day package zips (no API rate limit).

Downloads each day's package zip from ``www.govinfo.gov/content/pkg/<pkg>.zip``
(not the rate-limited api.govinfo.gov), parses the package-level ``mods.xml`` for
every granule's class/chamber/party, extracts each granule's HTML transcript,
segments it into speaker turns, and writes unified turn parquet — then deletes the
zip to bound disk use. Downloads and ingest both run in the thread pool; only the
per-congress parquet writes are serialized (under a lock).
"""

from __future__ import annotations

import concurrent.futures as cf
import io
import logging
import os
import re
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

try:  # defusedxml guards against entity-expansion DoS on remote MODS.
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except Exception:  # pragma: no cover
    from xml.etree.ElementTree import fromstring as _xml_fromstring
from xml.etree import ElementTree as ET

import pyarrow as pa

from analysis.ingest.schema import (
    ARROW_SCHEMA,
    normalize_chamber,
)
from analysis.ingest.govinfo import (
    build_turns,
    normalize_members,
    _congress_from_row,
    _strip_header,
)
from crec.download import html_to_text
from crec.metadata import parse_members

LOG = logging.getLogger("analysis.ingest.govinfo_bulk")

CONTENT_URL = "https://www.govinfo.gov/content/pkg/{pkg}.zip"
# GovInfo CREC package ids are exactly CREC-YYYY-MM-DD; validate before building
# URLs / filesystem paths from them (defense-in-depth).
_PKG_RE = re.compile(r"^CREC-\d{4}-\d{2}-\d{2}$")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _members_of(scope: ET.Element) -> List[Dict[str, str]]:
    """Normalized member list for one constituent's congMember elements.

    Delegates congMember extraction (including the display-name preference
    authority-fnf -> authority-lnf -> parsed) to the shared ``crec.metadata.parse_members``
    so the bulk and manifest ingest paths attribute identical names/parties.
    """
    return normalize_members(parse_members(scope))


def parse_package_mods(mods_bytes: bytes) -> Dict[str, Dict[str, Any]]:
    """Map granuleId -> {granuleClass, chamber, members[]} from a package mods.xml."""
    try:
        root = _xml_fromstring(mods_bytes)
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


def _package_congress(mods_bytes: bytes) -> int:
    """Congress number from package MODS, or zero when absent/invalid."""
    try:
        root = _xml_fromstring(mods_bytes)
    except ET.ParseError:
        return 0
    for node in root.iter():
        if _localname(node.tag) == "congress" and node.text:
            try:
                congress = int(node.text.strip())
            except ValueError:
                continue
            if congress > 0:
                return congress
    return 0


def _turns_from_zip(zip_bytes: bytes, pkg: str) -> Iterator[Dict[str, Any]]:
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = z.namelist()
    mods_name = next((n for n in names if n.endswith("mods.xml")), None)
    if not mods_name:
        return
    mods_bytes = z.read(mods_name)
    gmap = parse_package_mods(mods_bytes)
    date = pkg.replace("CREC-", "")  # YYYY-MM-DD
    congress = _congress_from_row(
        {"congress": _package_congress(mods_bytes), "dateIssued": date}
    )
    if not congress:
        return
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
        chamber = normalize_chamber(info.get("granuleClass") or info.get("chamber"))
        # Same segmentation + attribution as the manifest-based path.
        yield from build_turns(text, info.get("members", []), gid, date, congress, chamber)


def _download(pkg: str, dest: Path) -> Optional[Path]:
    """Download a package zip via curl (uses system CA certs, unlike urllib)."""
    if not _PKG_RE.match(pkg):
        LOG.warning("refusing malformed package id: %r", pkg)
        return None
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
    """Download packages and atomically merge their turns into per-Congress Parquet.

    Existing output is copied into a temporary replacement and retained by ``turn_id``.
    This makes partial/incremental invocations additive instead of truncating prior data.
    The replacements are published only if the run completes without an exception.
    """
    bulk_dir.mkdir(parents=True, exist_ok=True)
    turns_dir = out_dir / "turns"
    turns_dir.mkdir(parents=True, exist_ok=True)
    pkg_list = [p for p in pkg_list if _PKG_RE.match(p)]

    # Temporary Parquet writers per congress (serialized via lock). Existing data is
    # copied into each replacement before newly downloaded turns are appended.
    writers: Dict[int, "pq.ParquetWriter"] = {}
    temp_paths: Dict[int, Path] = {}
    final_paths: Dict[int, Path] = {}
    seen_ids: Dict[int, set[str]] = {}
    lock = threading.Lock()
    total = 0

    import pyarrow.parquet as pq  # local import: only needed for the writer handle

    def get_writer(congress: int) -> "pq.ParquetWriter":
        w = writers.get(congress)
        if w is None:
            final = turns_dir / f"govinfo_bulk_{congress:03d}.parquet"
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".govinfo_bulk_{congress:03d}.", suffix=".parquet.tmp",
                dir=turns_dir,
            )
            os.close(fd)
            tmp = Path(tmp_name)
            w = pq.ParquetWriter(tmp, ARROW_SCHEMA, compression="zstd")
            existing_ids: set[str] = set()
            if final.exists():
                existing = pq.ParquetFile(final)
                for batch in existing.iter_batches(batch_size=50_000):
                    w.write_batch(batch)
                    turn_id_col = batch.schema.get_field_index("turn_id")
                    existing_ids.update(batch.column(turn_id_col).to_pylist())
            writers[congress] = w
            temp_paths[congress] = tmp
            final_paths[congress] = final
            seen_ids[congress] = existing_ids
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
            fresh_by_c: Dict[int, List[Dict[str, Any]]] = {}
            with lock:
                for c, rows in rows_by_c.items():
                    get_writer(c)
                    fresh = []
                    for row in rows:
                        turn_id = row["turn_id"]
                        if turn_id in seen_ids[c]:
                            continue
                        seen_ids[c].add(turn_id)
                        fresh.append(row)
                    if fresh:
                        fresh_by_c[c] = fresh
            # Build Arrow tables outside the lock; hold it only for write_table.
            tables = {
                c: pa.Table.from_pylist(rows, schema=ARROW_SCHEMA)
                for c, rows in fresh_by_c.items()
            }
            with lock:
                for c, table in tables.items():
                    get_writer(c).write_table(table)
            return sum(len(rows) for rows in fresh_by_c.values())
        finally:
            try:
                zp.unlink()  # delete zip to bound disk
            except OSError:
                pass

    succeeded = False
    try:
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(process, p): p for p in pkg_list}
            done = 0
            for fut in cf.as_completed(futs):
                total += fut.result()
                done += 1
                if done % 50 == 0:
                    LOG.info("processed %d/%d packages, %d new turns", done, len(pkg_list), total)
        succeeded = True
    finally:
        with lock:
            for w in writers.values():
                w.close()
        if succeeded:
            for congress, tmp in temp_paths.items():
                os.replace(tmp, final_paths[congress])
        else:
            for tmp in temp_paths.values():
                tmp.unlink(missing_ok=True)
    LOG.info("bulk ingest complete: %d packages, %d turns", len(pkg_list), total)
    return total
