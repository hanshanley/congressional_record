"""Offline unit tests for the analysis pipeline (no network, no large data)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.normalize.parties import normalize_party, opposing_party  # noqa: E402
from analysis.score.scorers import Scorers  # noqa: E402
from analysis.ingest.govinfo import (  # noqa: E402
    _congress_from_date,
    _congress_from_row,
    _segment,
    _speaker_state,
    _speaker_surname,
    _strip_header,
    _surname,
    build_turns,
    normalize_members,
)
from analysis.ingest.schema import congress_from_year, year_from_congress  # noqa: E402
from analysis.score.registry import METRICS, SCORE_KEYS  # noqa: E402


def test_congress_year_roundtrip() -> None:
    assert congress_from_year(1873) == 43
    assert congress_from_year(2017) == 115
    assert congress_from_year(2025) == 119
    assert year_from_congress(43) == 1873
    assert year_from_congress(119) == 2025
    # round-trip: a congress's convening year maps back to itself
    for c in (43, 90, 115, 119):
        assert congress_from_year(year_from_congress(c)) == c


def test_metric_registry_matches_scorer_outputs() -> None:
    rates = [metric.rate for metric in METRICS]
    assert len(rates) == len(set(rates))
    assert {metric.rate for metric in METRICS if metric.headline} == {
        "formal_courtesy_per_1k", "profanity_per_1k", "ethnic_slurs_per_1k",
    }
    scored = Scorers().score_turn("I yield to my distinguished colleague. Damn.")
    assert set(scored) == {"n_words", *SCORE_KEYS, "profanity_hits"}


def test_multiword_surname_segmentation() -> None:
    # Multi-word surnames must each start their own turn (not merge into the prior one).
    text = _strip_header(
        "  Mr. VAN HOLLEN. Madam President, I rise.\n"
        "  Ms. WASSERMAN SCHULTZ. Mr. Speaker, I object.\n"
        "  Mr. VAN HOLLEN of Maryland. I yield back.\n"
    )
    markers = [sp for sp, _ in _segment(text) if sp]
    assert "Mr. VAN HOLLEN" in markers
    assert "Ms. WASSERMAN SCHULTZ" in markers
    assert _speaker_surname("Mr. VAN HOLLEN of Maryland") == "VAN HOLLEN"
    # A single-token surname followed by "Mr. Speaker" must NOT absorb the next word.
    m2 = [sp for sp, _ in _segment("Mr. CROWLEY. Mr. Speaker, I offer a bill.") if sp]
    assert m2 == ["Mr. CROWLEY"]
    # Multi-word STATE names (New York, North Carolina, Rhode Island, …) must be detected —
    # otherwise a large, common subset of 2017+ members would be dropped/misattributed.
    multi = _segment(
        "Mr. NADLER of New York. Mr. Speaker, I rise.\n"
        "Ms. ADAMS of North Carolina. I object.\n"
        "Mr. CICILLINE of Rhode Island. I yield.\n"
    )
    got = [sp for sp, _ in multi if sp]
    assert "Mr. NADLER of New York" in got
    assert "Ms. ADAMS of North Carolina" in got
    assert "Mr. CICILLINE of Rhode Island" in got


def test_build_turns_party_attribution_and_multiword_match() -> None:
    members = normalize_members([
        {"party": "D", "bioGuideId": "V000128", "state": "MD", "name": "Chris Van Hollen"},
        {"party": "R", "bioGuideId": "M000355", "state": "KY", "name": "Mitch McConnell"},
    ])
    text = ("Mr. VAN HOLLEN. Madam President, I support this.\n"
            "Mr. McCONNELL. I do not.\n")
    turns = list(build_turns(text, members, "CREC-2025-01-01-pt1-PgS1", "2025-01-01", 119, "senate"))
    by_party = {t["speaker_name"]: t["party"] for t in turns}
    # "Chris Van Hollen" -> surname index under "VAN HOLLEN" and "HOLLEN"; marker matches.
    assert by_party.get("Mr. VAN HOLLEN") == "D"
    assert by_party.get("Mr. McCONNELL") == "R"


def test_build_turns_preamble_not_attributed_to_sole() -> None:
    members = normalize_members([{"party": "R", "bioGuideId": "X", "state": "TX", "name": "Jane Smith"}])
    # Leading boilerplate before the first marker must not be attributed to the sole member.
    text = "SOME HEADING BOILERPLATE\nMr. SMITH. Mr. Speaker, I rise."
    turns = list(build_turns(text, members, "CREC-2025-01-01-pt1-PgH1", "2025-01-01", 119, "house"))
    preamble = [t for t in turns if not t["speaker_name"]]
    assert all(t["party"] == "other" for t in preamble)


def test_build_turns_rejects_ambiguous_surname_and_uses_state() -> None:
    members = normalize_members([
        {"party": "D", "bioGuideId": "S1", "state": "CA", "name": "Alex Smith"},
        {"party": "R", "bioGuideId": "S2", "state": "TX", "name": "Jordan Smith"},
    ])
    text = (
        "Mr. SMITH of Texas. I rise.\n"
        "Mr. SMITH. I yield back.\n"
        "Mr. JONES. I object.\n"
    )
    turns = list(build_turns(
        text, members, "CREC-2025-01-01-pt1-PgH1", "2025-01-01", 119, "house"
    ))
    assert turns[0]["party"] == "R" and turns[0]["bioguide"] == "S2"
    assert turns[1]["party"] == "other" and not turns[1]["bioguide"]
    # An unmatched marker is not assigned to the sole/nearest metadata member.
    assert turns[2]["party"] == "other" and not turns[2]["bioguide"]


def test_missing_identity_fallback_preserves_metadata_and_ambiguity():
    members = [
        {"name": "Known, Member", "bioguide": "K1", "party": "D", "state": "CA"},
        {"name": "Delaney, Member", "bioguide": "", "party": "", "state": ""},
        {"name": "Smith, First", "bioguide": "S1", "party": "D", "state": "CA"},
        {"name": "Smith, Second", "bioguide": "S2", "party": "R", "state": "TX"},
    ]
    calls = []

    def lookup(marker):
        calls.append(marker)
        return {
            "name": marker, "bioguide": "F1", "party": "R", "state": "MD",
        }

    rows = list(build_turns(
        "Mr. KNOWN. Known identity.\n"
        "Mrs. MCCLAIN DELANEY. Incomplete identity.\n"
        "Mr. VAN EPPS. Missing identity.\n"
        "Mr. SMITH. Ambiguous identity.\n"
        "The SPEAKER. Procedural.\n",
        members, "CREC-2026-09-16-pt1-PgH1", "2026-09-16", 119, "house",
        member_lookup=lookup,
    ))
    assert calls == ["Mrs. MCCLAIN DELANEY", "Mr. VAN EPPS"]
    assert [r["bioguide"] for r in rows] == ["K1", "F1", "F1", "", ""]
    assert rows[0]["party"] == "D"
    assert rows[-1]["is_procedural"]


def test_non_spoken_senate_sections_cannot_be_attributed():
    members = [
        {"name": "Smith, Member", "bioguide": "S1", "party": "D", "state": "CA"},
        {"name": "Jones, Member", "bioguide": "J1", "party": "R", "state": "TX"},
    ]
    rows = list(build_turns(
        "Mr. SMITH. Before.\n"
        "\u2211 Mr. JONES. Submitted.\nMr. SMITH. Quoted in submission.\u2211\n"
        "Mr. SMITH. After.\n",
        members, "CREC-2026-09-16-pt1-PgS1", "2026-09-16", 119, "senate",
    ))
    assert [r["text"] for r in rows if r["bioguide"]] == ["Before.", "After."]
    excluded = [r for r in rows if r["is_procedural"]]
    assert len(excluded) == 2
    assert all(not r["bioguide"] and r["party"] == "other" for r in excluded)
    assert len({r["turn_id"] for r in rows}) == len(rows)


def test_non_spoken_classification_never_looks_up_a_member():
    def lookup(marker):
        raise AssertionError("submitted statements must not trigger roster lookup")

    rows = list(build_turns(
        "Mr. SMITH. Submitted without surviving bullet markup.",
        [], "CREC-2026-09-16-pt1-PgS1", "2026-09-16", 119, "senate",
        member_lookup=lookup, non_spoken=True,
    ))
    assert len(rows) == 1
    assert rows[0]["is_procedural"] and not rows[0]["bioguide"]


def test_wrapped_member_references_are_not_floor_markers():
    calls = []

    def lookup(marker):
        calls.append(marker)
        return {"bioguide": "W1", "name": marker, "party": "R", "state": "TX"}

    rows = list(build_turns(
        "ADDITIONAL SPONSORS\nMr. Weber of Texas.\nH.R. 381: Other names.\n"
        "Mr. McCONNELL. Actual floor remarks.\n",
        [], "CREC-2026-09-16-pt1-PgH1", "2026-09-16", 119, "house",
        member_lookup=lookup,
    ))
    assert calls == ["Mr. McCONNELL"]
    assert rows[0]["speaker_name"] == "" and not rows[0]["bioguide"]
    assert rows[1]["speaker_name"] == "Mr. McCONNELL"


def test_page_labels_are_not_counted_as_speech():
    rows = list(build_turns(
        "Mr. SMITH. First words.\n[[Page H123]]\n{time} 1040\nLast words.",
        [], "CREC-2026-09-16-pt1-PgH1", "2026-09-16", 119, "house",
    ))
    assert rows[0]["word_count"] == 4
    assert "Page" not in rows[0]["text"]
    assert "1040" not in rows[0]["text"]


def test_editorial_notes_and_headings_are_not_scored_as_speech():
    turns = list(build_turns(
        "Mr. SMITH. Before.\n\n"
        "                  COMMITTEE ON THE JUDICIARY\n\n"
        "  After.\n"
        "========================= NOTE =========================\n"
        "Mr. JONES. An editorial correction, not a floor speech.\n"
        "========================= END NOTE =========================",
        [], "CREC-2026-09-16-pt1-PgH1", "2026-09-16", 119, "house",
    ))
    assert len(turns) == 1
    assert turns[0]["word_count"] == 2
    assert "correction" not in turns[0]["text"]
    assert "COMMITTEE" not in turns[0]["text"]


def test_stage_directions_are_not_charged_to_the_preceding_member():
    members = [
        {"name": "Smith, Member", "bioguide": "S1", "party": "D", "state": "CA"},
        {"name": "Jones, Member", "bioguide": "J1", "party": "R", "state": "TX"},
    ]
    rows = list(build_turns(
        "Mr. SMITH. My remarks.\n"
        "(Ms. JONES asked and was given permission to address the House for 1 minute.)\n"
        "Ms. JONES. My separate remarks.",
        members, "CREC-2026-09-16-pt1-PgH1", "2026-09-16", 119, "house",
    ))
    assert [(r["bioguide"], r["text"]) for r in rows if r["bioguide"]] == [
        ("S1", "My remarks."), ("J1", "My separate remarks."),
    ]
    assert any(r["is_procedural"] and "permission" in r["text"] for r in rows)


def test_president_pro_tempore_marker_is_procedural() -> None:
    turns = list(build_turns(
        "The PRESIDENT pro tempore. The Senate will come to order.",
        [], "CREC-2025-01-01-pt1-PgS1", "2025-01-01", 119, "senate",
    ))
    assert len(turns) == 1
    assert turns[0]["speaker_name"] == "The PRESIDENT pro tempore"
    assert turns[0]["is_procedural"] is True


def test_parenthetical_presiding_marker_ends_member_turn() -> None:
    members = normalize_members([
        {
            "party": "R",
            "bioGuideId": "R000575",
            "state": "AL",
            "name": "Mike Rogers",
        },
    ])
    for marker in (
        "The SPEAKER pro tempore (Mr. Simpson)",
        "The Acting CHAIR (Mr. McDowell)",
        "The Acting CHAIR (Mr. Goldman of Texas)",
    ):
        text = (
            "Mr. ROGERS of Alabama. I yield back the balance of my time.\n"
            f"  {marker}. The text of the bill is as follows: "
            + ("legislative text " * 1_000)
        )
        turns = list(build_turns(
            text, members, "CREC-2025-12-10-pt1-PgH1", "2025-12-10", 119, "house",
        ))
        assert len(turns) == 2
        assert turns[0]["bioguide"] == "R000575"
        assert turns[0]["word_count"] == 8
        assert turns[1]["speaker_name"] == marker
        assert turns[1]["is_procedural"] is True
        assert not turns[1]["bioguide"]


def test_inserted_legislative_material_is_not_attributed_to_member() -> None:
    members = normalize_members([
        {
            "party": "R",
            "bioGuideId": "G000546",
            "state": "LA",
            "name": "Sam Graves",
        },
    ])
    text = (
        "Mr. GRAVES. Madam Speaker, I move to suspend the rules and pass the bill.\n"
        "  The Clerk read the title of the bill.\n"
        "  The text of the bill is as follows: "
        + ("legislative text " * 1_000)
    )
    turns = list(build_turns(
        text, members, "CREC-2025-07-23-pt1-PgH1", "2025-07-23", 119, "house",
    ))
    assert len(turns) == 2
    assert turns[0]["bioguide"] == "G000546"
    assert turns[0]["word_count"] == 12
    assert turns[1]["speaker_name"] == "Inserted material"
    assert turns[1]["is_procedural"] is True
    assert not turns[1]["bioguide"]


def test_material_printed_by_unanimous_consent_is_not_attributed() -> None:
    members = normalize_members([
        {
            "party": "D",
            "bioGuideId": "S000148",
            "state": "NY",
            "name": "Charles Schumer",
        },
    ])
    text = (
        "Mr. SCHUMER. Mr. President, I ask unanimous consent that the text of the "
        "bill be printed in the Record.\n"
        "  There being no objection, the text of the bill was ordered to be printed "
        "in the Record, as follows: "
        + ("legislative text " * 1_000)
    )
    turns = list(build_turns(
        text, members, "CREC-2026-07-30-pt1-PgS1", "2026-07-30", 119, "senate",
    ))
    assert len(turns) == 2
    assert turns[0]["bioguide"] == "S000148"
    assert turns[0]["word_count"] == 17
    assert turns[1]["is_procedural"] is True
    assert not turns[1]["bioguide"]


def test_indented_inserted_letter_is_not_scored_as_floor_remarks():
    turns = list(build_turns(
        "Mr. SMITH. I include the following letter in the Record.\n\n"
        "                         September 14, 2026.\n"
        "     Dear Committee Members:\n"
        "       Printed letter, not spoken floor remarks.\n",
        [{"name": "Smith, Member", "bioguide": "S1", "party": "D", "state": "CA"}],
        "CREC-2026-09-16-pt1-PgH1", "2026-09-16", 119, "house",
    ))
    assert len(turns) == 2
    assert turns[0]["text"] == "I include the following letter in the Record."
    assert turns[0]["bioguide"] == "S1"
    assert turns[1]["is_procedural"] and not turns[1]["bioguide"]


def test_single_centered_editorial_heading_does_not_hide_floor_remarks():
    turns = list(build_turns(
        "Mr. SMITH. First topic.\n\n"
        "                             Next Topic\n\n"
        "  My floor remarks continue.\n",
        [{"name": "Smith, Member", "bioguide": "S1", "party": "D", "state": "CA"}],
        "CREC-2026-09-16-pt1-PgH1", "2026-09-16", 119, "house",
    ))
    assert len(turns) == 1
    assert turns[0]["text"].endswith("My floor remarks continue.")


def test_fuzzy_keyword_matching() -> None:
    from analysis.score.scorers import morph_variants, plural_variants
    assert {"colleague", "colleagues"}.issubset(morph_variants("colleague"))
    assert "gentlemen" in plural_variants("gentleman")   # irregular plural
    assert "ladies" in plural_variants("lady")           # y -> ies
    fuzzy, exact = Scorers(fuzzy=True), Scorers(fuzzy=False)
    # Courtesy plurals match under fuzzy and miss under exact matching.
    for txt in ("my distinguished colleagues", "the gentlemen from Ohio",
                "the gentlewomen from California"):
        assert fuzzy.score_turn(txt)["formal_courtesy_hits"] >= 1
        assert exact.score_turn(txt)["formal_courtesy_hits"] == 0
    neutral = "the committee will now consider the appropriations schedule for review"
    assert fuzzy.score_turn(neutral) == {
        "n_words": 10, "formal_courtesy_hits": 0, "profanity_mild": 0,
        "profanity_strong": 0, "profanity_hits": 0, "ethnic_slur_hits": 0,
    }
    # Profanity and slur codebooks are exact even when courtesy matching is fuzzy.
    sc = fuzzy.score_turn("he fucked up the whole vote")
    assert sc["profanity_hits"] == 1
    assert sc["profanity_mild"] == 0 and sc["profanity_strong"] == 1
    assert fuzzy.score_turn("we viewed the bill through that lens today")["profanity_hits"] == 0


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


def test_scorer_formal_courtesy() -> None:
    s = Scorers()
    assert s.score_turn("The distinguished gentlewoman from Ohio has the floor.")[
        "formal_courtesy_hits"
    ] >= 1
    assert s.score_turn("I yield to the gentleman from Texas.")["formal_courtesy_hits"] >= 1
    assert s.score_turn("The bill funds highway repairs.")["formal_courtesy_hits"] == 0


def test_scorer_profanity_tiers() -> None:
    s = Scorers()
    r = s.score_turn("what the hell is this damn nonsense")
    assert r["profanity_mild"] >= 2  # hell + damn
    assert r["profanity_hits"] >= 2


def test_scorer_conservative_profanity_expansion() -> None:
    scorer = Scorers()
    text = (
        "That bitching bastard called this a shitshow run by dipshits and douchebags. "
        "The cocksucker made it a motherfucking clusterfuck and a pain in the ass."
    )
    scored = scorer.score_turn(text)
    assert scored["profanity_strong"] == 9
    assert scored["profanity_hits"] == 9
    assert scorer.profanity_term_counts(text) == {
        "ass": 1,
        "bastard": 1,
        "bitching": 1,
        "clusterfuck": 1,
        "cocksucker": 1,
        "dipshits": 1,
        "douchebags": 1,
        "motherfucking": 1,
        "shitshow": 1,
    }


def test_scorer_still_excludes_ambiguous_profanity_candidates() -> None:
    scorer = Scorers()
    text = (
        "Dick chaired the medical panel on breasts and reproductive anatomy. "
        "The rooster cocked its head as the player kicked the balls."
    )
    assert scorer.score_turn(text)["profanity_hits"] == 0


def test_scorer_excludes_neutral_topics_from_profanity_and_slurs() -> None:
    s = Scorers()
    for text in (
        "organ transplant legislation", "sex trafficking bill", "murder victims",
        "gay rights legislation", "In God We Trust", "erected a memorial", "strips funding",
    ):
        scored = s.score_turn(text)
        assert scored["profanity_hits"] == 0 and scored["ethnic_slur_hits"] == 0


def test_scorer_counts_ethnic_slurs_separately_from_profanity() -> None:
    s = Scorers()
    scored = s.score_turn("He was called a wetback and a sand nigger. Damn.")
    # "sand nigger" is one phrase match, not a phrase plus its component word.
    assert scored["ethnic_slur_hits"] == 2
    assert scored["profanity_hits"] == 1
    assert s.signal_spans("a slant-eyed insult")["ethnic_slur"] == [(2, 12)]


def test_scorer_leaves_ambiguous_slur_homographs_unscored() -> None:
    s = Scorers()
    for text in (
        "The chink in the armor", "a raccoon, or coon, in Squaw Valley",
        "the Washington Redskins", "Jim Crow laws", "that spick and span office",
        "the Half-Breeds faction", "honky-tonk music", "the Negro Leagues",
    ):
        assert s.score_turn(text)["ethnic_slur_hits"] == 0, text


def test_scorer_contextual_false_positive_exclusions() -> None:
    s = Scorers()
    assert s.score_turn("The bill authorized a lock and damn.")["profanity_hits"] == 0
    assert s.score_turn("This is a Federal crap game.")["profanity_hits"] == 0
    assert s.score_turn("This is one damn bad bill.")["profanity_hits"] == 1


def test_govinfo_helpers() -> None:
    assert _congress_from_date("2017-01-03") == 115
    assert _congress_from_date("2024-01-09") == 118
    assert _congress_from_date("1995-02-01") == 104
    assert _surname("Smith, Jane Q.") == "SMITH"
    assert _surname("Jane Q. Smith") == "SMITH"
    assert _speaker_surname("Mr. McCONNELL") == "MCCONNELL"
    assert _speaker_surname("Ms. PELOSI of California") == "PELOSI"
    assert _speaker_state("Ms. PELOSI of California") == "CA"
    assert _speaker_state("Mr. NADLER of New York") == "NY"
    assert _congress_from_row({"congress": "118", "dateIssued": "2025-01-01"}) == 118


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
