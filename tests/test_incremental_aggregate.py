"""Tests for incremental (shard-cached) aggregation.

The load-bearing property is that reusing cached shards must produce exactly the
same metrics as rescoring everything -- otherwise the speedup silently changes
published numbers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.aggregate import score_and_aggregate  # noqa: E402
from analysis.incremental import (  # noqa: E402
    ShardCache,
    config_fingerprint,
    plan_shards,
    shard_key,
)
from analysis.ingest.schema import ARROW_SCHEMA  # noqa: E402


def _row(turn_id: str, text: str, congress: int = 115, party: str = "D", source: str = "govinfo"):
    return {
        "turn_id": turn_id, "source": source, "congress": congress, "year": 2017,
        "date": "2017-01-03", "chamber": "house", "session": None,
        "speaker_id": None, "speaker_name": "Mr. TEST", "party": party, "state": "CA",
        "text": text, "is_procedural": False,
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=ARROW_SCHEMA), path)


@pytest.fixture()
def corpus(tmp_path: Path):
    turns = tmp_path / "turns"
    _write(
        turns / "govinfo_bulk_115.parquet",
        [_row("a", "damn the gentleman"), _row("b", "I thank my friend")],
    )
    _write(
        turns / "govinfo_bulk_119.parquet",
        [_row("c", "the gentleman is corrupt", congress=119, party="R")],
    )
    _write(
        turns / "hein_100.parquet",
        [_row("h1", "I thank the distinguished senator", congress=100, source="hein_bound")],
    )
    return tmp_path, turns


# --------------------------------------------------------------------------- sharding


def test_govinfo_files_of_same_congress_share_a_shard():
    # Both GovInfo ingesters mint "crec:<granuleId>#<n>" ids, so same-Congress files
    # must be scored together for deduplication to see the duplicates.
    assert shard_key(Path("govinfo_115.parquet")) == shard_key(Path("govinfo_bulk_115.parquet"))
    assert shard_key(Path("govinfo_115.parquet")) != shard_key(Path("govinfo_119.parquet"))
    assert shard_key(Path("hein_100.parquet")) != shard_key(Path("govinfo_100.parquet"))


def test_plan_shards_groups_and_orders_deterministically(tmp_path: Path):
    names = ["govinfo_bulk_115.parquet", "govinfo_115.parquet", "hein_100.parquet"]
    for name in names:
        (tmp_path / name).touch()
    shards = plan_shards([tmp_path / n for n in names])
    assert [s.key for s in shards] == ["file:hein_100", "govinfo:115"]
    assert [p.name for p in shards[1].files] == ["govinfo_115.parquet", "govinfo_bulk_115.parquet"]


# --------------------------------------------------------------- exactness guarantees


def _metrics(turns: Path, out: Path, **kwargs) -> pd.DataFrame:
    return score_and_aggregate(turns, out, **kwargs).sort_values(
        ["congress", "chamber", "party"]
    ).reset_index(drop=True)


def test_cached_run_matches_full_recompute_exactly(corpus):
    root, turns = corpus
    cold = _metrics(turns, root / "cold", incremental=True)
    warm = _metrics(turns, root / "cold", incremental=True)  # same out dir -> cache hit
    full = _metrics(turns, root / "full", incremental=False)

    pd.testing.assert_frame_equal(cold, warm)
    pd.testing.assert_frame_equal(cold, full)


def test_second_run_reuses_cache_without_rescoring(corpus, caplog):
    root, turns = corpus
    _metrics(turns, root / "out", incremental=True)
    with caplog.at_level("INFO", logger="analysis.aggregate"):
        _metrics(turns, root / "out", incremental=True)
    assert "3 reused from cache" in caplog.text
    assert "0 rescored" in caplog.text


def test_only_the_changed_shard_is_rescored(corpus, caplog):
    root, turns = corpus
    _metrics(turns, root / "out", incremental=True)
    # A new day of transcripts lands in the 119th Congress only.
    _write(
        turns / "govinfo_bulk_119.parquet",
        [
            _row("c", "the gentleman is corrupt", congress=119, party="R"),
            _row("d", "I thank my colleague", congress=119, party="R"),
        ],
    )
    with caplog.at_level("INFO", logger="analysis.aggregate"):
        _metrics(turns, root / "out", incremental=True)
    assert "1 rescored" in caplog.text
    assert "2 reused from cache" in caplog.text


def test_incremental_update_matches_full_recompute(corpus):
    root, turns = corpus
    _metrics(turns, root / "out", incremental=True)
    _write(
        turns / "govinfo_bulk_119.parquet",
        [
            _row("c", "the gentleman is corrupt", congress=119, party="R"),
            _row("d", "I thank my colleague", congress=119, party="R"),
        ],
    )
    incremental = _metrics(turns, root / "out", incremental=True)
    full = _metrics(turns, root / "fresh", incremental=False)
    pd.testing.assert_frame_equal(incremental, full)


def test_new_congress_file_is_picked_up(corpus):
    root, turns = corpus
    before = _metrics(turns, root / "out", incremental=True)
    _write(
        turns / "govinfo_bulk_120.parquet",
        [_row("z", "damn", congress=120, party="D")],
    )
    after = _metrics(turns, root / "out", incremental=True)
    assert 120 not in set(before["congress"])
    assert 120 in set(after["congress"])
    pd.testing.assert_frame_equal(after, _metrics(turns, root / "fresh", incremental=False))


# ------------------------------------------------------------------ invalidation


def test_lexicon_change_invalidates_every_shard(corpus, monkeypatch, tmp_path):
    root, turns = corpus
    _metrics(turns, root / "out", incremental=True)
    baseline = config_fingerprint(False, False)

    # Point the scorer at a modified lexicon directory; the fingerprint must move.
    from analysis.score import scorers as scorers_module

    fake_lex = tmp_path / "lex"
    fake_lex.mkdir()
    for src in Path(scorers_module.LEXDIR).glob("*.txt"):
        (fake_lex / src.name).write_bytes(src.read_bytes())
    (fake_lex / "hostility.txt").write_text("newword\n", encoding="utf-8")
    monkeypatch.setattr(scorers_module, "LEXDIR", fake_lex)

    assert config_fingerprint(False, False) != baseline


def test_scoring_flags_are_part_of_the_fingerprint():
    base = config_fingerprint(False, False)
    assert config_fingerprint(True, False) != base
    assert config_fingerprint(False, True) != base


def test_display_only_changes_do_not_invalidate_the_cache(tmp_path):
    # A metric's title is display-only. Hashing whole modules meant renaming a chart
    # label invalidated all 89 shards and forced a ~74 minute rescore that could not
    # change a single number.
    import importlib

    import analysis.aggregate as aggregate_module
    import analysis.score.registry as registry_module

    registry_path = Path(registry_module.__file__)
    original = registry_path.read_text(encoding="utf-8")
    base = config_fingerprint(False, False)
    try:
        registry_path.write_text(
            original.replace('"words", 1000, "profanity", "Profanity",',
                             '"words", 1000, "profanity", "Swearing",'),
            encoding="utf-8",
        )
        importlib.reload(registry_module)
        importlib.reload(aggregate_module)
        assert config_fingerprint(False, False) == base
    finally:
        registry_path.write_text(original, encoding="utf-8")
        importlib.reload(registry_module)
        importlib.reload(aggregate_module)
    assert config_fingerprint(False, False) == base


def test_changing_an_accumulated_score_key_invalidates_the_cache(monkeypatch):
    import analysis.aggregate as aggregate_module
    import analysis.score.registry as registry_module

    base = config_fingerprint(False, False)
    monkeypatch.setattr(
        registry_module, "SCORE_KEYS", tuple(registry_module.SCORE_KEYS) + ("new_key",)
    )
    assert config_fingerprint(False, False) != base
    monkeypatch.undo()
    monkeypatch.setattr(
        aggregate_module, "_SUM_KEYS", list(aggregate_module._SUM_KEYS) + ["extra"]
    )
    assert config_fingerprint(False, False) != base


def test_changing_the_shard_scoring_function_invalidates_the_cache(monkeypatch):
    import analysis.aggregate as aggregate_module

    base = config_fingerprint(False, False)

    def _replacement(shard, scorers, include_procedural):  # different source text
        return {}, {}

    monkeypatch.setattr(aggregate_module, "_score_shard", _replacement)
    assert config_fingerprint(False, False) != base


def test_changed_fingerprint_discards_cached_entries(corpus):
    root, turns = corpus
    _metrics(turns, root / "out", incremental=True)
    cache_file = root / "out" / "cache" / "aggregate_shards.json"
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    payload["config_fingerprint"] = "stale-fingerprint"
    cache_file.write_text(json.dumps(payload), encoding="utf-8")

    cache = ShardCache(cache_file, config_fingerprint(False, False))
    shards = plan_shards(sorted(turns.glob("*.parquet")))
    assert all(cache.get(shard) is None for shard in shards)


def test_touching_a_file_invalidates_only_that_shard(corpus, caplog):
    root, turns = corpus
    _metrics(turns, root / "out", incremental=True)
    target = turns / "hein_100.parquet"
    # Rewriting with identical content still changes size/mtime -> conservative miss.
    _write(target, [_row("h1", "I thank the distinguished senator", congress=100,
                         source="hein_bound")])
    with caplog.at_level("INFO", logger="analysis.aggregate"):
        _metrics(turns, root / "out", incremental=True)
    assert "1 rescored" in caplog.text


def test_corrupt_cache_file_is_ignored_not_fatal(corpus):
    root, turns = corpus
    _metrics(turns, root / "out", incremental=True)
    cache_file = root / "out" / "cache" / "aggregate_shards.json"
    cache_file.write_text("{not json", encoding="utf-8")
    recovered = _metrics(turns, root / "out", incremental=True)
    pd.testing.assert_frame_equal(recovered, _metrics(turns, root / "fresh", incremental=False))


def test_deleted_file_drops_its_shard_from_cache_and_metrics(corpus):
    root, turns = corpus
    _metrics(turns, root / "out", incremental=True)
    (turns / "govinfo_bulk_119.parquet").unlink()
    after = _metrics(turns, root / "out", incremental=True)
    assert 119 not in set(after["congress"])
    payload = json.loads((root / "out" / "cache" / "aggregate_shards.json").read_text())
    assert "govinfo:119" not in payload["shards"]
    pd.testing.assert_frame_equal(after, _metrics(turns, root / "fresh", incremental=False))


def test_duplicate_govinfo_turns_still_deduplicated_via_cache(tmp_path: Path):
    turns = tmp_path / "turns"
    _write(turns / "govinfo_bulk_115.parquet", [_row("same", "damn"), _row("bulk", "hello")])
    _write(turns / "govinfo_115.parquet", [_row("same", "damn"), _row("manifest", "world")])
    cold = _metrics(turns, tmp_path / "out", incremental=True)
    warm = _metrics(turns, tmp_path / "out", incremental=True)
    assert cold.iloc[0]["turns"] == 3
    assert cold.iloc[0]["profanity_hits"] == 1
    pd.testing.assert_frame_equal(cold, warm)


def test_coverage_and_source_artifacts_match_full_recompute(corpus):
    root, turns = corpus
    _metrics(turns, root / "out", incremental=True)
    _metrics(turns, root / "out", incremental=True)  # served from cache
    _metrics(turns, root / "fresh", incremental=False)

    for name in ("turn_coverage.csv", "source_metadata.json"):
        cached = (root / "out" / "coverage" / name).read_text(encoding="utf-8")
        full = (root / "fresh" / "coverage" / name).read_text(encoding="utf-8")
        assert cached == full, f"{name} differs between cached and full runs"

    for name in ("civility_metrics.csv", "civility_metrics_by_source.csv"):
        cached = (root / "out" / "metrics" / name).read_text(encoding="utf-8")
        full = (root / "fresh" / "metrics" / name).read_text(encoding="utf-8")
        assert cached == full, f"{name} differs between cached and full runs"


def test_integer_coverage_counts_survive_the_json_round_trip(corpus):
    root, turns = corpus
    _metrics(turns, root / "out", incremental=True)
    _metrics(turns, root / "out", incremental=True)
    frame = pd.read_csv(root / "out" / "coverage" / "turn_coverage.csv")
    for column in ("total_turns", "total_words", "nonprocedural_turns"):
        assert frame[column].dtype.kind == "i", f"{column} should stay integral"


def test_interrupted_run_resumes_from_completed_shards(corpus, monkeypatch, caplog):
    root, turns = corpus
    import analysis.aggregate as aggregate_module

    # Interrupt via _merge_groups, which runs just after each shard is cached and is
    # deliberately not part of the config fingerprint -- patching _score_shard itself
    # would change the fingerprint and invalidate the very cache under test.
    real_merge = aggregate_module._merge_groups
    calls: list[int] = []

    def explode_on_third(target, addition):
        calls.append(1)
        if len(calls) == 5:  # acc+coverage per shard: fails partway through shard 3
            raise KeyboardInterrupt("simulated interrupt")
        return real_merge(target, addition)

    monkeypatch.setattr(aggregate_module, "_merge_groups", explode_on_third)
    with pytest.raises(KeyboardInterrupt):
        _metrics(turns, root / "out", incremental=True)
    monkeypatch.undo()

    # Shards completed before the interrupt must not be rescored.
    with caplog.at_level("INFO", logger="analysis.aggregate"):
        resumed = _metrics(turns, root / "out", incremental=True)
    assert "reused from cache" in caplog.text
    assert "0 rescored" in caplog.text
    pd.testing.assert_frame_equal(resumed, _metrics(turns, root / "fresh", incremental=False))
