"""Congress.gov API adapter for seeding legacy H.R. and S. bill records."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import pandas as pd
import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass

from analysis.bills import (
    BILL_COLUMNS,
    bill_id,
    canonical_bill,
    load_bills,
    merge_bills,
    save_bills,
)
from analysis.ingest.schema import normalize_chamber

API_BASE_URL = "https://api.congress.gov/v3/"
DEFAULT_CONGRESSES = tuple(range(103, 108))
DEFAULT_BILL_TYPES = ("HR", "S")
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class CongressAPIError(RuntimeError):
    """A bounded Congress.gov request or response failure."""


def _bill_type(value: str) -> str:
    normalized = str(value).replace(".", "").strip().upper()
    if normalized not in DEFAULT_BILL_TYPES:
        raise ValueError(f"unsupported bill type: {value!r}")
    return normalized


def _without_api_key(url: str) -> str:
    """Remove credentials from URLs returned by the API before reuse/storage."""
    parts = urlsplit(str(url))
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query)
            if key.lower() not in {"api_key", "apikey"}
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


class CongressAPIClient:
    """Small, retrying client for the Congress.gov bill and action endpoints."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        session: Optional[requests.Session] = None,
        base_url: str = API_BASE_URL,
        timeout: float | tuple[float, float] = (10.0, 30.0),
        max_retries: int = 4,
        min_interval: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        key = api_key
        if key is None:
            key = os.environ.get("CONGRESS_API_KEY") or os.environ.get(
                "GOVINFO_API_KEY"
            )
        if not key or not key.strip():
            raise ValueError(
                "Congress.gov API key required; set CONGRESS_API_KEY "
                "(preferred), GOVINFO_API_KEY (fallback), or pass api_key"
            )
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if min_interval < 0:
            raise ValueError("min_interval must be non-negative")
        self.api_key = key.strip()
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.max_retries = int(max_retries)
        self.min_interval = float(min_interval)
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: Optional[float] = None

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            remaining = self.min_interval - (
                self._monotonic() - self._last_request_at
            )
            if remaining > 0:
                self._sleep(remaining)

    def _request_json(
        self, url: str, *, params: Optional[Mapping[str, object]] = None
    ) -> Mapping[str, object]:
        clean_url = _without_api_key(urljoin(self.base_url, url))
        request_params = dict(params or {})
        request_params.update({"api_key": self.api_key, "format": "json"})

        for attempt in range(self.max_retries + 1):
            self._throttle()
            try:
                response = self.session.get(
                    clean_url, params=request_params, timeout=self.timeout
                )
                self._last_request_at = self._monotonic()
            except requests.RequestException as exc:
                self._last_request_at = self._monotonic()
                retryable = isinstance(
                    exc, (requests.Timeout, requests.ConnectionError)
                )
                if retryable and attempt < self.max_retries:
                    self._sleep(min(2**attempt, 30))
                    continue
                attempts = attempt + 1
                suffix = "" if attempts == 1 else "s"
                raise CongressAPIError(
                    f"Congress.gov request failed after {attempts} attempt{suffix}"
                ) from None

            status = int(getattr(response, "status_code", 0))
            if status in _RETRY_STATUSES and attempt < self.max_retries:
                retry_after = getattr(response, "headers", {}).get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after is not None else 2**attempt
                except (TypeError, ValueError):
                    delay = 2**attempt
                self._sleep(min(max(delay, 0.0), 60.0))
                continue
            if status < 200 or status >= 300:
                raise CongressAPIError(
                    f"Congress.gov request returned HTTP {status or 'unknown'}"
                )
            try:
                payload = response.json()
            except (TypeError, ValueError):
                raise CongressAPIError("Congress.gov returned invalid JSON") from None
            if not isinstance(payload, Mapping):
                raise CongressAPIError("Congress.gov returned a non-object JSON response")
            return payload

        raise CongressAPIError("Congress.gov request exhausted retries")  # pragma: no cover

    def _iter_pages(
        self, endpoint: str, collection: str, *, limit: int = 250
    ) -> Iterator[Mapping[str, object]]:
        next_url: Optional[str] = endpoint
        params: Optional[Mapping[str, object]] = {"limit": max(1, min(limit, 250))}
        seen_pages: set[str] = set()
        while next_url:
            page_url = _without_api_key(urljoin(self.base_url, next_url))
            if page_url in seen_pages:
                raise CongressAPIError("Congress.gov pagination repeated a page")
            seen_pages.add(page_url)
            payload = self._request_json(page_url, params=params)
            params = None
            items = payload.get(collection, [])
            if not isinstance(items, list):
                raise CongressAPIError(
                    f"Congress.gov response field {collection!r} is not a list"
                )
            for item in items:
                if isinstance(item, Mapping):
                    yield item
            pagination = payload.get("pagination") or {}
            if not isinstance(pagination, Mapping):
                raise CongressAPIError("Congress.gov pagination is not an object")
            raw_next = pagination.get("next")
            next_url = str(raw_next) if raw_next else None

    def iter_bill_summaries(
        self, congress: int, bill_type: str
    ) -> Iterator[Mapping[str, object]]:
        """Enumerate every bill summary for one Congress and measure type."""
        normalized = _bill_type(bill_type)
        if int(congress) <= 0:
            raise ValueError("congress must be positive")
        yield from self._iter_pages(
            f"bill/{int(congress)}/{normalized.lower()}", "bills"
        )

    def get_bill(
        self, congress: int, bill_type: str, bill_number: int | str
    ) -> Mapping[str, object]:
        """Fetch one detailed bill object."""
        normalized = _bill_type(bill_type)
        endpoint = (
            f"bill/{int(congress)}/{normalized.lower()}/{int(bill_number)}"
        )
        payload = self._request_json(endpoint)
        item = payload.get("bill")
        if not isinstance(item, Mapping):
            raise CongressAPIError("Congress.gov bill response omitted the bill object")
        return item

    def iter_actions(
        self, congress: int, bill_type: str, bill_number: int | str
    ) -> Iterator[Mapping[str, object]]:
        """Fetch all action pages for one bill."""
        normalized = _bill_type(bill_type)
        endpoint = (
            f"bill/{int(congress)}/{normalized.lower()}/{int(bill_number)}/actions"
        )
        yield from self._iter_pages(endpoint, "actions")

    def fetch_canonical_bill(
        self, congress: int, bill_type: str, bill_number: int | str
    ) -> dict:
        """Fetch bill details/actions and map them to the shared canonical row."""
        item = self.get_bill(congress, bill_type, bill_number)
        actions = list(self.iter_actions(congress, bill_type, bill_number))
        return canonicalize_bill(
            item,
            actions,
            congress=int(congress),
            bill_type=_bill_type(bill_type),
            bill_number=int(bill_number),
            base_url=self.base_url,
        )


