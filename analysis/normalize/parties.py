"""Normalize heterogeneous party labels to a small canonical set: D / R / I / other."""

from __future__ import annotations

from typing import Optional

# hein SpeakerMap uses D/R/I plus historical codes; GovInfo MODS uses D/R/I/ID etc.
_DEMOCRAT = {"D", "DEM", "DEMOCRAT", "DEMOCRATIC", "100"}
_REPUBLICAN = {"R", "REP", "REPUBLICAN", "GOP", "200"}
_INDEPENDENT = {"I", "IND", "INDEPENDENT", "ID", "II", "328"}


def normalize_party(raw: Optional[str]) -> str:
    """Map a raw party string/code to ``D`` / ``R`` / ``I`` / ``other``.

    ``other`` covers third/historical parties (Whig, Populist, etc.) and unknowns
    so downstream D-vs-R comparisons stay clean.
    """
    if raw is None:
        return "other"
    key = str(raw).strip().upper()
    if not key:
        return "other"
    if key in _DEMOCRAT:
        return "D"
    if key in _REPUBLICAN:
        return "R"
    if key in _INDEPENDENT:
        return "I"
    return "other"


def opposing_party(party: str) -> Optional[str]:
    """Return the canonical opposing major party, or None if not D/R."""
    if party == "D":
        return "R"
    if party == "R":
        return "D"
    return None
