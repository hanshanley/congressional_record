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
    scored = Scorers().score_turn(
        "I thank my Republican colleague, but that dishonest claim alleges bribery. Damn.",
        "D",
    )
    assert set(SCORE_KEYS).issubset(scored)


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


def test_fuzzy_keyword_matching() -> None:
    from analysis.score.scorers import morph_variants, plural_variants
    # morphological variants for single words
    assert {"coward", "cowards"}.issubset(morph_variants("coward"))
    assert "corrupting" in morph_variants("corrupt")
    assert "gentlemen" in plural_variants("gentleman")   # irregular plural
    assert "ladies" in plural_variants("lady")           # y -> ies
    fuzzy, exact = Scorers(fuzzy=True), Scorers(fuzzy=False)
    # plurals/verb-forms match under fuzzy, miss under exact
    for txt, lex in [("my distinguished colleagues", "comity"),
                     ("the gentlemen from Ohio", "comity"),
                     ("cowards and liars corrupting things", "hostility")]:
        assert fuzzy.score_turn(txt, "D")[f"{lex}_hits"] > exact.score_turn(txt, "D")[f"{lex}_hits"]
    # fuzzy must not fabricate hits in clean neutral text
    neutral = "the committee will now consider the appropriations schedule for review"
    r = fuzzy.score_turn(neutral, "D")
    assert r["hostility_hits"] == 0 and r["profanity_hits"] == 0

    # Phrase inflection must inflect the *correct* content word, not just the last one:
    # "reach across the aisle" -> "reaches/reached across the aisle" (leading verb).
    for txt in ["She reaches across the aisle", "He worked across the aisle"]:
        assert fuzzy.score_turn(txt, "D")["comity_hits"] >= 1
        assert exact.score_turn(txt, "D")["comity_hits"] == 0

    # Curated profanity surface forms are exact and assigned to one severity tier.
    sc = fuzzy.score_turn("he fucked up the whole vote", "D")
    assert sc["profanity_hits"] == 1
    assert sc["profanity_mild"] == 0 and sc["profanity_strong"] == 1

    # Short obfuscation stubs (< 4 chars) are matched literally, never expanded into
    # ordinary English words: "len" (a lexicon entry) must not expand to match "lens".
    assert fuzzy.score_turn("we viewed the bill through that lens today", "D")["profanity_hits"] == 0

    # Explicit and fuzzy phrase variants that cover the same span are counted once.
    assert fuzzy.score_turn("reaching across the aisle", "D")["comity_hits"] == 1
    assert fuzzy.score_turn(
        "reaching across the aisle and then reached across the aisle", "D"
    )["comity_hits"] == 2

    # Gendered comity must be matched symmetrically across gentleman/gentlewoman/gentlelady
    # (gender is not a morphological inflection), and fuzzy must catch their plurals too.
    for male, fem, lady in [
        ("I thank the gentleman.", "I thank the gentlewoman.", "I thank the gentlelady."),
        ("I appreciate the gentleman.", "I appreciate the gentlewoman.", "I appreciate the gentlelady."),
    ]:
        assert fuzzy.score_turn(male, "D")["comity_hits"] >= 1
        assert fuzzy.score_turn(fem, "D")["comity_hits"] >= 1
        assert fuzzy.score_turn(lady, "D")["comity_hits"] >= 1
    # fuzzy (not exact) recovers the plural gendered address forms
    assert fuzzy.score_turn("the gentlewomen from California", "D")["comity_hits"] >= 1
    assert exact.score_turn("the gentlewomen from California", "D")["comity_hits"] == 0

    s = Scorers(use_sentiment=True)
    hostile = s.score_turn("This is a corrupt, shameful lie. He is a coward and a fraud.", "D")
    civil = s.score_turn("I thank the distinguished gentleman and commend my friend.", "D")
    assert hostile["sentiment"] < 0 < civil["sentiment"]
    assert 0.0 <= hostile["neg_share"] <= 1.0
    assert hostile["neg_share"] > civil["neg_share"]
    # Late-speech negativity is not lost to truncation: a long positive preamble with a
    # trailing negative sentence still yields a non-zero negative share.
    long_tail = ("Thank you, Madam Speaker. " * 400) + "This bill is a corrupt, shameful disgrace."
    assert s.score_turn(long_tail, "D")["neg_share"] > 0




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
    assert r["formal_courtesy_hits"] >= 1
    assert r["gratitude_praise_hits"] >= 1
    assert r["cooperation_hits"] == 0
    assert r["comity_hits"] == (
        r["formal_courtesy_hits"] + r["gratitude_praise_hits"] + r["cooperation_hits"]
    )
    assert r["hostility_hits"] == 0

    r2 = s.score_turn("That deceitful, cowardly liar is a hypocrite.", "R")
    assert r2["hostility_hits"] >= 4
    assert r2["misconduct_hits"] == 0


