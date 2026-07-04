"""Enumerate Congressional Record packages (daily issues) and their granules."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, Iterator, Optional, Sequence

from .api import GovInfoClient

LOG = logging.getLogger("crec.enumerate")

COLLECTION = "CREC"


def next_month(d: dt.date) -> dt.date:
    """Return the first day of the month after ``d``."""
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1, day=1)
    return d.replace(month=d.month + 1, day=1)


def iter_packages(
    client: GovInfoClient,
    start_date: str,
    end_date: str,
    collection: str = COLLECTION,
    page_size: int = 1000,
) -> Iterator[Dict[str, Any]]:
    """Yield package records (one per published daily issue) over a date range.

    Dates are ``YYYY-MM-DD`` strings. Each yielded dict includes ``packageId``,
    ``dateIssued``, ``congress`` and ``title``.
    """
    url = client.url(f"/published/{start_date}/{end_date}")
    params = {"collection": collection, "pageSize": page_size, "offsetMark": "*"}
    yield from client.paginate(url, items_key="packages", params=params)


def iter_granules(
    client: GovInfoClient,
    package_id: str,
    classes: Optional[Sequence[str]] = None,
    page_size: int = 100,
) -> Iterator[Dict[str, Any]]:
    """Yield granule records for a package, optionally filtered by granuleClass.

    ``classes`` is a set/list of granuleClass values to keep (e.g.
    ``["HOUSE", "SENATE", "EXTENSIONS"]``). ``None`` keeps everything.
    """
    wanted = {c.upper() for c in classes} if classes else None
    url = client.url(f"/packages/{package_id}/granules")
    params = {"pageSize": page_size, "offsetMark": "*"}
    for granule in client.paginate(url, items_key="granules", params=params):
        if wanted is None or (granule.get("granuleClass") or "").upper() in wanted:
            yield granule


def daterange_months(start_date: str, end_date: str) -> Iterator[tuple[str, str]]:
    """Yield ``(month_start, month_end)`` date pairs covering ``[start, end]``.

    Chunking the enumeration by month keeps each ``/published`` response small
    and makes progress/restarts easier on multi-decade backfills.
    """
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    cur = start.replace(day=1)
    while cur <= end:
        nxt = next_month(cur)
        chunk_start = max(cur, start)
        chunk_end = min(nxt - dt.timedelta(days=1), end)
        yield chunk_start.isoformat(), chunk_end.isoformat()
        cur = nxt
