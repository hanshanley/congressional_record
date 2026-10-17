import hashlib
import json

import pytest

from analysis.ingest.schema import write_turns_parquet
from analysis.score.scorers import Scorers
from scripts.audit_profanity_families import audit
from scripts.compile_profanity_context_reviews import compile_reviews


@pytest.fixture
def original_review(tmp_path):
    text = "The final cunt is twenty votes. That bastard lied."
    source = tmp_path / "original.parquet"
    write_turns_parquet(source, iter([{
        "turn_id": "hein_bound:example", "source": "hein_bound", "text": text,
        "congress": 43, "date": "1873-03-04", "chamber": "house",
        "party": "D", "bioguide": "", "speaker_name": "Original member",
        "word_count": 10, "is_procedural": False,
    }]))
    directory = tmp_path / "audit"
    audit([source], directory, mode="aggregate")
    rows = [json.loads(line) for line in (directory / "evidence.jsonl").read_text().splitlines()]
    decisions = [{
        **row, "decision": "exclude_nonprofane" if row["form"] == "cunt" else "retain",
        "confidence": 0.99, "reason": "Count noun is an OCR error; separate insult is genuine.",
    } for row in rows]
    reviews = tmp_path / "reviews.jsonl"
    reviews.write_text("".join(json.dumps(row) + "\n" for row in decisions))
    return directory, reviews, text, decisions


def test_review_compiler_uses_the_complete_original_hash_and_preserves_real_curses(original_review):
    directory, reviews, text, _ = original_review
    manifest, report = compile_reviews(directory, [reviews])
    assert report["reviewed_occurrences"] == 2
    assert report["compiled_exclusion_spans"] == 1
    assert manifest["records"][0]["text_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    corrected = Scorers(profanity_contexts=manifest["records"])
    assert corrected.profanity_term_counts(text) == {"bastard": 1}
    assert corrected.score_turn(text)["n_words"] == Scorers().score_turn(text)["n_words"]


def test_review_compiler_rejects_unmatched_original_offsets_and_duplicate_decisions(original_review):
    directory, reviews, _, rows = original_review
    with pytest.raises(ValueError, match="Duplicate reviewed"):
        compile_reviews(directory, [reviews, reviews])
    rows[0]["start"] += 1
    reviews.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="absent from current original evidence"):
        compile_reviews(directory, [reviews])


def test_review_compiler_keeps_uncertainty_out_of_the_exclusion_codebook(original_review):
    directory, reviews, _, rows = original_review
    rows[0]["decision"] = "uncertain"
    rows[0]["confidence"] = 0.5
    reviews.write_text("".join(json.dumps(row) + "\n" for row in rows))
    manifest, report = compile_reviews(directory, [reviews])
    assert manifest["records"] == []
    assert report["decisions"] == {"uncertain": 1, "retain": 1}