def test_scorer_separates_comity_components() -> None:
    s = Scorers()
    formal = s.score_turn("The distinguished gentlewoman from Ohio has the floor.", "D")
    gratitude = s.score_turn("I thank my colleague for this work.", "D")
    cooperation = s.score_turn("We reached across the aisle in a bipartisan spirit.", "D")
    assert formal["formal_courtesy_hits"] >= 1
    assert gratitude["gratitude_praise_hits"] >= 1
    assert cooperation["cooperation_hits"] >= 1


def test_scorer_profanity_tiers() -> None:
    s = Scorers()
    r = s.score_turn("what the hell is this damn nonsense", "D")
    assert r["profanity_mild"] >= 2  # hell + damn
    assert r["profanity_hits"] >= 2


def test_scorer_separates_neutral_topics_and_discourse_categories() -> None:
    s = Scorers()
    for text in (
        "organ transplant legislation", "sex trafficking bill", "murder victims",
        "gay rights legislation", "In God We Trust", "erected a memorial", "strips funding",
    ):
        assert s.score_turn(text, "D")["profanity_hits"] == 0

    scored = s.score_turn(
        "That deceitful coward committed bribery, according to this allegation, "
        "and promoted a socialist policy. What the hell.",
        "D",
    )
    assert scored["hostility_hits"] >= 2
    assert scored["misconduct_hits"] >= 1
    assert scored["ideological_label_hits"] >= 1
    assert scored["profanity_hits"] >= 1


def test_scorer_outgroup_directed_and_pejorative() -> None:
    s = Scorers()
    # A Democrat attacking Republicans near an out-group reference.
    txt = "My Republican colleagues are deceitful cowards on this bill."
    r = s.score_turn(txt, "D")
    assert r["outgroup_refs"] >= 1               # "republican" is out-group for a D
    assert r["directed_hostility_hits"] >= 2     # dishonest + cowards near the ref
    assert r["outgroup_hostility_contexts"] == 1
    separated = s.score_turn(
        "My Republican colleagues support this bill. That unrelated witness is a liar.",
        "D",
    )
    assert separated["outgroup_refs"] == 1
    assert separated["outgroup_hostility_contexts"] == 0

    # Same words but speaker is Republican -> "republican" is NOT out-group.
    r2 = s.score_turn(txt, "R")
    assert r2["outgroup_refs"] == 0

    # Generic democratic-government language is not a party reference, while a
    # party-specific Democratic noun phrase is.
    assert s.score_turn("We defend democratic institutions and values.", "R")["outgroup_refs"] == 0
    assert s.score_turn("My Democratic colleagues support the bill.", "R")["outgroup_refs"] == 1

    # "Democrat party" pejorative marker.
    r3 = s.score_turn("The Democrat party wants to raise your taxes.", "R")
    assert r3["democrat_party_pej"] == 1


def test_scorer_contextual_false_positive_exclusions() -> None:
    s = Scorers()
    assert s.score_turn("She serves in a bipartisan commission.", "D")["cooperation_hits"] == 0
    assert s.score_turn("The phony price was listed in the estimate.", "D")["hostility_hits"] == 0
    assert s.score_turn("He is not a liar.", "D")["hostility_hits"] == 0
    assert s.score_turn("The Foreign Corrupt Practices Act applies.", "D")["misconduct_hits"] == 0
    assert s.score_turn("There is no corruption in this program.", "D")["misconduct_hits"] == 0
    assert s.score_turn("There was no bribery or perjury.", "D")["misconduct_hits"] == 0
    assert s.score_turn("The inquiry found no abuse of power.", "D")["misconduct_hits"] == 0
    assert s.score_turn("There is no doubt bribery occurred.", "D")["misconduct_hits"] == 1
    assert s.score_turn("The committee condemned the corrupt official.", "D")["misconduct_hits"] == 1
    assert s.score_turn("The bill authorized a lock and damn.", "D")["profanity_hits"] == 0
    assert s.score_turn("This is a Federal crap game.", "D")["profanity_hits"] == 0
    assert s.score_turn("This is one damn bad bill.", "D")["profanity_hits"] == 1
    assert s.score_turn("The majority party scheduled the vote.", "D")["outgroup_refs"] == 0
    assert s.score_turn("The Democratic conference met today.", "R")["outgroup_refs"] == 1
    context = s.score_turn("Democrats committed no bribery.", "R")
    assert context["directed_misconduct_hits"] == 0
    assert context["outgroup_misconduct_contexts"] == 0


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