# Short alias for callers that prefer the service name.
CongressAPI = CongressAPIClient


def canonicalize_bill(
    item: Mapping[str, object],
    actions: Sequence[Mapping[str, object]],
    *,
    congress: Optional[int] = None,
    bill_type: Optional[str] = None,
    bill_number: Optional[int | str] = None,
    base_url: str = API_BASE_URL,
) -> dict:
    """Map one Congress.gov bill and its actions without retaining raw payloads."""
    resolved_congress = int(congress or item.get("congress") or 0)
    resolved_type = _bill_type(str(bill_type or item.get("type") or ""))
    resolved_number = int(bill_number or item.get("number") or 0)
    if resolved_congress <= 0 or resolved_number <= 0:
        raise ValueError("bill payload is missing a valid congress or number")

    sponsors = item.get("sponsors") or []
    laws = item.get("laws") or []
    if not isinstance(sponsors, list) or not isinstance(laws, list):
        raise CongressAPIError("Congress.gov bill sponsors/laws must be lists")

    safe_sponsors = [
        {
            "bioguideId": sponsor.get("bioguideId", ""),
            "fullName": sponsor.get("fullName") or sponsor.get("name") or "",
            "party": sponsor.get("party", ""),
            "state": sponsor.get("state", ""),
            "isByRequest": sponsor.get("isByRequest", ""),
        }
        for sponsor in sponsors
        if isinstance(sponsor, Mapping)
    ]
    safe_laws = [
        {"type": law.get("type", ""), "number": law.get("number", "")}
        for law in laws
        if isinstance(law, Mapping)
    ]
    safe_actions = [
        {
            "actionDate": action.get("actionDate", ""),
            "actionCode": action.get("actionCode", ""),
            "text": action.get("text", ""),
        }
        for action in actions
        if isinstance(action, Mapping)
    ]
    fallback_url = urljoin(
        base_url.rstrip("/") + "/",
        f"bill/{resolved_congress}/{resolved_type.lower()}/{resolved_number}",
    )
    source_url = _without_api_key(str(item.get("url") or fallback_url))
    return canonical_bill(
        source="congress.gov",
        source_url=source_url,
        source_updated_at=str(
            item.get("updateDateIncludingText") or item.get("updateDate") or ""
        ),
        congress=resolved_congress,
        bill_type=resolved_type,
        bill_number=resolved_number,
        title=str(item.get("title") or ""),
        origin_chamber=normalize_chamber(
            str(item.get("originChamberCode") or item.get("originChamber") or "")
        ),
        introduced_date=str(item.get("introducedDate") or ""),
        sponsors=safe_sponsors,
        actions=safe_actions,
        laws=safe_laws,
    )


