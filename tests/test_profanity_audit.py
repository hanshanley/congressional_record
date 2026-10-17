import json

import pandas as pd
import pytest

from analysis.score.scorers import Scorers
from analysis.speakers import PROFANITY_TERM_FAMILIES
from scripts.audit_profanity_families import (
    ReferenceMatcher, audit, congress_window, discover_packages, inspect_turn,
)


def test_independent_reference_recognizes_every_actual_codebook_form():
    matcher = ReferenceMatcher()
    scorer = Scorers()
    for family, forms in PROFANITY_TERM_FAMILIES.items():
        for form in forms:
            for text in [form, form.upper(), f"{form}; {form}", f"({form})"]:
                expected = 2 if text == f"{form}; {form}" else 1
                row = {
                    "text": text, "chamber": "house", "bioguide": "A", "party": "D",
                    "turn_id": "original",
                }
                counts, evidence, mismatch = inspect_turn(row, matcher, scorer, "members")
                assert counts == {form: expected}
                assert mismatch is None, mismatch
                assert len(evidence) == expected
                assert all(item["family"] == family for item in evidence)
                assert all(item["matched_text"] in {form, form.upper()} for item in evidence)


def test_audit_records_original_quoted_and_inserted_words_without_charging_members():
    matcher, scorer = ReferenceMatcher(), Scorers()
    row = {
        "text": "This is damn hard. I quote ``fucking bullshit'' and object.",
        "turn_id": "source-original", "chamber": "house", "bioguide": "A", "party": "D",
    }
    counts, evidence, mismatch = inspect_turn(row, matcher, scorer, "members")
    assert counts == {"damn": 1}
    assert mismatch is None
    assert [item["disposition"] for item in evidence] == [
        "accepted", "quotation_exclusion", "quotation_exclusion",
    ]
    assert [item["matched_text"] for item in evidence] == ["damn", "fucking", "bullshit"]
    assert all("*" not in item["matched_text"] for item in evidence)
    counts, evidence, mismatch = inspect_turn(
        {**row, "is_procedural": True, "bioguide": ""}, matcher, scorer, "members",
    )
    assert not counts
    assert mismatch is None
    assert all(item["disposition"] == "procedural_or_inserted" for item in evidence)
    aggregate, _, mismatch = inspect_turn(row, matcher, scorer, "aggregate")
    assert aggregate == {"damn": 1, "fucking": 1, "bullshit": 1}
    assert mismatch is None


def test_audit_preserves_the_raw_word_and_flags_incorrect_stored_counts(tmp_path):
    path = tmp_path / "govinfo_bulk_119.parquet"
    pd.DataFrame([{
        "turn_id": "source-1", "source": "govinfo", "text": "That is fucking wrong.",
        "congress": 119, "date": "2025-03-01", "chamber": "house",
        "bioguide": "A", "speaker_name": "Alpha", "party": "D", "is_procedural": False,
    }]).to_parquet(path)
    daily = pd.DataFrame([{
        "bioguide": "A", "date": "2025-03-01", "chamber": "house",
        "congress": 119, "profanity_hits": 1, "profanity_terms": '{"damn":1}',
    }, {
        "bioguide": "B", "date": "2025-03-01", "chamber": "extensions",
        "congress": 119, "profanity_hits": 1, "profanity_terms": '{"crap":1}',
    }])
    out = tmp_path / "audit"
    summary = audit([path], out, mode="members", daily=daily, congress=119)
    assert summary["scorer_disagreements"] == 0
    assert summary["stored_member_day_differences"] == 1
    assert summary["families"]["fuck"]["accepted"] == 1
    evidence = json.loads((out / "evidence.jsonl").read_text())
    assert evidence["matched_text"] == "fucking"
    difference = json.loads((out / "stored_differences.json").read_text())[0]
    assert difference["stored"] == {"damn": 1}
    assert difference["reference"] == {"fucking": 1}


def test_literal_context_exclusions_and_attached_compounds_retain_original_evidence():
    row = {
        "text": "They tried to damn them. This is god damn-like, but that is damn hard.",
        "turn_id": "original", "chamber": "house", "bioguide": "A", "party": "D",
    }
    counts, evidence, mismatch = inspect_turn(row, ReferenceMatcher(), Scorers(), "members")
    assert counts == {"damn": 1}
    assert mismatch is None
    assert [(item["matched_text"], item["disposition"]) for item in evidence] == [
        ("damn", "literal_context_exclusion"), ("damn", "accepted"),
    ]


def test_multi_year_source_discovery_is_paginated_and_rejects_missing_stored_dates():
    class Client:
        def paginate(self, url, **kwargs):
            assert "2003-01-03/2005-01-02" in url
            assert kwargs["items_key"] == "packages"
            yield {"packageId": "CREC-2004-12-20"}
            yield {"packageId": "CREC-2003-01-07"}
            yield {"packageId": "CREC-2003-01-07"}
            yield {"packageId": "CREC-2005-01-03"}

        def url(self, path):
            return f"https://api.govinfo.gov{path}"

    assert discover_packages(
        "2003-01-03", "2005-01-02", {"2003-01-07", "2004-12-20"}, Client(),
    ) == ["CREC-2003-01-07", "CREC-2004-12-20"]
    with pytest.raises(RuntimeError, match="omits 1 stored dates"):
        discover_packages("2003-01-03", "2005-01-02", {"2004-01-20"}, Client())


def test_source_audit_keeps_known_outgoing_congress_boundary_dates():
    assert congress_window(103, "2026-10-17") == ("1994-01-25", "1995-01-03")
    assert congress_window(110, "2026-10-17") == ("2007-01-01", "2009-01-03")
    assert congress_window(119, "2026-10-17") == ("2025-01-01", "2026-10-17")
