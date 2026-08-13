"""Tests for the per-speaker leaderboard pipeline.

This feature names individuals, so the attribution safeguards are the part that
matters most: quoted speech must not be charged to the member reading it, turns
without a stable id must not be guessed at, and small samples must not be allowed
to top a rate ranking.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.speakers import (  # noqa: E402
    LANGUAGE_METRICS,
    language_member_rates,
    language_timeseries,
    leaderboard,
    load_daily,
    mask_quotations,
    merge_daily,
    save_daily,
    speaker_counts,
    timeseries,
)
from analysis.ingest.schema import ARROW_SCHEMA  # noqa: E402

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402


# ------------------------------------------------------------------ quotations


def test_mask_quotations_splits_spoken_from_quoted():
    spoken, quoted = mask_quotations("I said ``this is damn hard'' yesterday.")
    assert "damn" not in spoken
    assert "damn" in quoted
    # Masking preserves length so surrounding offsets stay valid.
    assert len(spoken) == len("I said ``this is damn hard'' yesterday.")
    assert spoken.startswith("I said")
    assert spoken.rstrip().endswith("yesterday.")


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


def test_unquoted_profanity_is_counted(turn_file):
    path = turn_file([_row(turn_id="a", bioguide="A000001", text="This is a damn disgrace.")])
    counts = speaker_counts([path])
    assert counts.iloc[0]["profanity_hits"] == 1
    assert counts.iloc[0]["profanity_quoted_hits"] == 0


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
    assert counts.iloc[1]["profanity_hits"] == 0


# -------------------------------------------------------------------- merging


def _daily(rows):
    return pd.DataFrame(rows, columns=[
        "bioguide", "date", "chamber", "speaker_name", "party", "state", "congress",
        "turns", "words", "profanity_hits", "profanity_quoted_hits",
        "hostility_hits", "misconduct_hits",
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
    assert january["hostility_per_100k"] == pytest.approx(100.0)
    assert january["misconduct_per_100k"] == pytest.approx(200.0)
    assert int(scoped["words"].sum()) == 20_000

    all_years = language_timeseries(daily)
    assert set(all_years["period"]) == {"2023", "2025"}
    for metric in LANGUAGE_METRICS.values():
        assert metric["hits"] in all_years
        assert metric["rate"] in all_years
    chamber = language_timeseries(daily, 119, by_chamber=True)
    assert set(chamber["chamber"]) == {"house", "senate"}


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
    rankings = language_member_rates(daily, 119, min_words=25_000, top=5)
    assert set(rankings) == set(LANGUAGE_METRICS)
    assert list(rankings["profanity"]["speaker_name"]) == ["Alpha", "Beta"]
    assert rankings["hostility"].iloc[0]["speaker_name"] == "Beta"
    assert rankings["misconduct"].iloc[0]["speaker_name"] == "Beta"
    assert "Tiny" not in set(rankings["profanity"]["speaker_name"])
    assert "Zero" not in set(rankings["profanity"]["speaker_name"])
    assert "Zero" not in set(rankings["hostility"]["speaker_name"])
    assert "Zero" not in set(rankings["misconduct"]["speaker_name"])
    alpha = rankings["profanity"].set_index("bioguide").loc["A"]
    assert alpha["profanity_per_100k"] == pytest.approx(25.0)
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