def load_completed(path: Path) -> set[str]:
    """Load completed canonical bill IDs from a checkpoint file."""
    if not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, Mapping):
        values = payload.get("completed_bill_ids", [])
    else:
        raise ValueError(f"invalid Congress API checkpoint: {path}")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"invalid Congress API checkpoint: {path}")
    return set(values)


def save_completed(path: Path, completed: set[str]) -> None:
    """Atomically save only stable identifiers, never credentials or API payloads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "completed_bill_ids": sorted(completed)}
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True)
class SeedResult:
    listed: int
    fetched: int
    skipped: int
    written: tuple[Path, ...]
    bill_ids: tuple[str, ...]


def seed_legacy_bills(
    client: CongressAPIClient,
    *,
    output_path: Path,
    state_path: Path,
    congresses: Sequence[int] = DEFAULT_CONGRESSES,
    bill_types: Sequence[str] = DEFAULT_BILL_TYPES,
    batch_size: int = 100,
    list_only: bool = False,
) -> SeedResult:
    """Enumerate, fetch, checkpoint, and idempotently merge legacy bills."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    normalized_types = tuple(_bill_type(value) for value in bill_types)
    normalized_congresses = tuple(int(value) for value in congresses)
    if not normalized_congresses or any(value <= 0 for value in normalized_congresses):
        raise ValueError("congresses must contain positive values")

    completed = load_completed(state_path)
    if not list_only and completed:
        stored = load_bills(output_path)
        stored_ids = set() if stored is None else set(stored["bill_id"])
        reconciled = completed.intersection(stored_ids)
        if reconciled != completed:
            completed = reconciled
            save_completed(state_path, completed)
    pending_rows: list[dict] = []
    pending_ids: list[str] = []
    listed_ids: list[str] = []
    fetched = skipped = 0
    written: list[Path] = []

    def flush() -> None:
        nonlocal pending_rows, pending_ids
        if not pending_rows:
            return
        fresh = pd.DataFrame(pending_rows, columns=BILL_COLUMNS)
        # Touch only Congress partitions represented in this checkpoint batch.
        # Loading and comparing the entire 103-present table every 100 bills
        # makes the rate-limited historical seed needlessly slower.
        for congress, group in fresh.groupby("congress", sort=True):
            part = output_path / f"congress_{int(congress):03d}.parquet"
            merged = merge_bills(load_bills(part), group.reset_index(drop=True))
            written.extend(save_bills(merged, output_path))
        completed.update(pending_ids)
        save_completed(state_path, completed)
        pending_rows = []
        pending_ids = []

    for congress in normalized_congresses:
        for measure_type in normalized_types:
            for summary in client.iter_bill_summaries(congress, measure_type):
                number = summary.get("number")
                try:
                    identity = bill_id(congress, measure_type, int(str(number)))
                except (TypeError, ValueError):
                    raise CongressAPIError(
                        "Congress.gov bill list contained an invalid bill number"
                    ) from None
                listed_ids.append(identity)
                if list_only:
                    continue
                if identity in completed:
                    skipped += 1
                    continue
                pending_rows.append(
                    client.fetch_canonical_bill(congress, measure_type, int(str(number)))
                )
                pending_ids.append(identity)
                fetched += 1
                if len(pending_rows) >= batch_size:
                    flush()
    flush()
    return SeedResult(
        listed=len(listed_ids),
        fetched=fetched,
        skipped=skipped,
        written=tuple(dict.fromkeys(written)),
        bill_ids=tuple(listed_ids),
    )
