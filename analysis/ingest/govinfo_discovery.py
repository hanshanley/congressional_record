"""Outage-tolerant discovery policy for scheduled site updates."""

from typing import List

from analysis.ingest import govinfo_bulk


def probe_packages(start: str, end: str, workers: int = 8) -> List[str]:
    """Retry unresolved dates through nearly 16 minutes of bounded backoff.

    GovInfo's edge can return 520/522 for several minutes. Keep this transport
    policy separate from the historically audited ingest and scoring code.
    Exhaustion still raises rather than allowing a partial publication.
    """
    return govinfo_bulk.probe_packages(
        start,
        end,
        workers=workers,
        retry_delays=(15, 30, 60, 120, 240, 480),
    )
