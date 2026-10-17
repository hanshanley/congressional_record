import json

import pytest
import yaml

from analysis.ingest.schema import write_turns_parquet
from scripts.prepare_hein_batches import ROOT, prepare, validate_batch


def test_complete_historical_sources_are_retained_in_six_verified_disjoint_batches(tmp_path):
    turns, out = tmp_path / "turns", tmp_path / "retained"
    for congress in range(43, 115):
        write_turns_parquet(turns / f"hein_{congress:03d}.parquet", iter([{
            "turn_id": f"hein:{congress}", "congress": congress,
            "source": "hein_bound" if congress < 97 else "hein_daily",
            "date": f"{1787 + 2 * congress}-01-01", "chamber": "house", "party": "D",
            "speaker_name": "Original member", "bioguide": "", "word_count": 4,
            "is_procedural": False, "text": "Complete original floor remarks.",
        }]))
    index = prepare(turns, out)
    covered = []
    for label in index:
        manifest = validate_batch(out / label)
        covered.extend(manifest["congresses"])
    assert covered == list(range(43, 115))
    assert len(index) == 6
    first = out / "43-54"
    manifest_path = first / "source_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["hein_043.parquet"]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="original source changed"):
        validate_batch(first)
    (turns / "hein_055.parquet").unlink()
    with pytest.raises(ValueError, match="Incomplete original Stanford coverage"):
        prepare(turns, tmp_path / "incomplete")


def test_historical_recovery_retains_originals_before_scoring_and_never_publishes():
    text = (ROOT / ".github/workflows/audit-historical-batches.yml").read_text()
    workflow = yaml.safe_load(text)
    assert workflow["permissions"]["contents"] == "read"
    assert workflow["jobs"]["score"]["needs"] == "originals"
    assert "ingest-govinfo" not in text
    assert "git push" not in text
    assert "validate_aggregate_counts" in text
