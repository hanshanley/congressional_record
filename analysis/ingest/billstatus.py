"""GovInfo Bill Status bulk-data adapter.

The public ``/bulkdata/BILLSTATUS`` tree requires no API key. Directory
listings expose each bill XML file and its modification time for incremental
current-Congress updates, while official bill-type ZIPs make full backfills
efficient.
"""

from __future__ import annotations

import concurrent.futures as futures
import datetime as dt
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence
from urllib.parse import quote, urljoin
from xml.etree.ElementTree import Element, ParseError

import pandas as pd
import requests
from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from analysis.bills import (
    BILL_COLUMNS,
    bill_id,
    canonical_bill,
    empty_bills,
    load_bills,
    merge_bills,
    normalize_bills,
    save_bills,
)

LOG = logging.getLogger("analysis.ingest.billstatus")

BASE_URL = "https://www.govinfo.gov/bulkdata/BILLSTATUS"
SOURCE = "govinfo"
MIN_CONGRESS = 108
SUPPORTED_TYPES = ("HR", "S")
_TYPE_PATHS = {"HR": "hr", "S": "s"}
_LISTING_TIME_FORMAT = "%d-%b-%Y %H:%M"
_FILE_RE = re.compile(r"^BILLSTATUS-(?P<congress>\d+)(?P<type>hr|s)(?P<number>\d+)\.xml$")
_MAX_ZIP_BYTES = 512 * 1024 * 1024
_MAX_ZIP_MEMBERS = 100_000
_MAX_ZIP_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_XML_BYTES = 25 * 1024 * 1024


class BillStatusError(RuntimeError):
    """Base error for Bill Status discovery, download, and parsing failures."""


class RetryableBillStatusError(BillStatusError):
    """Transient GovInfo failure that tenacity may retry."""


