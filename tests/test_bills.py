"""Tests for canonical bill storage and member activity leaderboards."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from analysis.bills import (
    activity_leaderboards,
    bill_id,
    canonical_bill,
    load_bills,
    member_activity,
    merge_bills,
    save_bills,
)


def _bill(
    number: int,
    sponsor: str = "A",
    actions=None,
    laws=None,
    congress: int = 119,
) -> dict:
    return canonical_bill(
        source="test",
        source_url=f"https://example.test/{number}",
        source_updated_at="2026-08-01T00:00:00Z",
        congress=congress,
        bill_type="HR",
        bill_number=number,
        title=f"Bill {number}",
        origin_chamber="House",
        introduced_date="2025-01-01",
        sponsors=[{
            "bioguideId": sponsor,
            "fullName": f"Member {sponsor}",
            "party": "D",
            "state": "CA",
        }],
        actions=actions or [],
        laws=laws or [],
    )


def _daily() -> pd.DataFrame:
    return pd.DataFrame([
        ["A", "2025-01-02", "house", "Member A", "D", "CA", 119, 10, 50_000, 5, 1, 0, 0],
        ["A", "2025-01-03", "house", "Member A", "D", "CA", 119, 5, 25_000, 1, 0, 0, 0],
        ["B", "2025-01-02", "senate", "Member B", "R", "TX", 119, 20, 100_000, 5, 0, 0, 0],
    ], columns=[
        "bioguide", "date", "chamber", "speaker_name", "party", "state", "congress",
        "turns", "words", "profanity_hits", "profanity_quoted_hits",
        "hostility_hits", "misconduct_hits",
    ])


def test_bill_id_rejects_non_bill_measure_types():
    assert bill_id(119, "HR", 42) == "119-hr-42"
    with pytest.raises(ValueError):
        bill_id(119, "HRES", 42)


def test_official_action_codes_set_passage_milestones():
    house = _bill(1, actions=[{"actionCode": "8000", "actionDate": "2025-02-02"}])
    senate = _bill(2, actions=[{"actionCode": "17000", "actionDate": "2025-03-03"}])
    assert house["passed_house"] and house["passed_house_date"] == "2025-02-02"
    assert senate["passed_senate"] and senate["passed_senate_date"] == "2025-03-03"


def test_unrelated_motion_text_does_not_count_as_bill_passage():
    row = _bill(1, actions=[{
        "text": "Motion to waive the Budget Act passed in Senate.",
        "actionDate": "2025-02-02",
    }])
    assert not row["passed_any_chamber"]


def test_law_record_sets_enactment_and_law_identity():
    row = _bill(
        1,
        actions=[
            {"actionCode": "8000", "actionDate": "2025-02-02"},
            {"actionCode": "36000", "actionDate": "2025-04-04"},
        ],
        laws=[{"type": "Public Law", "number": "119-1"}],
    )
    assert row["became_law"]
    assert row["law_number"] == "119-1"
    assert row["became_law_date"] == "2025-04-04"


def test_law_citation_matches_bill_congress_instead_of_first_entry():
    row = _bill(
        1,
        congress=110,
        laws=[
            {"type": "Public Law", "number": "109-9"},
            {"type": "Private Law", "number": "110-2"},
        ],
    )
    assert row["became_law"]
    assert row["law_type"] == "Private Law"
    assert row["law_number"] == "110-2"


def test_law_citation_falls_back_to_first_valid_entry_when_none_match():
    row = _bill(
        1,
        congress=110,
        laws=[
            {"type": "Private Law", "number": "108-4"},
            {"type": "Public Law", "number": "109-7"},
        ],
    )
    assert row["law_type"] == "Private Law"
    assert row["law_number"] == "108-4"


def test_law_citation_ignores_malformed_and_blank_entries():
    row = _bill(
        1,
        congress=110,
        laws=[
            {"type": "", "number": "110-1"},
            {"type": "Public Law", "number": ""},
            {"type": "Public Law", "number": "not-a-citation"},
            {"type": "Private Law", "number": " 110-8 "},
        ],
    )
    assert row["law_type"] == "Private Law"
    assert row["law_number"] == "110-8"


def test_malformed_laws_preserve_enactment_without_invalid_identity():
    row = _bill(
        1,
        laws=[
            {"type": "Public Law", "number": ""},
            {"type": "", "number": "119-3"},
        ],
    )
    assert row["became_law"]
    assert row["law_type"] == ""
    assert row["law_number"] == ""


def test_merge_replaces_changed_bill_instead_of_duplicating():
    existing = pd.DataFrame([_bill(1)])
    fresh = pd.DataFrame([_bill(1, actions=[{"actionCode": "8000"}])])
    merged = merge_bills(existing, fresh)
    assert len(merged) == 1
    assert bool(merged.iloc[0]["passed_any_chamber"])


def test_partitioned_storage_rewrites_only_changed_congress(tmp_path: Path):
    store = tmp_path / "bills"
    frame = pd.DataFrame([_bill(1, congress=118), _bill(2, congress=119)])
    assert {path.name for path in save_bills(frame, store)} == {
        "congress_118.parquet", "congress_119.parquet",
    }
    assert save_bills(frame, store) == []
    updated = merge_bills(frame, pd.DataFrame([
        _bill(2, congress=119, actions=[{"actionCode": "8000"}])
    ]))
    assert [path.name for path in save_bills(updated, store)] == ["congress_119.parquet"]
    assert len(load_bills(store)) == 2


def test_activity_outer_joins_speech_and_legislation():
    bills = pd.DataFrame([
        _bill(1, "A", actions=[{"actionCode": "8000"}]),
        _bill(2, "C"),
    ])
    activity = member_activity(_daily(), bills, 119).set_index("bioguide")
    assert set(activity.index) == {"A", "B", "C"}
    assert activity.loc["A", "words"] == 75_000
    assert activity.loc["A", "active_days"] == 2
    assert activity.loc["A", "bills_passed"] == 1
    assert activity.loc["B", "bills_sponsored"] == 0
    assert activity.loc["C", "words"] == 0


def test_extensions_of_remarks_are_not_counted_as_floor_speech():
    daily = pd.concat([
        _daily(),
        pd.DataFrame([
            ["A", "2025-01-04", "extensions", "Member A", "D", "CA", 119,
             1, 1_000_000, 100, 0, 0, 0],
        ], columns=_daily().columns),
    ], ignore_index=True)
    activity = member_activity(daily, pd.DataFrame([_bill(1, "A")]), 119)
    member = activity.set_index("bioguide").loc["A"]
    assert member["words"] == 75_000
    assert member["profanity_hits"] == 6


def test_leaderboards_use_transparent_metrics_and_stable_threshold():
    activity = member_activity(
        _daily(),
        pd.DataFrame([
            _bill(1, "A", actions=[{"actionCode": "8000"}]),
            _bill(2, "A"),
            _bill(3, "B"),
        ]),
        119,
    )
    boards = activity_leaderboards(activity, min_words=60_000)
    assert boards["speech"].iloc[0]["bioguide"] == "B"
    assert boards["sponsored"].iloc[0]["bioguide"] == "A"
    assert boards["passed"].iloc[0]["bills_passed"] == 1
    assert "B" not in set(boards["passed"]["bioguide"])
    assert boards["enacted"].empty
    assert set(boards["profanity"]["bioguide"]) == {"A", "B"}


def test_member_activity_preserves_all_language_measures_and_rates():
    daily = _daily()
    daily.loc[daily["bioguide"] == "A", "hostility_hits"] = [2, 4]
    daily.loc[daily["bioguide"] == "A", "misconduct_hits"] = [3, 6]
    activity = member_activity(
        daily,
        pd.DataFrame([_bill(1, "A")]),
        119,
    ).set_index("bioguide")
    member = activity.loc["A"]
    assert member["hostility_hits"] == 6
    assert member["misconduct_hits"] == 9
    assert member["profanity_per_100k"] == pytest.approx(8.0)
    assert member["hostility_per_100k"] == pytest.approx(8.0)
    assert member["misconduct_per_100k"] == pytest.approx(12.0)
    assert set(activity_leaderboards(activity)) == {
        "speech", "sponsored", "passed", "enacted", "profanity",
    }
