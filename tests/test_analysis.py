"""Offline unit tests for the analysis pipeline (no network, no large data)."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

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
from analysis.speakers import PROFANITY_TERM_FAMILIES, grouped_profanity_terms  # noqa: E402


def test_relocated_adjudication_validator_finds_batches_from_another_directory(
    tmp_path, monkeypatch,
):
    import json
    import runpy
    import shutil

    root = Path(__file__).resolve().parents[1]
    archive = tmp_path / "adjudication"
    archive.mkdir()
    script = archive / "validate_result.py"
    shutil.copy2(root / "data/validation/adjudication/validate_result.py", script)
    module = runpy.run_path(str(script))
    (archive / "input").mkdir()
    (archive / "results").mkdir()
    (archive / "input/batch_00.json").write_text(json.dumps([{"sample_id": "sample"}]))
    result = {
        **{key: sorted(values)[0] for key, values in module["ALLOWED"].items()},
        "sample_id": "sample",
        "rationale": "Test annotation.",
    }
    (archive / "results/batch_00.json").write_text(json.dumps([result]))
    monkeypatch.chdir(tmp_path)
    assert module["main"](0) == 0


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
        "formal_courtesy_per_1k", "profanity_per_1k", "slurs_per_1k",
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


def test_indented_quotes_do_not_swallow_the_members_resumed_remarks():
    turns = list(build_turns(
        "Mr. SMITH. Let me read the letter.\n\n"
        "       The author used fucking and bullshit in the letter.\n"
        "     This is still the letter, not the member speaking.\n\n"
        "  Now I resume my own remarks. This is damn important.\n"
        "I am not quoting the letter here.\n\n"
        "       Another excerpt from the same letter follows.\n"
        "     This indented line is also inserted material.\n\n"
        "  He did one hell of a job.\n",
        [{"name": "Smith, Member", "bioguide": "S1", "party": "D", "state": "CA"}],
        "CREC-1994-05-25-pt1-PgS8", "1994-05-25", 103, "senate",
    ))
    assert len(turns) == 5
    spoken = [turn for turn in turns if not turn["is_procedural"]]
    inserted = [turn for turn in turns if turn["is_procedural"]]
    assert all(turn["bioguide"] == "S1" for turn in spoken)
    assert all(not turn["bioguide"] for turn in inserted)
    assert len({turn["turn_id"] for turn in turns}) == len(turns)
    counts = Scorers().profanity_term_counts(" ".join(turn["text"] for turn in spoken))
    assert counts == {"damn": 1, "hell of a": 1}
    assert "fucking" not in " ".join(turn["text"] for turn in spoken)


def test_bill_introduction_after_an_indented_block_is_not_resumed_floor_speech():
    turns = list(build_turns(
        "Mr. JEFFORDS. I conclude my own remarks.\n\n"
        "      By Mr. STEVENS:\n"
        "  S. 1924. A bill to authorize documentation for the vessel Damn Yankee;\n"
        "to the Committee on Commerce, Science, and Transportation.\n"
        "\u2211 Mr. STEVENS. Submitted explanation of the bill.\u2211\n"
        "  Mr. JEFFORDS. Now I resume my own remarks. This is damn important.\n",
        [
            {"name": "Jeffords, James", "bioguide": "J1", "party": "R", "state": "VT"},
            {"name": "Stevens, Ted", "bioguide": "S1", "party": "R", "state": "AK"},
        ],
        "CREC-1996-06-28-pt1-PgS7302-2", "1996-06-28", 104, "senate",
    ))
    spoken = [turn for turn in turns if not turn["is_procedural"]]
    assert len(spoken) == 2
    assert all(turn["bioguide"] == "J1" for turn in spoken)
    assert "Damn Yankee" not in " ".join(turn["text"] for turn in spoken)
    assert spoken[-1]["text"].endswith("This is damn important.")
    assert any("S. 1924." in turn["text"] and turn["is_procedural"] for turn in turns)


def test_single_indented_line_after_a_quotation_cue_is_not_the_members_own_words():
    turns = list(build_turns(
        "Mr. SMITH. The witness described it as:\n\n"
        "     This is fucking bullshit.\n\n"
        "  I disagree. This is damn serious.\n",
        [{"name": "Smith, Member", "bioguide": "S1", "party": "D", "state": "CA"}],
        "CREC-2004-04-22-pt1-PgS4334-2", "2004-04-22", 108, "senate",
    ))
    spoken = [turn for turn in turns if not turn["is_procedural"]]
    assert len(spoken) == 2
    assert all(turn["bioguide"] == "S1" for turn in spoken)
    assert Scorers().profanity_term_counts(" ".join(turn["text"] for turn in spoken)) == {
        "damn": 1,
    }
    assert any(
        turn["is_procedural"] and "fucking bullshit" in turn["text"] for turn in turns
    )


def test_missing_period_before_explicit_floor_address_starts_a_new_member_turn():
    turns = list(build_turns(
        "Mr. GOSS. I include this chart in the Record.\n\n"
        "       The first printed row in the chart.\n"
        "     The second printed row in the chart.\n\n"
        "  Mr. SOLOMON Mr. Speaker, I thank the gentleman for yielding.\n"
        "  The rest of the House be damned.\n",
        [
            {"name": "Goss, Porter", "bioguide": "G1", "party": "R", "state": "FL"},
            {"name": "Solomon, Gerald", "bioguide": "S1", "party": "R", "state": "NY"},
        ],
        "CREC-1994-10-05-pt1-PgH14", "1994-10-05", 103, "house",
    ))
    matched = [turn for turn in turns if "damned" in turn["text"]]
    assert len(matched) == 1
    assert matched[0]["speaker_name"] == "Mr. SOLOMON"
    assert matched[0]["bioguide"] == "S1"
    assert not matched[0]["is_procedural"]


def test_short_resumed_remarks_deduplicate_only_with_the_same_original_segment():
    from analysis.ingest.govinfo_bulk import _turn_fingerprint

    def resumed(prefix):
        turns = list(build_turns(
            f"Mr. SMITH. {prefix}\n\n"
            "       Inserted letter with a first line.\n"
            "     Inserted letter with a second line.\n\n"
            "  This is damn wrong.\n",
            [{"name": "Smith, Member", "bioguide": "S1", "party": "D", "state": "CA"}],
            "CREC-2025-01-06-pt1-PgH1", "2025-01-06", 119, "house",
        ))
        return turns[-1]

    first = resumed("Substantive remarks " * 50)
    duplicate = resumed("Substantive remarks " * 50)
    separate = resumed("A different set of substantive remarks " * 50)
    assert len(first["text"]) < 500
    assert _turn_fingerprint(first)
    assert _turn_fingerprint(first) == _turn_fingerprint(duplicate)
    assert _turn_fingerprint(first) != _turn_fingerprint(separate)


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
        "profanity_strong": 0, "profanity_hits": 0, "slur_hits": 0,
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


@pytest.mark.parametrize(("family", "form"), [
    (family, form)
    for family, forms in PROFANITY_TERM_FAMILIES.items()
    for form in forms
])
def test_every_profanity_form_is_counted_and_grouped_consistently(family, form) -> None:
    scorer = Scorers()
    for text, count in [(f"This is {form}.", 1), (form.upper(), 1), (f"{form}; {form}", 2)]:
        assert scorer.score_turn(text)["profanity_hits"] == count
        assert scorer.profanity_term_counts(text) == {form: count}
        assert grouped_profanity_terms(scorer.profanity_term_counts(text)) == {family: count}


@pytest.mark.parametrize(("text", "expected"), [
    ("god damn-like", {}),
    ("god damn's", {}),
    ("prefix-fuck off", {}),
    ("prefix'fuck off", {}),
    ("mother fucker-like", {}),
    ("kick ass's", {}),
    ("shit bag-like", {"shit": 1}),
    ("god damn-like damn", {"damn": 1}),
    ("'fuck off'", {"fuck off": 1}),
    ("god damn-", {"god damn": 1}),
    ("A lock and damn-like policy is damn wrong.", {"damn": 1}),
])
def test_phrase_boundaries_agree_with_whole_token_counting(text, expected):
    scorer = Scorers()
    assert scorer.profanity_term_counts(text) == expected
    assert scorer.score_turn(text)["profanity_hits"] == sum(expected.values())


@pytest.mark.parametrize("text", [
    "William the Bastard brought feudalism to England.",
    "We need information to asses the fees established by the Commission.",
    "The workers were building this damn, and needed doctors.",
    "PAEs should asses the need for affordable housing.",
    "The agency was directed to asses all regulatory provisions.",
    "The analysts interpret and asses that intelligence.",
    "These are so-called bastard LATA's in telecommunications.",
    "They were known as the battling bastards of Bataan.",
    "He was not observed riding a jackass.",
    "A father will hunt buggers, as they used to say; buggers or monsters.",
    "They played at the crap tables.",
    "This was a crap shooting deal.",
    "The substitute offers a cheap crap shoot, with all bets off.",
    "Trout take san juan worms, wooly buggers, and Joe's Hoppers.",
    "They fish with woolly buggers.",
    "The poison has burst the damn and flooded the suburbs.",
    "Damn removal will irreversibly remove jobs, harm the economy and environment.",
    "Ohio fans cheer The Best Damn Band in the Land.",
    "Mercury affects waterfowl such ass loons.",
    "The wind-fueled blaze damned more than 30 buildings.",
    "If the ox or the ass were in the pit, pull him out.",
    "Communities are unable to asses or challenge the recommendations.",
    "Did the Pentagon accurately asses both bases?",
    "The EPA ignored demands to asses such injuries.",
    "The program is evaluated to asses its effectiveness.",
    "The documents help them asses their own effectiveness.",
    "He was a pilot of a B-17, the Damn Yankee, in World War II.",
    "They teamed up with Michael Oxley ass the Sarbanes-Oxley legislation.",
    "These people and the battered bastards of Bastogne died for freedom.",
])
def test_verified_literal_and_transcription_contexts_are_not_curses(text):
    scorer = Scorers()
    assert scorer.score_turn(text)["profanity_hits"] == 0
    assert scorer.profanity_term_counts(text) == {}
    assert scorer.signal_spans(text)["profanity"] == []


@pytest.mark.parametrize(("text", "expected"), [
    ("They should asses the proposal. Those asses ignored it.", {"asses": 1}),
    ("The battling bastards of Bataan fought those bastards.", {"bastards": 1}),
    ("They hunt buggers and run these buggers out of business.", {"buggers": 2}),
    ("He was riding a jackass. Stop being a jackass.", {"jackass": 1}),
    ("I will not defer to asses or fools, or to asses such as those.", {"asses": 2}),
    ("A B-17, the Damn Yankee, fought in the war. We damn Yankees remember it.", {"damn": 1}),
    ("The battered bastards of Bastogne fought those bastards.", {"bastards": 1}),
    ("They worked with Michael Oxley ass the Sarbanes-Oxley bill. My ass.", {"ass": 1}),
])
def test_new_literal_filters_preserve_separate_actual_curses(text, expected):
    scorer = Scorers()
    assert scorer.profanity_term_counts(text) == expected
    assert scorer.score_turn(text)["profanity_hits"] == sum(expected.values())


def test_content_addressed_ocr_review_preserves_other_curses_and_word_denominators():
    text = "The final cunt was twenty votes. That bastard lied."
    start = text.index("cunt")
    review = {
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "start": start, "end": start + 4, "matched_text": "cunt",
        "form": "cunt", "tier": "strong", "source_turn_id": "hein_bound:example",
        "reason": "The vote-count noun is an OCR error, not a vulgar word.",
    }
    baseline = Scorers(profanity_contexts=[]).score_turn(text)
    scorer = Scorers(profanity_contexts=[review])
    corrected = scorer.score_turn(text)
    assert scorer.profanity_term_counts(text) == {"bastard": 1}
    assert corrected["profanity_hits"] == corrected["profanity_strong"] == 1
    assert corrected["n_words"] == baseline["n_words"]
    assert corrected["formal_courtesy_hits"] == baseline["formal_courtesy_hits"]
    assert corrected["slur_hits"] == baseline["slur_hits"]
    assert scorer.signal_spans(text)["profanity"] == [(text.index("bastard"), text.index("bastard") + 7)]
    assert scorer.score_turn("That is a cunt.")["profanity_hits"] == 1
    with pytest.raises(ValueError, match="no longer matches"):
        Scorers(profanity_contexts=[{**review, "start": start + 1, "end": start + 5}]).score_turn(text)


def test_reviewed_original_offsets_remain_correct_when_casefolding_expands_earlier_text():
    from scripts.audit_profanity_families import ReferenceMatcher, inspect_turn

    text = "İİİİİ cunt is an OCR error. That bastard lied."
    start = text.index("cunt")
    scorer = Scorers(profanity_contexts=[{
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "start": start, "end": start + 4, "matched_text": "cunt",
        "form": "cunt", "tier": "strong", "source_turn_id": "hein_bound:example",
        "reason": "Verified source-context fixture.",
    }])
    row = {
        "turn_id": "hein_bound:example", "text": text, "party": "D",
        "chamber": "house", "bioguide": "",
    }
    counts, evidence, mismatch = inspect_turn(row, ReferenceMatcher(), scorer, "aggregate")
    assert mismatch is None
    assert counts == {"bastard": 1}
    assert evidence[0]["disposition"] == "literal_context_exclusion"
    assert scorer.profanity_term_counts(text) == counts


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
        assert scored["profanity_hits"] == 0 and scored["slur_hits"] == 0


def test_scorer_counts_slurs_separately_from_profanity() -> None:
    s = Scorers()
    scored = s.score_turn("He was called a wetback and a sand nigger. Damn.")
    # "sand nigger" is one phrase match, not a phrase plus its component word.
    assert scored["slur_hits"] == 2
    assert scored["profanity_hits"] == 1
    assert s.signal_spans("a slant-eyed insult")["slur"] == [(2, 12)]


def test_scorer_counts_orientation_and_disability_slurs() -> None:
    s = Scorers()
    for text, expected in (
        ("He called him a faggot.", 1), ("a fag hag", 1), ("a bull dyke", 1),
        ("tranny and shemale", 2), ("You retard.", 1), ("those retards", 1),
        ("that spaz", 1), ("window lickers", 1),
    ):
        assert s.score_turn(text)["slur_hits"] == expected, text


def test_scorer_excludes_verb_archaic_and_fragment_uses_of_slur_forms() -> None:
    s = Scorers()
    for text in (
        "This tax will retard the growth of the economy.",
        "it retards the development of the West", "tends to retard progress",
        "a faggot of sticks", "the faggot votes in the borough", "the fag end of the session",
        "homo sapiens", "the homo- geneous population", "re- tard",
    ):
        assert s.score_turn(text)["slur_hits"] == 0, text
        assert s.signal_spans(text)["slur"] == [], text
    # An excluded context does not hide a genuine slur elsewhere in the same turn.
    assert s.score_turn("Taxes retard the growth. You retard.")["slur_hits"] == 1


def test_scorer_leaves_ambiguous_slur_homographs_unscored() -> None:
    s = Scorers()
    for text in (
        "The chink in the armor", "a raccoon, or coon, in Squaw Valley",
        "the Washington Redskins", "Jim Crow laws", "that spick and span office",
        "the Half-Breeds faction", "honky-tonk music", "the Negro Leagues",
        "the queer community", "Senator Gaylord Nelson", "the mentally retarded",
        "spastic paralysis", "cripple the economy", "midget submarine",
        "the dyke broke near Van Dyke", "refugees on Lesbos",
    ):
        assert s.score_turn(text)["slur_hits"] == 0, text


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
