import pandas as pd
import pytest

from scripts.rescore_audited_profanity import replace_counts, validate_quotation_extension


def _member_inputs():
    row = {
        "bioguide": "A", "date": "2025-03-01", "chamber": "house",
        "words": 1000, "turns": 10, "slur_hits": 4, "slur_quoted_hits": 2,
        "profanity_hits": 2, "profanity_quoted_hits": 1,
        "profanity_terms": '{"asses":1,"damn":1}',
    }
    baseline = pd.DataFrame([row])
    before = pd.DataFrame([{**row, "words": 20, "turns": 1, "slur_hits": 1}])
    after = before.copy()
    after.loc[0, "profanity_hits"] = 1
    after.loc[0, "profanity_terms"] = '{"damn":1}'
    return baseline, before, after


def test_source_replay_corrects_actual_forms_without_replacing_full_corpus_denominators():
    baseline, before, after = _member_inputs()
    corrected, changed = replace_counts(baseline, before, after, "members")
    assert changed == 1
    assert corrected.loc[0, "profanity_hits"] == 1
    assert corrected.loc[0, "profanity_terms"] == '{"damn":1}'
    for column in ["words", "turns", "slur_hits", "slur_quoted_hits"]:
        pd.testing.assert_series_equal(baseline[column], corrected[column])


def test_source_replay_rejects_incomplete_or_wrong_form_baseline_evidence():
    baseline, before, after = _member_inputs()
    before.loc[0, "profanity_terms"] = '{"crap":2}'
    with pytest.raises(ValueError, match="original forms"):
        replace_counts(baseline, before, after, "members")
    before.loc[0, "profanity_hits"] = 1
    with pytest.raises(ValueError, match="reproduce the baseline"):
        replace_counts(baseline, before, after, "members")


def test_profanity_only_replay_rejects_word_or_eligibility_changes():
    baseline, before, after = _member_inputs()
    after.loc[0, "words"] += 1
    with pytest.raises(ValueError, match="changed words"):
        replace_counts(baseline, before, after, "members")
    after.loc[0, "bioguide"] = "B"
    with pytest.raises(ValueError, match="identities"):
        replace_counts(baseline, before, after, "members")


def test_reviewed_quotation_replay_moves_only_original_words_and_slurs_into_quoted_counts():
    baseline, before, after = _member_inputs()
    after.loc[0, "words"] -= 4
    after.loc[0, "profanity_quoted_hits"] += 1
    after.loc[0, "slur_hits"] -= 1
    after.loc[0, "slur_quoted_hits"] += 1
    corrected, changed = replace_counts(
        baseline, before, after, "members", quotation_extension=True,
    )
    assert changed == 1
    assert corrected.loc[0, "words"] == 996
    assert corrected.loc[0, "profanity_quoted_hits"] == 2
    assert corrected.loc[0, "slur_hits"] == 3
    assert corrected.loc[0, "slur_quoted_hits"] == 3
    after.loc[0, "slur_quoted_hits"] += 1
    with pytest.raises(ValueError, match="more than quoted attribution"):
        replace_counts(baseline, before, after, "members", quotation_extension=True)


def test_quotation_replay_requires_complete_candidate_coverage_and_unchanged_member_logic(tmp_path):
    before, after = tmp_path / "before.py", tmp_path / "after.py"
    before.write_text("_REVIEWED_SOURCE_QUOTES = {}\ndef member_rule(): return 'stable'\n")
    after.write_text(
        "_REVIEWED_SOURCE_QUOTES = {'source': (('Those bastards.', 'Own words.'),)}\n"
        "def member_rule(): return 'stable'\n"
    )
    assert validate_quotation_extension(before, after)
    after.write_text(after.read_text().replace("Those bastards.", "External transcript."))
    with pytest.raises(ValueError, match="complete profanity occurrence"):
        validate_quotation_extension(before, after)
    after.write_text(before.read_text().replace("'stable'", "'changed'"))
    with pytest.raises(ValueError, match="cannot change member scoring"):
        validate_quotation_extension(before, after)


def test_aggregate_replay_recomputes_rates_from_real_full_words_not_sparse_cache_words():
    row = {
        "source": "hein_bound", "congress": 43, "chamber": "house", "party": "D",
        "words": 1000, "turns": 10, "slur_hits": 4, "formal_courtesy_hits": 5,
        "profanity_hits": 3, "profanity_mild_hits": 2, "profanity_strong_hits": 1,
        "profanity_per_1k": 3.0, "profanity_mild_per_1k": 2.0,
        "profanity_strong_per_1k": 1.0,
    }
    baseline = pd.DataFrame([row])
    before = pd.DataFrame([{**row, "words": 20, "turns": 1, "slur_hits": 1}])
    after = before.copy()
    after.loc[0, ["profanity_hits", "profanity_strong_hits"]] = [2, 0]
    corrected, changed = replace_counts(baseline, before, after, "aggregate")
    assert changed == 1
    assert corrected.loc[0, "profanity_per_1k"] == 2.0
    assert corrected.loc[0, "profanity_mild_per_1k"] == 2.0
    assert corrected.loc[0, "profanity_strong_per_1k"] == 0.0
    for column in ["words", "turns", "slur_hits", "formal_courtesy_hits"]:
        pd.testing.assert_series_equal(baseline[column], corrected[column])
