import json

import pandas as pd
import pytest

from analysis.score.scorers import Scorers
from analysis.ingest.schema import write_turns_parquet
from analysis.speakers import PROFANITY_TERM_FAMILIES, save_daily, speaker_counts
from scripts import audit_profanity_families
from scripts.audit_profanity_families import (
    ReferenceMatcher, audit, congress_window, discover_packages, inspect_turn,
    validate_candidate_counts,
    validate_aggregate_counts,
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


@pytest.mark.parametrize(("text", "wrong_score"), [
    ("Ordinary neutral words with no lexical occurrence.", 1),
    ("This is damn wrong.", 0),
])
def test_full_audit_still_checks_scalar_errors_on_zero_match_and_positive_match_text(text, wrong_score):
    class BrokenScalar(Scorers):
        def score_profanity(self, text):
            return {"profanity_mild": wrong_score, "profanity_strong": 0, "profanity_hits": wrong_score}

    row = {
        "text": text, "turn_id": "original", "chamber": "house", "party": "D",
        "bioguide": "A",
    }
    _, _, mismatch = inspect_turn(row, ReferenceMatcher(), BrokenScalar(), "members")
    assert mismatch is not None
    assert mismatch["scored_hits"] == wrong_score


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
    snapshot = pd.read_parquet(out / "candidate_turns.parquet")
    assert summary["candidate_turns_retained"] == 1
    assert snapshot.iloc[0]["text"] == "That is fucking wrong."
    assert snapshot.iloc[0]["turn_id"] == "source-1"
    difference = json.loads((out / "stored_differences.json").read_text())[0]
    assert difference["stored"] == {"damn": 1}
    assert difference["reference"] == {"fucking": 1}


def test_audit_recognizes_the_external_dialogue_boundary_without_display_masking():
    row = {
        "text": (
            "This is damn serious.\n"
            "  The MiG-29: Give us authorization, damn it.\n"
            "  Military Control: Fire.\n"
            "  MiG-29: Damn, we hit him.\n"
            "  The judge continues: These were civilians."
        ),
        "turn_id": "crec:CREC-1998-02-24-pt1-PgH550#2",
        "chamber": "house", "bioguide": "A", "party": "R",
    }
    counts, evidence, mismatch = inspect_turn(row, ReferenceMatcher(), Scorers(), "members")
    assert mismatch is None
    assert counts == {"damn": 1}
    assert [item["disposition"] for item in evidence] == [
        "accepted", "quotation_exclusion", "quotation_exclusion",
    ]


def test_replacement_partition_must_match_actual_original_forms_not_just_hit_totals(tmp_path):
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_text(json.dumps({
        "bioguide": "A", "date": "2025-03-01", "chamber": "house",
        "form": "fucking", "disposition": "accepted",
    }) + "\n")
    candidate = pd.DataFrame([{
        "bioguide": "A", "date": "2025-03-01", "chamber": "house",
        "profanity_hits": 1, "profanity_terms": '{"damn":1}',
    }])
    with pytest.raises(RuntimeError, match="original-source reference"):
        validate_candidate_counts(candidate, evidence)
    candidate.loc[0, "profanity_terms"] = '{"fucking":1}'
    validate_candidate_counts(candidate, evidence)
    with pytest.raises(ValueError, match="Duplicate"):
        validate_candidate_counts(pd.concat([candidate, candidate]), evidence)


def test_aggregate_artifact_must_reconcile_the_actual_tiers_not_just_the_total(tmp_path):
    evidence = tmp_path / "evidence.jsonl"
    evidence.write_text(json.dumps({
        "congress": 119, "chamber": "house", "party": "D", "tier": "strong",
        "disposition": "accepted",
    }) + "\n")
    frame = pd.DataFrame([{
        "congress": 119, "chamber": "house", "party": "D",
        "profanity_hits": 1, "profanity_mild_hits": 1, "profanity_strong_hits": 0,
    }])
    with pytest.raises(RuntimeError, match="original-source reference"):
        validate_aggregate_counts(frame, evidence)
    frame.loc[0, ["profanity_mild_hits", "profanity_strong_hits"]] = [0, 1]
    validate_aggregate_counts(frame, evidence)


def test_historical_aggregate_reconciles_each_original_source_separately(tmp_path):
    evidence = tmp_path / "evidence.jsonl"
    rows = [
        {"source": source, "congress": 109, "chamber": "house", "party": "D",
         "tier": tier, "disposition": "accepted"}
        for source, tier in [("hein_bound", "mild"), ("hein_daily", "strong")]
    ]
    evidence.write_text("".join(json.dumps(row) + "\n" for row in rows))
    frame = pd.DataFrame([
        {**row, "profanity_hits": 1, "profanity_mild_hits": int(row["tier"] == "mild"),
         "profanity_strong_hits": int(row["tier"] == "strong")}
        for row in rows
    ])
    validate_aggregate_counts(frame, evidence)
    frame[["profanity_mild_hits", "profanity_strong_hits"]] = (
        frame[["profanity_strong_hits", "profanity_mild_hits"]].to_numpy()
    )
    with pytest.raises(RuntimeError, match="original-source reference"):
        validate_aggregate_counts(frame, evidence)


@pytest.mark.parametrize("fail_ingestion", [False, True])
def test_one_download_retains_distinct_member_and_aggregate_quotation_policies(
    tmp_path, monkeypatch, fail_ingestion,
):
    rows = [{
        "turn_id": "crec:CREC-2017-03-01-pt1-PgH1#1", "source": "govinfo",
        "date": "2017-03-01", "congress": 115, "chamber": "house",
        "bioguide": "A", "speaker_name": "Mr. ALPHA", "party": "D",
        "word_count": 10, "is_procedural": False,
        "text": "That is damn hard. I quote ``fucking nonsense''.",
    }]
    source = tmp_path / "seed/govinfo_bulk_115.parquet"
    write_turns_parquet(source, iter(rows))
    daily = tmp_path / "daily"
    save_daily(speaker_counts([source]), daily)
    downloads = []

    def ingest(packages, bulk, out, **kwargs):
        downloads.append(list(packages))
        write_turns_parquet(out / "turns/govinfo_bulk_115.parquet", iter(rows))
        if fail_ingestion:
            raise RuntimeError("A later original package failed")
        return 1, []

    monkeypatch.setattr(audit_profanity_families, "discover_packages", lambda *a: [
        "CREC-2017-03-01",
    ])
    monkeypatch.setattr("analysis.ingest.govinfo_bulk.run_incremental_bulk", ingest)
    arguments = [
        "--fetch-congress", "115", "--out", str(tmp_path / "audit"),
        "--daily", str(daily), "--candidate-out", str(tmp_path / "members"),
        "--aggregate-out", str(tmp_path / "aggregates"),
        "--source-out", str(tmp_path / "originals"),
    ]
    if fail_ingestion:
        with pytest.raises(RuntimeError, match="later original package failed"):
            audit_profanity_families.main(arguments)
        assert pd.read_parquet(tmp_path / "originals/govinfo_bulk_115.parquet").iloc[0]["text"] == rows[0]["text"]
        assert not (tmp_path / "audit/summary.json").exists()
        return
    assert audit_profanity_families.main(arguments) == 0
    member = pd.read_parquet(tmp_path / "members/congress_115.parquet").iloc[0]
    aggregate = pd.read_parquet(tmp_path / "aggregates/congress_115.parquet").iloc[0]
    assert member.profanity_hits == 1
    assert member.profanity_quoted_hits == 1
    assert aggregate.profanity_hits == 2
    assert aggregate.profanity_mild_hits == aggregate.profanity_strong_hits == 1
    assert downloads == [["CREC-2017-03-01"]]
    retained = pd.read_parquet(tmp_path / "originals/govinfo_bulk_115.parquet")
    assert retained.iloc[0]["text"] == rows[0]["text"]


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


def test_multi_year_source_discovery_is_paginated_and_rejects_missing_stored_dates(monkeypatch):
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
    monkeypatch.setattr("analysis.ingest.govinfo_bulk.probe_packages", lambda *a, **kw: [])
    with pytest.raises(RuntimeError, match="omits 1 stored dates"):
        discover_packages("2003-01-03", "2005-01-02", {"2004-01-20"}, Client())


def test_api_omission_is_recovered_only_from_a_verified_existing_public_archive(monkeypatch):
    class Client:
        def paginate(self, *args, **kwargs):
            yield {"packageId": "CREC-2009-01-02"}

        def url(self, path):
            return path

    probes = []

    def probe(start, end, **kwargs):
        probes.append((start, end))
        return ["CREC-2009-01-03"]

    monkeypatch.setattr("analysis.ingest.govinfo_bulk.probe_packages", probe)
    assert discover_packages(
        "2007-01-01", "2009-01-03", {"2009-01-02", "2009-01-03"}, Client(),
    ) == ["CREC-2009-01-02", "CREC-2009-01-03"]
    assert probes == [("2009-01-03", "2009-01-03")]


def test_source_audit_keeps_known_outgoing_congress_boundary_dates():
    assert congress_window(103, "2026-10-17") == ("1994-01-25", "1995-01-03")
    assert congress_window(110, "2026-10-17") == ("2007-01-01", "2009-01-03")
    assert congress_window(119, "2026-10-17") == ("2025-01-01", "2026-10-17")
