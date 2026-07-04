"""Offline unit tests for the analysis pipeline (no network, no large data)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.normalize.parties import normalize_party, opposing_party  # noqa: E402
from analysis.score.scorers import Scorers  # noqa: E402
from analysis.ingest.govinfo import (  # noqa: E402
    _congress_from_date,
    _segment,
    _speaker_surname,
    _strip_header,
    _surname,
)


def test_normalize_party() -> None:
    assert normalize_party("D") == "D"
    assert normalize_party("Democrat") == "D"
    assert normalize_party("100") == "D"
    assert normalize_party("R") == "R"
    assert normalize_party("200") == "R"
    assert normalize_party("I") == "I"
    assert normalize_party("Whig") == "other"
    assert normalize_party("") == "other"
    assert normalize_party(None) == "other"
    assert opposing_party("D") == "R"
    assert opposing_party("R") == "D"
    assert opposing_party("I") is None


def test_scorer_comity_and_hostility() -> None:
    s = Scorers()
    r = s.score_turn("I thank the gentleman from Ohio, my distinguished colleague.", "D")
    assert r["comity_hits"] >= 2  # "i thank the gentleman" + "my distinguished colleague"
    assert r["hostility_hits"] == 0

    r2 = s.score_turn("This is a disgraceful, corrupt, un-American lie.", "R")
    assert r2["hostility_hits"] >= 3  # disgraceful, corrupt, un-american, lie


def test_scorer_profanity_tiers() -> None:
    s = Scorers()
    r = s.score_turn("what the hell is this damn nonsense", "D")
    assert r["profanity_mild"] >= 2  # hell + damn
    assert r["profanity_hits"] >= 2


def test_scorer_outgroup_directed_and_pejorative() -> None:
    s = Scorers()
    # A Democrat attacking Republicans near an out-group reference.
    txt = "My Republican colleagues are reckless and dangerous on this bill."
    r = s.score_turn(txt, "D")
    assert r["outgroup_refs"] >= 1               # "republican" is out-group for a D
    assert r["directed_hostility_hits"] >= 2     # reckless + dangerous near the ref

    # Same words but speaker is Republican -> "republican" is NOT out-group.
    r2 = s.score_turn(txt, "R")
    assert r2["outgroup_refs"] == 0

    # "Democrat party" pejorative marker.
    r3 = s.score_turn("The Democrat party wants to raise your taxes.", "R")
    assert r3["democrat_party_pej"] == 1


def test_govinfo_helpers() -> None:
    assert _congress_from_date("2017-01-03") == 115
    assert _congress_from_date("2024-01-09") == 118
    assert _congress_from_date("1995-02-01") == 104
    assert _surname("Smith, Jane Q.") == "SMITH"
    assert _surname("Jane Q. Smith") == "SMITH"
    assert _speaker_surname("Mr. McCONNELL") == "MCCONNELL"
    assert _speaker_surname("Ms. PELOSI of California") == "PELOSI"


def test_govinfo_segment_and_header() -> None:
    raw = (
        "[Congressional Record Volume 163, Number 1 (Tuesday, January 3, 2017)]\n"
        "[House]\n[Page H29]\n"
        "From the Congressional Record Online through the Government Publishing Office [www.gpo.gov]\n\n"
        "  Mr. CROWLEY. Mr. Speaker, I offer a resolution.\n"
        "  Ms. NORTON. Mr. Speaker, I rise in support.\n"
    )
    stripped = _strip_header(raw)
    assert stripped.startswith("Mr. CROWLEY")
    segs = _segment(stripped)
    speakers = [sp for sp, _ in segs if sp]
    assert "Mr. CROWLEY" in speakers
    assert "Ms. NORTON" in speakers


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