class GovInfoNotFoundError(BillStatusError):
    """A requested GovInfo bulk resource does not exist."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"GovInfo returned HTTP 404 for {url}")


@dataclass(frozen=True, order=True)
class BillStatusFile:
    """One XML record advertised by a GovInfo Bill Status directory."""

    congress: int
    bill_type: str
    bill_number: int
    url: str
    modified_at: str

    @property
    def bill_id(self) -> str:
        return bill_id(self.congress, self.bill_type, self.bill_number)


@dataclass(frozen=True)
class BillStatusUpdate:
    """Summary of one atomic discovery/fetch/merge/save operation."""

    discovered: int
    selected: int
    fetched: int
    written: tuple[Path, ...]
    bills: pd.DataFrame


def validate_congress(congress: int) -> int:
    """Validate a Congress supported by the official bulk collection."""
    try:
        value = int(congress)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Congress: {congress!r}") from exc
    if value < MIN_CONGRESS:
        raise ValueError(f"GovInfo Bill Status begins with Congress {MIN_CONGRESS}")
    return value


def validate_bill_type(bill_type: str) -> str:
    """Return the canonical bill type, rejecting non-H.R./S. measures."""
    value = str(bill_type).strip().upper()
    if value not in SUPPORTED_TYPES:
        raise ValueError(f"unsupported Bill Status type: {bill_type!r}")
    return value


def _localname(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _child(parent: Optional[Element], name: str) -> Optional[Element]:
    if parent is None:
        return None
    return next((node for node in parent if _localname(node.tag) == name), None)


def _children(parent: Optional[Element], name: str) -> list[Element]:
    if parent is None:
        return []
    return [node for node in parent if _localname(node.tag) == name]


def _text(parent: Optional[Element], name: str) -> str:
    node = _child(parent, name)
    return "" if node is None or node.text is None else node.text.strip()


def _text_any(parent: Optional[Element], *names: str) -> str:
    """Return the first populated direct child across schema-version aliases."""
    for name in names:
        value = _text(parent, name)
        if value:
            return value
    return ""


def _parse_xml(data: bytes, description: str) -> Element:
    try:
        return DefusedET.fromstring(data)
    except (ParseError, DefusedXmlException, ValueError) as exc:
        raise BillStatusError(f"malformed or unsafe XML in {description}: {exc}") from exc


def _normalize_listing_time(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise BillStatusError("Bill Status listing entry has no modification timestamp")
    try:
        parsed = dt.datetime.strptime(raw, _LISTING_TIME_FORMAT)
    except ValueError:
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BillStatusError(
                f"invalid Bill Status modification timestamp: {raw!r}"
            ) from exc
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


class GovInfoBulkClient:
    """Bounded, retrying HTTP client for the public GovInfo bulk host."""

    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        timeout: tuple[float, float] = (10.0, 60.0),
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent", "congressional-record-billstatus/1.0"
        )
        # requests supplies ``Accept: */*`` by default, but GovInfo answers that
        # with HTML. The explicit XML accept header is required for listings.
        self.session.headers["Accept"] = "application/xml"
        self.timeout = timeout

    @retry(
        retry=retry_if_exception_type(
            (RetryableBillStatusError, requests.RequestException)
        ),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        stop=stop_after_attempt(4),
        before_sleep=before_sleep_log(LOG, logging.WARNING),
        reraise=True,
    )
    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        response = self.session.get(url, timeout=self.timeout)
        if response.status_code == 404:
            raise GovInfoNotFoundError(url)
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableBillStatusError(
                f"GovInfo returned HTTP {response.status_code} for {url}"
            )
        if response.status_code >= 400:
            raise BillStatusError(
                f"GovInfo returned HTTP {response.status_code} for {url}"
            )
        declared = response.headers.get("Content-Length")
        if declared:
            try:
                if int(declared) > max_bytes:
                    raise BillStatusError(f"GovInfo response too large for {url}")
            except ValueError:
                pass
        content = response.content
        if len(content) > max_bytes:
            raise BillStatusError(f"GovInfo response too large for {url}")
        return content


def parse_directory_listing(
    data: bytes,
    *,
    congress: int,
    bill_type: str,
    listing_url: str,
) -> list[BillStatusFile]:
    """Parse one official GovInfo directory listing, including modification times."""
    congress = validate_congress(congress)
    bill_type = validate_bill_type(bill_type)
    expected_path_type = _TYPE_PATHS[bill_type]
    root = _parse_xml(data, listing_url)
    records: list[BillStatusFile] = []

    for node in root.iter():
        if _localname(node.tag) != "file" or _text(node, "folder").lower() == "true":
            continue
        name = _text(node, "name")
        match = _FILE_RE.fullmatch(name)
        if not match:
            continue
        if (
            int(match.group("congress")) != congress
            or match.group("type") != expected_path_type
        ):
            continue
        number = int(match.group("number"))
        if number <= 0:
            continue
        records.append(
            BillStatusFile(
                congress=congress,
                bill_type=bill_type,
                bill_number=number,
                url=urljoin(listing_url, quote(name)),
                modified_at=_normalize_listing_time(
                    _text(node, "formattedLastModifiedTime")
                ),
            )
        )

    return sorted(records, key=lambda record: record.bill_number)


def discover_bill_files(
    congress: int,
    *,
    bill_types: Sequence[str] = SUPPORTED_TYPES,
    client: Optional[GovInfoBulkClient] = None,
) -> list[BillStatusFile]:
    """Discover H.R. and S. XML records for a Congress from public listings."""
    congress = validate_congress(congress)
    normalized_types = tuple(validate_bill_type(value) for value in bill_types)
    if not normalized_types:
        raise ValueError("at least one bill type is required")
    if len(set(normalized_types)) != len(normalized_types):
        raise ValueError("duplicate bill types are not allowed")
    client = client or GovInfoBulkClient()
    records: list[BillStatusFile] = []

    for bill_type in normalized_types:
        listing_url = f"{BASE_URL}/{congress}/{_TYPE_PATHS[bill_type]}/"
        data = client.get_bytes(listing_url, max_bytes=100 * 1024 * 1024)
        records.extend(
            parse_directory_listing(
                data,
                congress=congress,
                bill_type=bill_type,
                listing_url=listing_url,
            )
        )
    return sorted(records)


def bill_type_zip_url(congress: int, bill_type: str) -> str:
    """Return the official no-key GovInfo ZIP URL for one bill type."""
    congress = validate_congress(congress)
    path_type = _TYPE_PATHS[validate_bill_type(bill_type)]
    return f"{BASE_URL}/{congress}/{path_type}/BILLSTATUS-{congress}-{path_type}.zip"


def _items(parent: Element, container_name: str) -> list[Element]:
    container = _child(parent, container_name)
    return _children(container, "item")


def parse_bill_xml(
    data: bytes,
    *,
    source_url: str,
    source_updated_at: str = "",
) -> dict:
    """Normalize one Bill Status XML document through ``canonical_bill``."""
    root = _parse_xml(data, source_url)
    bill = next(
        (node for node in root.iter() if _localname(node.tag) == "bill"), None
    )
    if bill is None:
        raise BillStatusError(f"Bill Status XML has no bill element: {source_url}")

    try:
        congress = validate_congress(int(_text(bill, "congress")))
        # Bill Status 2.x used billType/billNumber; 3.x uses type/number.
        bill_type = validate_bill_type(_text_any(bill, "type", "billType"))
        bill_number = int(_text_any(bill, "number", "billNumber"))
    except (TypeError, ValueError) as exc:
        raise BillStatusError(f"invalid bill identity in {source_url}: {exc}") from exc
    if bill_number <= 0:
        raise BillStatusError(f"invalid bill number in {source_url}: {bill_number}")

    sponsors = [
        {
            "bioguide_id": _text(item, "bioguideId"),
            "full_name": _text(item, "fullName"),
            "party": _text(item, "party"),
            "state": _text(item, "state"),
            "is_by_request": _text(item, "isByRequest"),
        }
        for item in _items(bill, "sponsors")
    ]
    if sponsors and not sponsors[0]["is_by_request"]:
        sponsors[0]["is_by_request"] = _text(bill, "isByRequest")
    actions = [
        {
            "action_date": _text(item, "actionDate"),
            "action_code": _text(item, "actionCode"),
            "text": _text(item, "text"),
        }
        for item in _items(bill, "actions")
    ]
    laws = [
        {"type": _text(item, "type"), "number": _text(item, "number")}
        for item in _items(bill, "laws")
    ]
    updated = (
        str(source_updated_at).strip()
        or _text(bill, "updateDateIncludingText")
        or _text(bill, "updateDate")
    )
    origin_chamber = _text(bill, "originChamber")
    if not origin_chamber:
        origin_chamber = {"H": "House", "S": "Senate"}.get(
            _text(bill, "originChamberCode"), ""
        )

    return canonical_bill(
        source=SOURCE,
        source_url=source_url,
        source_updated_at=updated,
        congress=congress,
        bill_type=bill_type,
        bill_number=bill_number,
        title=_text(bill, "title"),
        origin_chamber=origin_chamber,
        introduced_date=_text(bill, "introducedDate"),
        sponsors=sponsors,
        actions=actions,
        laws=laws,
    )


def changed_bill_files(
    discovered: Iterable[BillStatusFile],
    existing: Optional[pd.DataFrame],
    *,
    full: bool = False,
) -> list[BillStatusFile]:
    """Select missing records and records newer than the stored source timestamp."""
    files = sorted(discovered)
    if full or existing is None or existing.empty:
        return files
    current = normalize_bills(existing).set_index("bill_id")
    selected: list[BillStatusFile] = []
    for record in files:
        if record.bill_id not in current.index:
            selected.append(record)
            continue
        row = current.loc[record.bill_id]
        if (
            str(row["source"]) != SOURCE
            or str(row["source_url"]) != record.url
            or _timestamp_is_newer(
                record.modified_at, str(row["source_updated_at"])
            )
        ):
            selected.append(record)
    return selected


def _timestamp_is_newer(candidate: str, stored: str) -> bool:
    """Compare ISO timestamps, selecting defensively when either is invalid."""
    def parse(value: str) -> dt.datetime:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return parsed

    try:
        return parse(candidate) > parse(stored)
    except (TypeError, ValueError):
        return str(candidate).strip() != str(stored).strip()


def fetch_bill_files(
    records: Sequence[BillStatusFile],
    *,
    client: Optional[GovInfoBulkClient] = None,
    workers: int = 8,
) -> pd.DataFrame:
    """Download and parse selected bill records, failing the batch on any error."""
    if workers <= 0:
        raise ValueError("workers must be positive")
    if not records:
        return empty_bills()
    client = client or GovInfoBulkClient()

    def fetch(record: BillStatusFile) -> dict:
        data = client.get_bytes(record.url, max_bytes=_MAX_XML_BYTES)
        row = parse_bill_xml(
            data,
            source_url=record.url,
            source_updated_at=record.modified_at,
        )
        if row["bill_id"] != record.bill_id:
            raise BillStatusError(
                f"listing/XML identity mismatch: {record.bill_id} != {row['bill_id']}"
            )
        return row

    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(fetch, records))
    return normalize_bills(pd.DataFrame(rows, columns=BILL_COLUMNS))


def parse_bill_type_zip(
    data: bytes,
    *,
    congress: int,
    bill_type: str,
) -> pd.DataFrame:
    """Parse one official bill-type ZIP without extracting files to disk."""
    congress = validate_congress(congress)
    bill_type = validate_bill_type(bill_type)
    path_type = _TYPE_PATHS[bill_type]
    archive_url = bill_type_zip_url(congress, bill_type)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise BillStatusError(f"invalid Bill Status ZIP {archive_url}: {exc}") from exc

    with archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) > _MAX_ZIP_MEMBERS:
            raise BillStatusError(f"too many entries in Bill Status ZIP {archive_url}")
        if sum(info.file_size for info in members) > _MAX_ZIP_UNCOMPRESSED_BYTES:
            raise BillStatusError(
                f"uncompressed Bill Status ZIP is too large: {archive_url}"
            )

        rows: list[dict] = []
        seen: set[str] = set()
        for info in members:
            name = Path(info.filename).name
            match = _FILE_RE.fullmatch(name)
            if not match:
                continue
            if (
                int(match.group("congress")) != congress
                or match.group("type") != path_type
            ):
                continue
            if info.file_size > _MAX_XML_BYTES:
                raise BillStatusError(f"Bill Status XML entry is too large: {name}")
            number = int(match.group("number"))
            source_url = (
                f"{BASE_URL}/{congress}/{path_type}/{quote(name)}"
            )
            # GovInfo stamps ZIP members at archive-build time. Rounded to the
            # listing's minute precision, this is a high-water mark: unchanged
            # per-file listing timestamps are older, while later edits are newer.
            modified_at = dt.datetime(*info.date_time).replace(
                second=0, microsecond=0
            ).isoformat(timespec="seconds")
            try:
                xml = archive.read(info)
            except (RuntimeError, zipfile.BadZipFile) as exc:
                raise BillStatusError(
                    f"could not read {name} from {archive_url}: {exc}"
                ) from exc
            row = parse_bill_xml(
                xml,
                source_url=source_url,
                source_updated_at=modified_at,
            )
            expected_id = bill_id(congress, bill_type, number)
            if row["bill_id"] != expected_id:
                raise BillStatusError(
                    f"ZIP/XML identity mismatch: {expected_id} != {row['bill_id']}"
                )
            if expected_id in seen:
                raise BillStatusError(
                    f"duplicate bill {expected_id} in Bill Status ZIP {archive_url}"
                )
            seen.add(expected_id)
            rows.append(row)

    if not rows:
        raise BillStatusError(f"Bill Status ZIP contains no {bill_type} bills: {archive_url}")
    return normalize_bills(pd.DataFrame(rows, columns=BILL_COLUMNS))


def fetch_bill_type_zips(
    congresses: Sequence[int],
    *,
    client: Optional[GovInfoBulkClient] = None,
    workers: int = 8,
) -> pd.DataFrame:
    """Download the two official H.R./S. ZIPs for every requested Congress."""
    if workers <= 0:
        raise ValueError("workers must be positive")
    normalized = tuple(validate_congress(value) for value in congresses)
    client = client or GovInfoBulkClient()
    targets = [
        (congress, bill_type)
        for congress in normalized
        for bill_type in SUPPORTED_TYPES
    ]

    def fetch(target: tuple[int, str]) -> pd.DataFrame:
        congress, bill_type = target
        url = bill_type_zip_url(congress, bill_type)
        data = client.get_bytes(url, max_bytes=_MAX_ZIP_BYTES)
        return parse_bill_type_zip(
            data,
            congress=congress,
            bill_type=bill_type,
        )

    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        frames = list(pool.map(fetch, targets))
    return normalize_bills(pd.concat(frames, ignore_index=True))


def update_bill_status(
    congresses: Sequence[int],
    output_path: Path,
    *,
    full: bool = False,
    allow_missing_listings_for: Sequence[int] = (),
    client: Optional[GovInfoBulkClient] = None,
    workers: int = 8,
) -> BillStatusUpdate:
    """Discover, fetch, merge, and atomically save one or more Congresses.

    A 404 while discovering a Congress listed in ``allow_missing_listings_for``
    is treated as no data. Other HTTP, download, and parsing failures propagate.
    """
    normalized = tuple(validate_congress(value) for value in congresses)
    if not normalized:
        raise ValueError("at least one Congress is required")
    if len(set(normalized)) != len(normalized):
        raise ValueError("duplicate Congresses are not allowed")
    if workers <= 0:
        raise ValueError("workers must be positive")
    optional = tuple(
        validate_congress(value) for value in allow_missing_listings_for
    )
    if len(set(optional)) != len(optional):
        raise ValueError("duplicate optional Congresses are not allowed")
    if not set(optional).issubset(normalized):
        raise ValueError("optional Congresses must be update targets")
    if full and optional:
        raise ValueError("full updates cannot allow missing listings")

    client = client or GovInfoBulkClient()
    existing = load_bills(output_path)
    if full:
        fresh = fetch_bill_type_zips(
            normalized,
            client=client,
            workers=workers,
        )
        retained = (
            None
            if existing is None
            else existing[~existing["congress"].isin(normalized)].reset_index(drop=True)
        )
        merged = merge_bills(retained, fresh)
        written = tuple(save_bills(merged, output_path))
        return BillStatusUpdate(
            discovered=len(fresh),
            selected=len(fresh),
            fetched=len(fresh),
            written=written,
            bills=merged,
        )

    selected: list[BillStatusFile] = []
    discovered_count = 0
    for congress in normalized:
        try:
            discovered = discover_bill_files(congress, client=client)
        except GovInfoNotFoundError:
            if congress not in optional:
                raise
            LOG.info(
                "GovInfo listing for optional Congress %d is not available yet",
                congress,
            )
            continue
        discovered_count += len(discovered)
        congress_existing = (
            None
            if existing is None
            else existing[existing["congress"] == congress].reset_index(drop=True)
        )
        selected.extend(
            changed_bill_files(discovered, congress_existing, full=full)
        )

    fresh = fetch_bill_files(selected, client=client, workers=workers)
    merged = merge_bills(existing, fresh)
    written = tuple(save_bills(merged, output_path)) if not fresh.empty else ()
    return BillStatusUpdate(
        discovered=discovered_count,
        selected=len(selected),
        fetched=len(fresh),
        written=written,
        bills=merged,
    )
