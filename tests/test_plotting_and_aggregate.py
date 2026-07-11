"""Tests for aggregate source-dedup and the shared plotting theme."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from analysis.aggregate import _select_turn_files, score_and_aggregate  # noqa: E402
from analysis.ingest.schema import ARROW_SCHEMA  # noqa: E402
from analysis.plotting import theme  # noqa: E402


def _empty_parquet(path: Path) -> None:
    pq.write_table(pa.Table.from_pylist([], schema=ARROW_SCHEMA), path)


def test_select_turn_files_keeps_bulk_and_manifest_for_union(tmp_path=None) -> None:
    d = Path(__file__).resolve().parent / "_tmp_turns"
    d.mkdir(exist_ok=True)
    try:
        for name in ("hein_100.parquet", "govinfo_115.parquet",
                     "govinfo_bulk_115.parquet", "govinfo_118.parquet"):
            _empty_parquet(d / name)
        picked = {p.name for p in _select_turn_files(d)}
        # Both 115 paths remain so a partial bulk file cannot suppress manifest coverage.
        assert "govinfo_bulk_115.parquet" in picked
        assert "govinfo_115.parquet" in picked
        assert "govinfo_118.parquet" in picked
        assert "hein_100.parquet" in picked
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_aggregate_unions_and_deduplicates_govinfo_turns() -> None:
    import shutil

    root = Path(__file__).resolve().parent / "_tmp_union"
    turns = root / "turns"
    out = root / "processed"
    turns.mkdir(parents=True, exist_ok=True)

    def row(turn_id: str, text: str):
        return {
            "turn_id": turn_id, "source": "govinfo", "congress": 115, "year": 2017,
            "date": "2017-01-03", "chamber": "house", "session": None,
            "speaker_id": None, "speaker_name": "Mr. TEST", "party": "D", "state": "CA",
            "text": text, "is_procedural": False,
        }

    try:
        pq.write_table(
            pa.Table.from_pylist([row("same", "damn"), row("bulk-only", "hello")],
                                 schema=ARROW_SCHEMA),
            turns / "govinfo_bulk_115.parquet",
        )
        pq.write_table(
            pa.Table.from_pylist([row("same", "damn"), row("manifest-only", "world")],
                                 schema=ARROW_SCHEMA),
            turns / "govinfo_115.parquet",
        )
        metrics = score_and_aggregate(turns, out)
        got = metrics.iloc[0]
        assert got["turns"] == 3
        assert got["words"] == 3
        assert got["profanity_hits"] == 1
        coverage = out / "coverage" / "turn_coverage.csv"
        assert coverage.exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_theme_palette_and_apply() -> None:
    # Party colours resolve to the shared palette and apply() sets the cream bg.
    assert theme.PARTY_COLORS["D"] == theme.BLUE
    assert theme.PARTY_COLORS["R"] == theme.ACCENT
    theme.apply()
    import matplotlib.pyplot as plt
    assert plt.rcParams["figure.facecolor"] in (theme.BG, (0.968627, 0.960784, 0.941176, 1.0)) \
        or plt.rcParams["axes.spines.top"] is False


def test_chamber_party_aggregation_word_weighted() -> None:
    import pandas as pd
    from analysis.viz import _by_year_chamber_party
    # two House-D rows in the same congress must combine word-weighted, not mean-of-rates.
    df = pd.DataFrame([
        {"congress": 119, "year": 2025, "chamber": "house", "party": "D",
         "hostility_hits": 10, "words": 1000, "comity_hits": 0, "profanity_hits": 0,
         "profanity_slurs_hits": 0, "outgroup_refs": 0, "democrat_party_pej": 0,
         "directed_comity_hits": 0, "directed_hostility_hits": 0},
        {"congress": 119, "year": 2025, "chamber": "house", "party": "D",
         "hostility_hits": 0, "words": 9000, "comity_hits": 0, "profanity_hits": 0,
         "profanity_slurs_hits": 0, "outgroup_refs": 0, "democrat_party_pej": 0,
         "directed_comity_hits": 0, "directed_hostility_hits": 0},
    ])
    g = _by_year_chamber_party(df)
    row = g[(g.chamber == "house") & (g.party == "D")].iloc[0]
    # 10 hits / 10000 words * 1000 = 1.0  (NOT the mean of 10.0 and 0.0 = 5.0)
    assert abs(row["hostility_per_1k"] - 1.0) < 1e-9



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
