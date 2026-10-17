"""Tests for the per-speaker leaderboard pipeline.

This feature names individuals, so the attribution safeguards are the part that
matters most: quoted speech must not be charged to the member reading it, turns
without a stable id must not be guessed at, and small samples must not be allowed
to top a rate ranking.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.score.scorers import Scorers  # noqa: E402
from analysis.speakers import (  # noqa: E402
    LANGUAGE_METRICS,
    PROFANITY_TERM_FAMILIES,
    congress_plot_year,
    incomplete_profanity_term_rows,
    language_member_rates,
    language_timeseries,
    leaderboard,
    load_daily,
    mask_quotations,
    merge_daily,
    parse_profanity_terms,
    profanity_term_member_counts,
    profanity_term_leaders,
    replace_daily_window,
    save_daily,
    speaker_counts,
    timeseries,
)
from analysis.ingest.schema import ARROW_SCHEMA  # noqa: E402

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402


# ------------------------------------------------------------------ quotations


@pytest.mark.parametrize("floor_date,expected", [
    ("2025-12-31", 2025), ("2026-09-30", 2026), ("2027-01-02", 2026),
])
def test_congress_endpoint_uses_floor_dates_without_inventing_years(floor_date, expected):
    daily = pd.DataFrame([
        {"congress": 119, "date": floor_date, "chamber": "house", "party": "D"},
        {"congress": 119, "date": "2027-01-02", "chamber": "extensions", "party": "R"},
        {"congress": 120, "date": "2027-01-03", "chamber": "senate", "party": "R"},
    ])
    assert congress_plot_year(119, daily) == expected
    assert congress_plot_year(119, None) == 2025
    assert congress_plot_year(118, daily) == 2023


def test_mask_quotations_splits_spoken_from_quoted():
    spoken, quoted = mask_quotations("I said ``this is damn hard'' yesterday.")
    assert "damn" not in spoken
    assert "damn" in quoted
    # Masking preserves length so surrounding offsets stay valid.
    assert len(spoken) == len("I said ``this is damn hard'' yesterday.")
    assert spoken.startswith("I said")
    assert spoken.rstrip().endswith("yesterday.")


@pytest.mark.parametrize("opening,closing", [
    ("``", "''"), ("\u2018\u2018", "\u2019\u2019"), ("\u201c", "\u201d"),
])
def test_html_and_pdf_quote_styles_are_masked_identically(opening, closing):
    text = f"I said {opening}this is damn hard{closing} yesterday."
    spoken, quoted = mask_quotations(text)
    assert len(spoken) == len(text)
    assert spoken.split() == ["I", "said", "yesterday."]
    assert quoted == "this is damn hard"
    assert mask_quotations(f"{opening}an unmatched damn quote")[1] == ""


def test_text_without_quotes_is_returned_unchanged():
    text = "This is damn hard."
    assert mask_quotations(text) == (text, "")


def test_unterminated_quote_does_not_swallow_the_speech():
    # A stray opener must not blank out everything after it.
    spoken, quoted = mask_quotations("``unclosed and then damn appears")
    assert "damn" in spoken
    assert quoted == ""


def test_multiple_quotations_are_all_masked():
    spoken, quoted = mask_quotations("``damn one'' middle ``crap two'' end")
    assert "damn" not in spoken and "crap" not in spoken
    assert "middle" in spoken and "end" in spoken
    assert "damn" in quoted and "crap" in quoted


# ------------------------------------------------------------------- counting


def _row(**kw):
    base = {
        "turn_id": "t1", "source": "govinfo", "congress": 119, "year": 2025,
        "date": "2025-03-04", "chamber": "house", "session": None,
        "speaker_id": None, "speaker_name": "Mr. TEST", "party": "D", "state": "CA",
        "text": "hello", "is_procedural": False,
    }
    base.update(kw)
    return base


def _write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows, schema=ARROW_SCHEMA)
    # bioguide is carried alongside the base schema in the real corpus.
    pq.write_table(table, path)
    return path


@pytest.fixture()
def turn_file(tmp_path: Path):
    def _make(rows: list[dict]) -> Path:
        frame = pd.DataFrame(rows)
        path = tmp_path / "govinfo_bulk_119.parquet"
        frame.to_parquet(path, index=False)
        return path
    return _make


def test_quoted_profanity_is_not_charged_to_the_speaker(turn_file):
    path = turn_file([
        _row(turn_id="a", bioguide="A000001",
             text="The witness told us ``this is a damn disgrace'' and I agree."),
    ])
    counts = speaker_counts([path])
    assert len(counts) == 1
    assert counts.iloc[0]["profanity_hits"] == 0
    assert counts.iloc[0]["profanity_quoted_hits"] == 1
    assert json.loads(counts.iloc[0]["profanity_terms"]) == {}


def test_unquoted_profanity_is_counted(turn_file):
    path = turn_file([_row(turn_id="a", bioguide="A000001", text="This is a damn disgrace.")])
    counts = speaker_counts([path])
    assert counts.iloc[0]["profanity_hits"] == 1
    assert counts.iloc[0]["profanity_quoted_hits"] == 0


def test_reported_radio_dialogue_is_not_charged_to_the_member(turn_file):
    text = (
        "I am damn concerned about this recording.\n"
        "  The MiG-29: Give us authorization, damn it.\n"
        "  Military Control: Fire.\n"
        "  MiG-29: We hit him. Damn, we hit him.\n"
        "  The judge continues: The missiles destroyed the planes.\n"
        "This is a damn serious matter."
    )
    identity = "crec:CREC-1998-02-24-pt1-PgH550#2"
    spoken, quoted = mask_quotations(text, identity)
    assert len(spoken) == len(text)
    assert "Military Control" not in spoken
    assert "Military Control" in quoted
    assert "The judge continues:" in spoken
    assert spoken.endswith("This is a damn serious matter.")
    counts = speaker_counts([turn_file([_row(
        turn_id=identity, bioguide="A000001", text=text,
    )])])
    assert counts.iloc[0]["profanity_hits"] == 2
    assert counts.iloc[0]["profanity_quoted_hits"] == 2


def test_reported_radio_dialogue_requires_an_explicit_closing_cue():
    text = "  MiG-29: An unmatched label is not a reason to hide this damn speech."
    assert mask_quotations(text) == (text, "")
    with pytest.raises(ValueError, match="closing cue"):
        mask_quotations(
            "The MiG-29: A source-reviewed passage lost its end.",
            "crec:CREC-1998-02-24-pt1-PgH550#2",
        )


def test_reviewed_telephone_transcript_preserves_own_words_and_does_not_double_count_quotes(
    turn_file,
):
    text = (
        "I reject this crap.\n"
        "  Kohl: We are discussing Germany.\n"
        "  Bush to Kohl: Helmut, you are a hell of a campaigner.\n"
        "  Kohl: ``Thank you.''\n"
        "  The Berlin Wall did not come down in a day. This is damn important."
    )
    counts = speaker_counts([turn_file([_row(
        turn_id="crec:CREC-1999-11-03-pt1-PgH11472#3", bioguide="A000001", text=text,
    )])])
    assert counts.iloc[0]["profanity_hits"] == 2
    assert counts.iloc[0]["profanity_quoted_hits"] == 1
    assert mask_quotations(
        "Kohl: This is damn serious. The Berlin Wall did not come down in a day.",
        "a-different-original-source",
    )[1] == ""


def test_source_reviewed_external_quote_preserves_the_members_own_use_of_the_same_word(turn_file):
    text = (
        "Another member said, Spare me the\nbullshit about constitutional rights. "
        "So pardon us for disagreeing. That is no bullshit; we will."
    )
    counts = speaker_counts([turn_file([_row(
        turn_id="crec:CREC-2022-06-09-pt1-PgH5415#33",
        bioguide="A000001", text=text,
    )])])
    assert counts.iloc[0]["profanity_hits"] == 1
    assert counts.iloc[0]["profanity_quoted_hits"] == 1
    assert counts.iloc[0]["words"] == Scorers().score_turn(
        "Another member said, So pardon us for disagreeing. That is no bullshit; we will."
    )["n_words"]
    assert mask_quotations(text, "another-source")[1] == ""


def test_reviewed_mixed_quote_delimiter_does_not_swallow_the_members_own_question():
    text = (
        'Here is a ``how high": They eliminate the fee.\n'
        "What the hell does permitting reform have to do with the debt limit?\n"
        "But fossil fuel says ``jump,'' and House Republicans say ``how high?''"
    )
    spoken, quoted = mask_quotations(text, "crec:CREC-2023-04-26-pt1-PgS1370-5#27-resumed-6")
    assert len(spoken) == len(text)
    assert "What the hell does permitting reform" in spoken
    assert "how high" not in spoken and "jump" not in spoken
    assert "how high" in quoted and "jump" in quoted
    assert Scorers().profanity_term_counts(spoken) == {"what the hell": 1}
    with pytest.raises(ValueError, match="uniquely"):
        mask_quotations(text + '\nHere is a ``how high":', "crec:CREC-2023-04-26-pt1-PgS1370-5#other")


@pytest.mark.parametrize(("source", "own_quote", "form"), [
    ("crec:CREC-2026-03-23-pt1-PgS1535", "``Save Your Ass Act''", "ass"),
    ("crec:CREC-2026-03-23-pt1-PgS1535", "``Save Our Asses Act,''", "asses"),
    ("crec:CREC-2026-03-26-pt1-PgS1648", "``Save Their Asses Act.''", "asses"),
])
def test_members_own_sarcastic_bill_names_are_not_external_quotations(source, own_quote, form):
    text = f"I call this the {own_quote} They said ``bullshit'' about it."
    spoken, quoted = mask_quotations(text, source + "#1")
    assert Scorers().profanity_term_counts(spoken) == {form: 1}
    assert Scorers().profanity_term_counts(quoted) == {"bullshit": 1}
    assert mask_quotations(text, "another-source")[0].count(form) == 0


def test_external_newspaper_headline_is_separate_from_the_members_own_use_of_the_same_word():
    text = (
        "The Capital issued a statement: We are putting out a damn paper tomorrow,\n"
        "and that they did. They made Maryland damn proud."
    )
    spoken, quoted = mask_quotations(text, "crec:CREC-2025-06-27-pt1-PgH3024-5#2")
    assert Scorers().profanity_term_counts(spoken) == {"damn": 1}
    assert Scorers().profanity_term_counts(quoted) == {"damn": 1}


def test_congress_scope_is_applied_before_boundary_rows_can_change_member_metadata(turn_file):
    source = turn_file([
        _row(
            turn_id="old", congress=111, date="2010-12-30", bioguide="A",
            party="D", speaker_name="Earlier name", text="This is damn wrong.",
        ),
        _row(
            turn_id="incoming", congress=112, date="2011-01-03", bioguide="A",
            party="R", speaker_name="Incoming name", text="That is bullshit.",
        ),
    ])
    frame = speaker_counts([source], congress=111)
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["party"] == "D" and row["speaker_name"] == "Earlier name"
    assert row["congress"] == 111 and row["profanity_hits"] == 1
    assert row["profanity_terms"] == '{"damn":1}'


def test_turns_without_a_bioguide_are_dropped(turn_file):
    path = turn_file([
        _row(turn_id="a", bioguide=None, text="damn"),
        _row(turn_id="b", bioguide="", text="damn"),
        _row(turn_id="c", bioguide="A000001", text="damn"),
    ])
    counts = speaker_counts([path])
    assert len(counts) == 1
    assert counts.iloc[0]["bioguide"] == "A000001"


def test_procedural_turns_are_excluded(turn_file):
    path = turn_file([
        _row(turn_id="a", bioguide="A000001", is_procedural=True, text="damn"),
        _row(turn_id="b", bioguide="A000001", text="damn"),
    ])
    counts = speaker_counts([path])
    assert counts.iloc[0]["turns"] == 1
    assert counts.iloc[0]["profanity_hits"] == 1


def test_duplicate_turn_ids_are_counted_once(turn_file):
    path = turn_file([
        _row(turn_id="dup", bioguide="A000001", text="damn"),
        _row(turn_id="dup", bioguide="A000001", text="damn"),
    ])
    counts = speaker_counts([path])
    assert counts.iloc[0]["turns"] == 1


def test_congress_comes_from_the_row_not_the_members_latest_term(turn_file):
    # A long-serving member's older days must keep their own Congress. Taking it
    # from the member's most recent metadata would relabel their whole history,
    # inflating the latest Congress's word totals and deflating their rate.
    path = turn_file([
        _row(turn_id="a", bioguide="A000001", date="2023-03-04", congress=118, text="damn"),
        _row(turn_id="b", bioguide="A000001", date="2025-03-04", congress=119, text="damn"),
    ])
    counts = speaker_counts([path]).sort_values("date").reset_index(drop=True)
    assert list(counts["congress"]) == [118, 119]


def test_counts_are_grouped_by_member_day_and_chamber(turn_file):
    path = turn_file([
        _row(turn_id="a", bioguide="A000001", date="2025-03-04", text="damn"),
        _row(turn_id="b", bioguide="A000001", date="2025-03-04", text="crap"),
        _row(turn_id="c", bioguide="A000001", date="2025-03-05", text="hello"),
    ])
    counts = speaker_counts([path]).sort_values("date").reset_index(drop=True)
    assert len(counts) == 2
    assert counts.iloc[0]["profanity_hits"] == 2
    assert json.loads(counts.iloc[0]["profanity_terms"]) == {"crap": 1, "damn": 1}
    assert counts.iloc[1]["profanity_hits"] == 0


def test_term_counts_use_accepted_unquoted_surface_forms(turn_file):
    path = turn_file([
        _row(
            turn_id="a",
            bioguide="A000001",
            text="Damn, damn. This is a crap game. ``bullshit''",
        ),
    ])
    counts = speaker_counts([path])
    assert counts.iloc[0]["profanity_hits"] == 2
    assert counts.iloc[0]["profanity_quoted_hits"] == 1
    assert json.loads(counts.iloc[0]["profanity_terms"]) == {"damn": 2}
    assert sum(json.loads(counts.iloc[0]["profanity_terms"]).values()) == (
        counts.iloc[0]["profanity_hits"]
    )


def test_term_counts_include_conservative_expanded_forms(turn_file):
    path = turn_file([
        _row(
            turn_id="a",
            bioguide="A000001",
            text=(
                "This bitching shitshow is a motherfucking clusterfuck "
                "run by cocksuckers and asshats."
            ),
        ),
    ])
    counts = speaker_counts([path])
    assert counts.iloc[0]["profanity_hits"] == 6
    assert json.loads(counts.iloc[0]["profanity_terms"]) == {
        "asshats": 1,
        "bitching": 1,
        "clusterfuck": 1,
        "cocksuckers": 1,
        "motherfucking": 1,
        "shitshow": 1,
    }


def test_term_parser_handles_legacy_missing_values_and_rejects_non_integers():
    assert parse_profanity_terms(pd.NA) == {}
    assert parse_profanity_terms(float("nan")) == {}
    with pytest.raises(ValueError):
        parse_profanity_terms('{"damn":1.0}')
    with pytest.raises(ValueError):
        parse_profanity_terms('{"damn":true}')


def test_incomplete_term_rows_require_counts_to_equal_accepted_hits():
    daily = _daily([
        ["A", "2025-01-02", "house", "X", "D", "CA", 119, 1, 100, 2, 0, 0, 0],
        ["B", "2025-01-02", "house", "Y", "R", "TX", 119, 1, 100, 1, 0, 0, 0],
    ])
    daily["profanity_terms"] = ['{"damn":2}', "{}"]
    incomplete = incomplete_profanity_term_rows(daily)
    assert list(incomplete["bioguide"]) == ["B"]


def test_term_leaders_include_ties_and_support_chamber_scope():
    daily = _daily([
        ["A", "2025-01-02", "house", "Alpha", "D", "CA", 119, 1, 100, 3, 0, 0, 0],
        ["B", "2025-01-02", "senate", "Beta", "R", "TX", 119, 1, 100, 4, 0, 0, 0],
        ["C", "2025-01-02", "house", "Gamma", "D", "NY", 119, 1, 100, 2, 0, 0, 0],
    ])
    daily["profanity_terms"] = [
        '{"damn":2,"crap":1}', '{"damned":2,"shit":2}', '{"crap":2}',
    ]
    leaders = {row["term"]: row for row in profanity_term_leaders(daily, 119)}
    assert [leader["speaker_name"] for leader in leaders["damn"]["leaders"]] == [
        "Alpha", "Beta",
    ]
    assert leaders["damn"]["leader_hits"] == 2
    assert leaders["damn"]["total_hits"] == 4
    assert leaders["damn"]["variants"] == ["damn", "damned"]
    house = {
        row["term"]: row
        for row in profanity_term_leaders(daily, 119, chamber="house")
    }
    assert [leader["speaker_name"] for leader in house["crap"]["leaders"]] == ["Gamma"]


def test_term_families_group_phrases_that_use_the_same_base_expletive():
    daily = _daily([
        ["A", "2025-01-02", "house", "Alpha", "D", "CA", 119, 1, 100, 6, 0, 0, 0],
        ["B", "2025-01-02", "house", "Beta", "R", "TX", 119, 1, 100, 4, 0, 0, 0],
    ])
    daily["profanity_terms"] = [
        '{"fuck":1,"fuck you":1,"fucked up":1,"god damn":1,"kick ass":1,'
        '"son of a bitch":1}',
        '{"fucking":1,"damn":1,"ass":1,"bitch":1}',
    ]

    leaders = {row["term"]: row for row in profanity_term_leaders(daily, 119)}

    assert set(leaders) == {"ass", "bitch", "damn", "fuck"}
    assert leaders["fuck"]["leader_hits"] == 3
    assert leaders["fuck"]["total_hits"] == 4
    assert leaders["fuck"]["variants"] == ["fuck", "fuck you", "fucked up", "fucking"]
    assert leaders["damn"]["variants"] == ["damn", "god damn"]
    assert leaders["ass"]["variants"] == ["ass", "kick ass"]
    assert leaders["bitch"]["variants"] == ["bitch", "son of a bitch"]


def test_every_profanity_form_has_an_explicit_display_family():
    configured = {
        line.split("\t", 1)[0]
        for line in (
            ROOT / "analysis" / "score" / "lexicons" / "profanity.txt"
        ).read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }
    assigned = {
        form
        for forms in PROFANITY_TERM_FAMILIES.values()
        for form in forms
    }

    assert assigned == configured
    assert sum(len(forms) for forms in PROFANITY_TERM_FAMILIES.values()) == len(assigned)


def test_term_member_counts_preserve_party_chamber_and_state_dimensions():
    daily = _daily([
        ["A", "2025-01-02", "house", "Alpha", "D", "CA", 119, 1, 100, 2, 0, 0, 0],
        ["A", "2025-02-02", "house", "Alpha", "D", "CA", 119, 1, 100, 1, 0, 0, 0],
        ["B", "2025-01-02", "senate", "Beta", "R", "TX", 119, 1, 100, 2, 0, 0, 0],
    ])
    daily["profanity_terms"] = [
        '{"damn":1,"damned":1}', '{"damn":1}', '{"shit":2}',
    ]

    rows = profanity_term_member_counts(daily, 119)

    assert rows == [
        {
            "bioguide": "A",
            "speaker_name": "Alpha",
            "party": "D",
            "state": "CA",
            "chamber": "house",
            "term": "damn",
            "hits": 3,
            "variants": ["damn", "damned"],
        },
        {
            "bioguide": "B",
            "speaker_name": "Beta",
            "party": "R",
            "state": "TX",
            "chamber": "senate",
            "term": "shit",
            "hits": 2,
            "variants": ["shit"],
        },
    ]


# -------------------------------------------------------------------- merging


def _daily(rows):
    return pd.DataFrame(rows, columns=[
        "bioguide", "date", "chamber", "speaker_name", "party", "state", "congress",
        "turns", "words", "profanity_hits", "profanity_quoted_hits",
        "slur_hits", "slur_quoted_hits",
    ])


def test_merge_replaces_rather_than_double_counting_a_recomputed_day():
    existing = _daily([["A", "2025-01-02", "house", "X", "D", "CA", 119, 1, 100, 1, 0, 0, 0]])
    fresh = _daily([["A", "2025-01-02", "house", "X", "D", "CA", 119, 2, 200, 5, 0, 0, 0]])
    merged = merge_daily(existing, fresh)
    assert len(merged) == 1
    assert merged.iloc[0]["profanity_hits"] == 5  # replaced, not 6


def test_merge_appends_new_days_and_sorts():
    existing = _daily([["A", "2025-01-02", "house", "X", "D", "CA", 119, 1, 100, 1, 0, 0, 0]])
    fresh = _daily([["A", "2025-01-01", "house", "X", "D", "CA", 119, 1, 100, 2, 0, 0, 0]])
    merged = merge_daily(existing, fresh)
    assert list(merged["date"]) == ["2025-01-01", "2025-01-02"]


def test_replace_daily_window_removes_stale_member_rows():
    existing = _daily([
        ["A", "2025-01-01", "house", "X", "D", "CA", 119, 1, 100, 1, 0, 0, 0],
        ["B", "2025-01-02", "house", "Y", "R", "TX", 119, 1, 100, 1, 0, 0, 0],
    ])
    fresh = _daily([
        ["C", "2025-01-02", "house", "Z", "D", "NY", 119, 1, 100, 0, 0, 0, 0],
    ])

    replaced = replace_daily_window(existing, fresh, "2025-01-02", "2025-01-02")

    assert list(replaced["bioguide"]) == ["A", "C"]


def test_merge_handles_missing_and_empty_inputs():
    fresh = _daily([["A", "2025-01-02", "house", "X", "D", "CA", 119, 1, 100, 1, 0, 0, 0]])
    assert len(merge_daily(None, fresh)) == 1
    assert len(merge_daily(fresh, _daily([]))) == 1


# ---------------------------------------------------------------- leaderboard


def test_small_samples_are_excluded_from_the_rate_ranking():
    daily = _daily([
        # One profane word in 300 words would otherwise dominate any rate ranking.
        ["SMALL", "2025-01-02", "house", "Tiny", "D", "CA", 119, 1, 300, 1, 0, 0, 0],
        ["BIG", "2025-01-02", "house", "Big", "R", "TX", 119, 50, 60_000, 6, 0, 0, 0],
    ])
    board = leaderboard(daily, min_words=25_000)
    assert list(board["bioguide"]) == ["BIG"]


def test_leaderboard_ranks_by_rate_not_raw_count():
    daily = _daily([
        ["LOUD", "2025-01-02", "house", "Loud", "D", "CA", 119, 10, 30_000, 9, 0, 0, 0],
        ["VERBOSE", "2025-01-02", "house", "Verbose", "R", "TX", 119, 99, 300_000, 20, 0, 0, 0],
    ])
    board = leaderboard(daily, min_words=25_000)
    # 30 per 100k beats 6.7 per 100k despite half the raw hits.
    assert list(board["speaker_name"]) == ["Loud", "Verbose"]
    assert list(board["rank"]) == [1, 2]


def test_leaderboard_scopes_to_a_congress():
    daily = _daily([
        ["A", "2023-01-02", "house", "Old", "D", "CA", 118, 10, 40_000, 40, 0, 0, 0],
        ["B", "2025-01-02", "house", "New", "R", "TX", 119, 10, 40_000, 4, 0, 0, 0],
    ])
    board = leaderboard(daily, min_words=25_000, congress=119)
    assert list(board["speaker_name"]) == ["New"]


def test_leaderboard_is_empty_when_nobody_qualifies():
    daily = _daily([["A", "2025-01-02", "house", "Tiny", "D", "CA", 119, 1, 10, 1, 0, 0, 0]])
    assert leaderboard(daily, min_words=25_000).empty


def test_zero_word_rows_do_not_divide_by_zero():
    daily = _daily([["A", "2025-01-02", "house", "X", "D", "CA", 119, 1, 0, 0, 0, 0, 0]])
    board = leaderboard(daily, min_words=0)
    assert board.iloc[0]["profanity_per_100k"] == 0.0


# ----------------------------------------------------------------- timeseries


def test_timeseries_aggregates_by_year_and_chamber():
    daily = _daily([
        ["A", "2025-01-02", "house", "X", "D", "CA", 119, 1, 50_000, 5, 0, 0, 0],
        ["B", "2025-06-02", "house", "Y", "R", "TX", 119, 1, 50_000, 5, 0, 0, 0],
        ["C", "2025-06-02", "senate", "Z", "D", "NY", 119, 1, 100_000, 1, 0, 0, 0],
    ])
    series = timeseries(daily).set_index(["period", "chamber"])
    assert series.loc[("2025", "house"), "profanity_per_100k"] == pytest.approx(10.0)
    assert series.loc[("2025", "senate"), "profanity_per_100k"] == pytest.approx(1.0)


def test_language_timeseries_uses_months_and_compares_parties():
    daily = _daily([
        ["A", "2025-01-02", "house", "X", "D", "CA", 119, 1, 1_000, 10, 0, 5, 20],
        ["B", "2025-01-20", "house", "Y", "D", "TX", 119, 1, 9_000, 0, 0, 5, 0],
        ["C", "2025-02-02", "senate", "Z", "R", "NY", 119, 1, 10_000, 1, 0, 2, 3],
        ["D", "2025-02-02", "extensions", "E", "D", "MA", 119, 1, 1, 100, 0, 100, 100],
        ["A", "2023-02-02", "house", "X", "D", "CA", 118, 1, 10_000, 2, 0, 4, 6],
    ])
    scoped = language_timeseries(daily, 119).set_index(["period", "party"])
    assert set(scoped.index.get_level_values("period")) == {"2025-01", "2025-02"}
    january = scoped.loc[("2025-01", "D")]
    assert january["profanity_per_100k"] == pytest.approx(100.0)
    assert january["slurs_per_100k"] == pytest.approx(100.0)
    assert int(scoped["words"].sum()) == 20_000

    all_years = language_timeseries(daily)
    assert set(all_years["period"]) == {"2023", "2025"}
    for metric in LANGUAGE_METRICS.values():
        assert metric["hits"] in all_years
        assert metric["rate"] in all_years
    chamber = language_timeseries(daily, 119, by_chamber=True)
    assert set(chamber["chamber"]) == {"house", "senate"}


def test_family_timeseries_combines_inflections_and_uses_party_word_totals():
    from analysis.speakers import profanity_family_timeseries

    daily = _daily([
        ["A", "2025-01-02", "house", "Alpha", "D", "CA", 119, 1, 1_000, 2, 0, 0, 0],
        ["B", "2025-02-02", "senate", "Beta", "D", "NY", 119, 1, 9_000, 1, 0, 0, 0],
        ["C", "2025-03-02", "house", "Gamma", "R", "TX", 119, 1, 20_000, 1, 0, 0, 0],
        ["D", "2026-01-02", "house", "Delta", "D", "CA", 119, 1, 1_000, 1, 0, 0, 0],
        ["E", "2026-02-02", "senate", "Epsilon", "R", "FL", 119, 1, 1_000, 0, 0, 0, 0],
        ["F", "2025-02-02", "extensions", "Printed", "D", "MA", 119, 1, 1, 100, 0, 0, 0],
    ])
    daily["profanity_terms"] = [
        '{"fuck":1,"fucking":1}', '{"fucked":1}', '{"damn":1}',
        '{"motherfucker":1}', "{}", '{"crap":100}',
    ]
    payload = profanity_family_timeseries(daily, top=2)
    assert [item["term"] for item in payload["terms"]] == ["fuck", "damn"]
    fuck = payload["terms"][0]
    assert fuck["total_hits"] == 3
    assert fuck["variants"] == ["fuck", "fucked", "fucking"]
    assert fuck["label"] == "fu*k"
    assert next(row for row in fuck["series"] if row["year"] == 2025 and row["party"] == "D") == {
        "year": 2025, "party": "D", "words": 10_000, "hits": 3, "rate_per_100k": 30.0,
    }
    assert next(row for row in fuck["series"] if row["year"] == 2025 and row["party"] == "R")["rate_per_100k"] == 0.0
    assert next(row for row in fuck["series"] if row["year"] == 2026 and row["party"] == "D")["hits"] == 0
    assert payload["coverage"]["words"] == 32_000


def test_family_timeseries_refuses_missing_term_counts():
    from analysis.speakers import profanity_family_timeseries

    daily = _daily([
        ["A", "2015-01-02", "house", "Alpha", "D", "CA", 114, 1, 1000, 1, 0, 0, 0],
    ])
    with pytest.raises(ValueError, match="incomplete profanity terms"):
        profanity_family_timeseries(daily)


def test_family_codebook_keeps_zero_match_strong_words_and_exact_denominators():
    from analysis.speakers import profanity_family_timeseries

    daily = _daily([
        ["A", "2025-01-02", "house", "Alpha", "D", "CA", 119, 1, 1_000, 2, 0, 0, 0],
        ["B", "2025-01-03", "senate", "Beta", "R", "TX", 119, 1, 4_000, 1, 0, 0, 0],
    ])
    daily["profanity_terms"] = ['{"fucking":1,"damn":1}', '{"damned":1}']
    payload = profanity_family_timeseries(daily, top=1)
    assert [item["term"] for item in payload["terms"]] == ["damn"]
    families = {item["term"]: item for item in payload["codebook"]}
    assert families["fuck"]["total_hits"] == 1
    assert families["fuck"]["variants"] == ["fucking"]
    assert {"fuck", "fucked", "fucking"} <= set(families["fuck"]["included_forms"])
    assert families["fuck"]["series"] == [
        {"year": 2025, "party": "D", "words": 1000, "hits": 1, "rate_per_100k": 100.0},
        {"year": 2025, "party": "R", "words": 4000, "hits": 0, "rate_per_100k": 0.0},
    ]
    assert families["motherfucker"]["total_hits"] == 0
    assert families["motherfucker"]["series"] and all(
        row["hits"] == row["rate_per_100k"] == 0
        for row in families["motherfucker"]["series"]
    )
    assert sum(item["total_hits"] for item in families.values()) == 3


def test_multitoken_term_labels_mask_profanity_after_the_first_word():
    from analysis.speakers import censor_profanity_term

    assert censor_profanity_term("god damn") == "g*d da*n"
    assert censor_profanity_term("kick ass") == "ki*k a*s"
    assert censor_profanity_term("taking the piss") == "ta***g the pi*s"
    assert censor_profanity_term("fuck you") == "fu*k you"


def test_language_member_rates_keep_measures_separate_and_apply_threshold():
    daily = _daily([
        ["A", "2025-01-02", "house", "Alpha", "D", "CA", 119,
         2, 20_000, 10, 0, 1, 2],
        ["A", "2025-02-02", "house", "Alpha", "D", "CA", 119,
         2, 20_000, 0, 0, 7, 2],
        ["B", "2025-01-02", "senate", "Beta", "R", "TX", 119,
         2, 50_000, 5, 0, 20, 100],
        ["C", "2025-01-02", "house", "Tiny", "D", "NY", 119,
         1, 1_000, 100, 0, 100, 100],
        ["D", "2025-01-02", "house", "Zero", "R", "FL", 119,
         1, 60_000, 0, 0, 0, 0],
    ])
    daily["profanity_terms"] = [
        '{"damn":3,"damned":3,"crap":4}', "{}", '{"shit":5}', '{"crap":100}', "{}",
    ]
    rankings = language_member_rates(daily, 119, min_words=25_000, top=5)
    assert set(rankings) == set(LANGUAGE_METRICS)
    assert list(rankings["profanity"]["speaker_name"]) == ["Alpha", "Beta"]
    assert rankings["slurs"].iloc[0]["speaker_name"] == "Beta"
    assert "Tiny" not in set(rankings["profanity"]["speaker_name"])
    assert "Zero" not in set(rankings["profanity"]["speaker_name"])
    assert "Zero" not in set(rankings["slurs"]["speaker_name"])
    alpha = rankings["profanity"].set_index("bioguide").loc["A"]
    assert alpha["profanity_per_100k"] == pytest.approx(25.0)
    assert alpha["favorite_profanity_term"] == "damn"
    assert alpha["favorite_profanity_term_hits"] == 6
    house_only = language_member_rates(
        daily, 119, min_words=25_000, top=5, chamber="house"
    )
    assert set(house_only["profanity"]["speaker_name"]) == {"Alpha"}


# ------------------------------------------------------------------- storage


def test_storage_partitions_by_congress(tmp_path):
    daily = _daily([
        ["A", "2023-01-02", "house", "X", "D", "CA", 118, 1, 100, 1, 0, 0, 0],
        ["B", "2025-01-02", "house", "Y", "R", "TX", 119, 1, 100, 1, 0, 0, 0],
    ])
    save_daily(daily, tmp_path / "speaker_daily")
    names = sorted(p.name for p in (tmp_path / "speaker_daily").glob("*.parquet"))
    assert names == ["congress_118.parquet", "congress_119.parquet"]


def test_unchanged_partitions_are_not_rewritten(tmp_path):
    # The table is committed and rewritten by a scheduled job, so an update that
    # touches one Congress must not produce a diff for every other one.
    store = tmp_path / "speaker_daily"
    daily = _daily([
        ["A", "2023-01-02", "house", "X", "D", "CA", 118, 1, 100, 1, 0, 0, 0],
        ["B", "2025-01-02", "house", "Y", "R", "TX", 119, 1, 100, 1, 0, 0, 0],
    ])
    save_daily(daily, store)
    assert save_daily(daily, store) == []

    updated = merge_daily(daily, _daily([
        ["C", "2025-01-03", "house", "Z", "D", "NY", 119, 1, 100, 2, 0, 0, 0],
    ]))
    written = save_daily(updated, store)
    assert [p.name for p in written] == ["congress_119.parquet"]


def test_round_trip_through_partitioned_storage(tmp_path):
    store = tmp_path / "speaker_daily"
    daily = _daily([
        ["A", "2023-01-02", "house", "X", "D", "CA", 118, 1, 100, 1, 0, 0, 0],
        ["B", "2025-01-02", "house", "Y", "R", "TX", 119, 2, 200, 3, 1, 0, 0],
    ])
    save_daily(daily, store)
    loaded = load_daily(store).sort_values("date").reset_index(drop=True)
    assert list(loaded["bioguide"]) == ["A", "B"]
    assert int(loaded.loc[loaded.bioguide == "B", "profanity_hits"].iloc[0]) == 3


def test_load_returns_none_when_nothing_stored(tmp_path):
    assert load_daily(tmp_path / "missing") is None
    empty = tmp_path / "speaker_daily"
    empty.mkdir()
    assert load_daily(empty) is None
