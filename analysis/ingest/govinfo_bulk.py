"""Bulk GovInfo CREC ingest via whole-day package zips (no API rate limit).

Downloads each day's package zip from ``www.govinfo.gov/content/pkg/<pkg>.zip``
(not the rate-limited api.govinfo.gov), parses the package-level ``mods.xml`` for
every granule's class/chamber/party, extracts HTML transcripts or section PDFs for
PDF-only issues, segments them into speaker turns, and writes unified turn parquet
— then deletes the zip to bound disk use. Downloads and ingest both run in the
thread pool; only the per-congress parquet writes are serialized (under a lock).
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

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
_TURN_PACKAGE_PREFIX_LENGTH = len("crec:CREC-YYYY-MM-DD-")
_PAGE_MARKER_RE = re.compile(r"\[\[Page [^\]]+\]\]")
_MIN_DEDUPE_CHARS = 500
_PROBE_RETRY_DELAYS = (15, 30, 60)


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


def _turn_fingerprint(turn: Dict[str, Any]) -> str:
    """Stable fingerprint for long turns duplicated across overlapping granules."""
    text = _PAGE_MARKER_RE.sub(" ", turn.get("text") or "")
    normalized = " ".join(text.split())
    if len(normalized) < _MIN_DEDUPE_CHARS:
        return ""
    identity = "\0".join([
        turn.get("bioguide") or "",
        turn.get("speaker_name") or "",
        turn.get("chamber") or "",
        "1" if turn.get("is_procedural") else "0",
        normalized,
    ])
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


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
    if not htm_names:
        from analysis.ingest.govinfo_pdf import turns_from_pdfs

        LOG.info("%s has no HTML transcripts; ingesting section PDFs", pkg)
        yield from turns_from_pdfs(z, gmap, pkg, date, congress)
        return
    seen_fingerprints: set[str] = set()

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
        for turn in build_turns(
            text, info.get("members", []), gid, date, congress, chamber
        ):
            fingerprint = _turn_fingerprint(turn)
            if fingerprint:
                if fingerprint in seen_fingerprints:
                    continue
                seen_fingerprints.add(fingerprint)
            yield turn


def probe_packages(
    start: str,
    end: str,
    workers: int = 8,
    max_days: int = 400,
    retry_delays: Tuple[float, ...] = _PROBE_RETRY_DELAYS,
) -> List[str]:
    """Return the CREC packages published in ``[start, end]`` without an API key.

    The GovInfo *API* requires a key (and otherwise falls back to ``DEMO_KEY``,
    ~50 requests per day), but the bulk content URLs are public. Since package ids
    are exactly ``CREC-YYYY-MM-DD``, a day's issue can be tested by asking whether
    its zip exists: a published day answers ``200``, a day with no session
    redirects.

    This lets the scheduled update run with no API key and no repository secret.
    Verified against the API over 2026-07-20..2026-07-29: both return the same six
    packages.
    """
    first = dt.date.fromisoformat(start)
    last = dt.date.fromisoformat(end)
    if last < first:
        return []
    span = (last - first).days + 1
    if span > max_days:
        raise ValueError(
            f"refusing to probe {span} days one request at a time; "
            f"narrow the window or enumerate via the API instead"
        )

    days = [first + dt.timedelta(days=i) for i in range(span)]

    def exists(day: dt.date) -> Tuple[str, Optional[str]]:
        pkg = f"CREC-{day.isoformat()}"
        url = CONTENT_URL.format(pkg=pkg)
        try:
            result = subprocess.run(
                ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                 "-I", "--max-time", "30", "--retry", "3", "--retry-all-errors",
                 "--retry-delay", "2", url],
                capture_output=True, text=True, timeout=180, check=True,
            )
        except Exception as exc:  # noqa: BLE001 - treat a probe failure as "unknown"
            LOG.warning("probe failed for %s: %s", pkg, exc)
            return pkg, None
        # Only a direct 200 means the zip is there; anything else (notably the 302
        # to an error page) means no issue was published that day.
        status = result.stdout.strip()
        if status == "200":
            return pkg, pkg
        if status in {"301", "302", "303", "307", "308", "404"}:
            return pkg, ""
        LOG.warning("unexpected HTTP status probing %s: %s", pkg, status or "empty")
        return pkg, None

    found: List[str] = []
    pending = days
    unknown: List[str] = []
    for attempt in range(len(retry_delays) + 1):
        unknown = []
        unknown_days: List[dt.date] = []
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            for day, (pkg, result) in zip(pending, pool.map(exists, pending)):
                if result is None:
                    unknown.append(pkg)
                    unknown_days.append(day)
                elif result:
                    found.append(result)
        if not unknown:
            break
        if attempt >= len(retry_delays):
            break
        delay = retry_delays[attempt]
        LOG.warning(
            "GovInfo probe was inconclusive for %d package(s); retrying in %s seconds",
            len(unknown),
            delay,
        )
        time.sleep(delay)
        pending = unknown_days
    if unknown:
        raise RuntimeError(
            f"could not determine whether {len(unknown)} GovInfo packages exist: "
            + ", ".join(unknown)
        )
    return sorted(found)


def _download(pkg: str, dest: Path) -> Optional[Path]:
    """Download a package zip via curl (uses system CA certs, unlike urllib)."""
    if not _PKG_RE.match(pkg):
        LOG.warning("refusing malformed package id: %r", pkg)
        return None
    url = CONTENT_URL.format(pkg=pkg)
    try:
        subprocess.run(
            ["curl", "-sL", "--retry", "5", "--retry-all-errors",
             "--retry-delay", "2", "-o", str(dest), url],
            check=True, timeout=600,
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
    replaced_pdf_ids: Dict[int, set[str]] = {}
    html_packages: Dict[int, set[str]] = {}
    requested_prefixes = {f"crec:{pkg}-" for pkg in pkg_list}
    lock = threading.Lock()
    total = 0
    package_results: List[Dict[str, Any]] = []

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
            replaced_pdf_ids[congress] = set()
            html_packages[congress] = set()
            if final.exists():
                existing = pq.ParquetFile(final)
                for batch in existing.iter_batches(batch_size=50_000):
                    turn_id_col = batch.schema.get_field_index("turn_id")
                    ids = batch.column(turn_id_col).to_pylist()
                    # PDF turns are provisional: a re-fetch replaces them, and
                    # later HTML must not be appended alongside the same speech.
                    keep = [
                        not (
                            "#pdf-" in tid
                            and tid[:_TURN_PACKAGE_PREFIX_LENGTH] in requested_prefixes
                        )
                        for tid in ids
                    ]
                    retained = batch.filter(pa.array(keep, type=pa.bool_()))
                    w.write_batch(retained)
                    existing_ids.update(tid for tid, retain in zip(ids, keep) if retain)
                    replaced_pdf_ids[congress].update(
                        tid for tid, retain in zip(ids, keep) if not retain
                    )
                    html_packages[congress].update(
                        tid[:_TURN_PACKAGE_PREFIX_LENGTH] for tid in ids
                        if tid.startswith("crec:CREC-") and "#pdf-" not in tid
                    )
            writers[congress] = w
            temp_paths[congress] = tmp
            final_paths[congress] = final
            seen_ids[congress] = existing_ids
        return w

    def process(pkg: str) -> Dict[str, Any]:
        zp = bulk_dir / f"{pkg}.zip"
        if not (zp.exists() and zp.stat().st_size > 0 and zipfile.is_zipfile(zp)):
            if _download(pkg, zp) is None:
                return {"package_id": pkg, "status": "download_failed", "turns": 0}
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
                        if "#pdf-" in turn_id and f"crec:{pkg}-" in html_packages[c]:
                            continue
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
            parsed = sum(len(rows) for rows in rows_by_c.values())
            return {
                "package_id": pkg,
                "status": "ok" if parsed else "empty_or_unparsed",
                "turns": sum(
                    row["turn_id"] not in replaced_pdf_ids[c]
                    for c, rows in fresh_by_c.items() for row in rows
                ),
                "parsed_turns": parsed,
                "pdf_turns": sum(
                    "#pdf-" in row["turn_id"]
                    for rows in rows_by_c.values() for row in rows
                ),
            }
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
                result = fut.result()
                package_results.append(result)
                total += int(result["turns"])
                done += 1
                if done % 50 == 0:
                    LOG.info("processed %d/%d packages, %d new turns", done, len(pkg_list), total)
        failures = [
            result for result in package_results if result["status"] != "ok"
        ]
        if failures:
            raise RuntimeError(
                f"{len(failures)} GovInfo packages failed or produced no parsed turns"
                + ": "
                + ", ".join(
                    f"{result['package_id']} ({result['status']})"
                    for result in failures
                )
            )
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
        coverage_dir = out_dir / "coverage"
        coverage_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        outputs = []
        if succeeded:
            for congress, path in sorted(final_paths.items()):
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                outputs.append({
                    "congress": congress,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "sha256": digest.hexdigest(),
                })
        manifest = {
            "timestamp_utc": stamp,
            "content_url_template": CONTENT_URL,
            "requested_packages": len(pkg_list),
            "successful_packages": sum(
                result["status"] == "ok" for result in package_results
            ),
            "failed_or_empty_packages": [
                result for result in package_results if result["status"] != "ok"
            ],
            "pdf_packages": sorted(
                result["package_id"] for result in package_results
                if result.get("pdf_turns", 0)
            ),
            "new_turns": total,
            "published": succeeded,
            "outputs": outputs,
        }
        manifest_path = coverage_dir / f"govinfo_bulk_run_{stamp}.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        (coverage_dir / "govinfo_bulk_latest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
    LOG.info("bulk ingest complete: %d packages, %d turns", len(pkg_list), total)
    return total


def run_incremental_bulk(
    pkg_list: List[str],
    bulk_dir: Path,
    out_dir: Path,
    *,
    end: str,
    workers: int = 12,
    grace_days: int = 2,
    runner: Optional[Callable[..., int]] = None,
) -> Tuple[int, List[str]]:
    """Run an incremental ingest while deferring only brand-new incomplete packages.

    GovInfo may expose a package URL before its archive contains parseable turns.
    Packages older than ``grace_days`` remain strict and fail the update. Newer
    packages are attempted individually; an incomplete one is deferred until the
    next scheduled run, when it is retried and eventually ages into strict mode.
    """
    runner = runner or run_bulk
    final = dt.date.fromisoformat(end)
    cutoff = final - dt.timedelta(days=max(0, grace_days))
    stable: List[str] = []
    recent: List[str] = []
    for package in sorted(set(pkg_list)):
        match = _PKG_RE.match(package)
        if not match:
            continue
        package_date = dt.date.fromisoformat(package.removeprefix("CREC-"))
        (recent if package_date >= cutoff else stable).append(package)

    total = 0
    if stable:
        total += runner(stable, bulk_dir, out_dir, workers=workers)

    deferred: List[str] = []
    for package in recent:
        try:
            total += runner([package], bulk_dir, out_dir, workers=workers)
        except RuntimeError as exc:
            deferred.append(package)
            LOG.warning(
                "deferring newly published package %s until the next update: %s",
                package,
                exc,
            )
    return total, deferred
