"""Dated member identities for Record PDFs that lack member-level MODS."""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
import json
import subprocess
import threading
import unicodedata


ROSTER_URLS = (
    "https://unitedstates.github.io/congress-legislators/legislators-current.json",
    "https://unitedstates.github.io/congress-legislators/legislators-historical.json",
)
_ROSTER_LOCK = threading.Lock()


@lru_cache(maxsize=1)
def _load_legislators() -> list[dict]:
    members = []
    for url in ROSTER_URLS:
        result = subprocess.run(
            [
                "curl", "-fsSL", "--retry", "3", "--retry-all-errors",
                "--connect-timeout", "15", "--max-time", "90", url,
            ],
            capture_output=True, text=True, check=True, timeout=420,
        )
        records = json.loads(result.stdout)
        if not isinstance(records, list) or not records:
            raise ValueError(f"empty or invalid legislator roster from {url}")
        members.extend(records)
    return members


def _active(period: dict, date: str) -> bool:
    return period.get("start", "") <= date <= period.get("end", "9999-12-31")


def members_on(date: str, chamber: str) -> list[dict[str, str]]:
    """Return only members serving in this chamber on the issue's date.

    Use dated terms and party-affiliation intervals, not today's membership or
    party. Surname-first names preserve compound surnames in the shared matcher.
    """
    dt.date.fromisoformat(date)
    term_type = {"house": "rep", "senate": "sen"}.get(chamber)
    if term_type is None:
        raise ValueError(f"no member roster for chamber {chamber!r}")
    with _ROSTER_LOCK:
        legislators = _load_legislators()
    members = []
    for legislator in legislators:
        terms = [
            term for term in legislator["terms"]
            if term["type"] == term_type and _active(term, date)
        ]
        if not terms:
            continue
        term = max(terms, key=lambda item: item["start"])
        party = term.get("party", "")
        affiliations = [
            item for item in term.get("party_affiliations", [])
            if _active(item, date)
        ]
        if affiliations:
            party = max(affiliations, key=lambda item: item["start"])["party"]
        names = [legislator["name"]]
        names.extend(
            name for name in legislator.get("other_names", [])
            if _active(name, date) and name.get("last")
        )
        for name in names:
            surname = "".join(
                char for char in unicodedata.normalize("NFKD", name["last"])
                if not unicodedata.combining(char)
            )
            members.append({
                "bioguide": legislator["id"]["bioguide"],
                "name": f"{surname}, {name.get('first', '')}".strip(),
                "state": term["state"],
                "party": party,
            })
    if not members:
        raise ValueError(f"no legislator identities for {chamber} on {date}")
    return members
