"""GovInfo API client: authentication, throttling, retries, and pagination.

Docs: https://api.govinfo.gov/docs/ and https://www.govinfo.gov/developers
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterator, Optional
from urllib.parse import parse_qs, urlparse

import requests
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass

LOG = logging.getLogger("crec.api")

BASE_URL = "https://api.govinfo.gov"


class GovInfoError(Exception):
    """Base error for GovInfo API problems."""


class RetryableError(GovInfoError):
    """A transient error (429 / 5xx / network) that is safe to retry."""


class GovInfoClient:
    """Thin HTTP client for the GovInfo API.

    Handles api_key injection, polite throttling, exponential-backoff retries on
    transient failures, and helpers for the cursor (``offsetMark``) pagination
    style that GovInfo uses.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = BASE_URL,
        min_interval: float = 0.0,
        timeout: float = 60.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("GOVINFO_API_KEY") or "DEMO_KEY"
        self.base_url = base_url.rstrip("/")
        self.min_interval = max(0.0, float(min_interval))
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "congressional-record-pipeline/0.1")
        self._last_request = 0.0

    # ------------------------------------------------------------------ #
    # Low-level request handling
    # ------------------------------------------------------------------ #
    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        wait = self.min_interval - elapsed
        if wait > 0:
            time.sleep(wait)

    @retry(
        retry=retry_if_exception_type((RetryableError, requests.RequestException)),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(LOG, logging.WARNING),
        reraise=True,
    )
    def _request(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        self._throttle()
        params = dict(params or {})
        # Never duplicate api_key if the URL already carries one (e.g. nextPage links).
        if "api_key" not in urlparse(url).query:
            params.setdefault("api_key", self.api_key)
        try:
            resp = self.session.request(method, url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            LOG.warning("Network error for %s: %s", url, exc)
            raise
        finally:
            self._last_request = time.monotonic()

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    time.sleep(min(30.0, float(retry_after)))
                except ValueError:
                    pass
            raise RetryableError(f"429 rate limited for {url}")
        if resp.status_code >= 500:
            raise RetryableError(f"{resp.status_code} server error for {url}")
        if resp.status_code == 404:
            raise GovInfoError(f"404 not found: {url}")
        resp.raise_for_status()

        # api.data.gov sometimes returns HTTP 200 with a JSON error body for the
        # hourly GovInfo rate limit; detect it so we retry instead of silently
        # treating the page as empty.
        ctype = resp.headers.get("Content-Type", "")
        if "json" in ctype and b'"error"' in resp.content[:4096]:
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            code = (err.get("code") or "").upper()
            message = err.get("message") or "unknown API error"
            if "RATE" in code or "LIMIT" in code:
                raise RetryableError(f"rate limited ({code}) for {url}: {message}")
            raise GovInfoError(f"API error ({code}) for {url}: {message}")
        return resp

    # ------------------------------------------------------------------ #
    # Typed helpers
    # ------------------------------------------------------------------ #
    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._request("GET", url, params).json()

    def get_text(self, url: str, params: Optional[Dict[str, Any]] = None) -> str:
        return self._request("GET", url, params).text

    def get_bytes(self, url: str, params: Optional[Dict[str, Any]] = None) -> bytes:
        return self._request("GET", url, params).content

    def url(self, path: str) -> str:
        """Build an absolute API URL from a relative path."""
        if path.startswith("http"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    # ------------------------------------------------------------------ #
    # Pagination
    # ------------------------------------------------------------------ #
    def paginate(
        self,
        url: str,
        items_key: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield items across all pages of a cursor-paginated GovInfo endpoint.

        Follows the ``nextPage`` link returned by the API until it is null or a
        page returns no items. ``items_key`` is the JSON array field to iterate
        (e.g. ``"packages"`` or ``"granules"``).
        """
        params = dict(params or {})
        params.setdefault("offsetMark", "*")
        next_url: Optional[str] = url
        next_params: Optional[Dict[str, Any]] = params
        while next_url:
            data = self.get_json(next_url, next_params)
            items = data.get(items_key) or []
            for item in items:
                yield item
            next_page = data.get("nextPage")
            if not next_page or not items:
                break
            # nextPage is a full URL already carrying offsetMark; follow it directly.
            next_url = next_page
            next_params = None
