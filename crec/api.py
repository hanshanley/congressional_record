"""GovInfo API client: authentication, throttling, retries, and pagination.

Docs: https://api.govinfo.gov/docs/ and https://www.govinfo.gov/developers
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterator, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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


def _redact(url: str) -> str:
    """Return ``url`` with any ``api_key`` query value masked, for safe logging."""
    try:
        parts = urlparse(url)
        if not parts.query:
            return url
        q = [
            (k, "REDACTED" if k == "api_key" else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunparse(parts._replace(query=urlencode(q)))
    except Exception:  # pragma: no cover - never let logging redaction fail a request
        return url


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
        wait=wait_exponential(multiplier=2, min=2, max=90),
        stop=stop_after_attempt(12),
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
            LOG.warning("Network error for %s: %s", _redact(url), exc)
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
            raise RetryableError(f"429 rate limited for {_redact(url)}")
        if resp.status_code >= 500:
            raise RetryableError(f"{resp.status_code} server error for {_redact(url)}")
        # Non-retryable 4xx (400/401/403/404/406...) must fail fast, not retry: an
        # invalid api_key (401/403) otherwise triggers a futile 5x backoff storm.
        if resp.status_code == 404:
            raise GovInfoError(f"404 not found: {_redact(url)}")
        if 400 <= resp.status_code < 500:
            raise GovInfoError(f"{resp.status_code} client error for {_redact(url)}")
        resp.raise_for_status()

        # api.data.gov sometimes returns HTTP 200 with a JSON error body for the
        # hourly GovInfo rate limit; detect it (by parsing, not substring-scanning,
        # to avoid false positives on data whose value happens to be "error") so we
        # retry instead of silently treating the page as empty.
        ctype = resp.headers.get("Content-Type", "")
        if "json" in ctype:
            try:
                body = resp.json()
            except ValueError:
                body = None
            err = body.get("error") if isinstance(body, dict) else None
            if err is not None:
                if isinstance(err, str):
                    err = {"message": err}
                code = (err.get("code") or "").upper()
                message = err.get("message") or "unknown API error"
                if "RATE" in code or "LIMIT" in code:
                    raise RetryableError(f"rate limited ({code}) for {_redact(url)}: {message}")
                raise GovInfoError(f"API error ({code}) for {_redact(url)}: {message}")
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

    def _same_host(self, url: str) -> bool:
        """True if ``url`` targets the configured API host (blocks SSRF/key leak)."""
        return urlparse(url).netloc == urlparse(self.base_url).netloc

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

        Follows the ``nextPage`` link returned by the API until it is null.
        ``items_key`` is the JSON array field to iterate (e.g. ``"packages"`` or
        ``"granules"``).
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
            # Terminate only when the API says there is no next page. An empty
            # intermediate page must NOT stop enumeration or we silently truncate.
            if not next_page:
                break
            # nextPage is a full URL already carrying offsetMark; follow it directly,
            # but only if it stays on the trusted host (prevents SSRF / api_key leak).
            if not self._same_host(next_page):
                LOG.warning("Refusing cross-host nextPage: %s", _redact(next_page))
                break
            next_url = next_page
            next_params = None
