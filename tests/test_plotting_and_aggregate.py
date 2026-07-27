"""Tests for aggregate source-dedup and the shared plotting theme."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from analysis.aggregate import (  # noqa: E402
    _select_primary_source,
    score_and_aggregate,
)
from analysis.ingest.schema import ARROW_SCHEMA  # noqa: E402
from analysis.inputs import select_turn_files  # noqa: E402
from analysis.plotting import charts, theme  # noqa: E402
from analysis.calibrate import calibration_summary, paired_overlap  # noqa: E402
from analysis.score.registry import METRICS  # noqa: E402


def _empty_parquet(path: Path) -> None:
    pq.write_table(pa.Table.from_pylist([], schema=ARROW_SCHEMA), path)


def test_select_turn_files_keeps_bulk_and_manifest_for_union(tmp_path=None) -> None:
    d = Path(__file__).resolve().parent / "_tmp_turns"
    d.mkdir(exist_ok=True)
    try:
        for name in ("hein_100.parquet", "govinfo_115.parquet",
                     "govinfo_bulk_115.parquet", "govinfo_118.parquet"):
            _empty_parquet(d / name)
        picked = {p.name for p in select_turn_files(d)}
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


def test_overlap_calibration_pairs_sources() -> None:
    import pandas as pd

    rows = []
    for source, multiplier in (("hein_daily", 1.0), ("govinfo", 2.0)):
        for congress in (103, 104):
            row = {
                "source": source, "congress": congress, "year": 1993 + 2 * (congress - 103),
                "chamber": "house", "party": "D", "words": 1000, "outgroup_refs": 10,
            }
            for metric in METRICS:
                row[metric.raw_count] = 2 * multiplier
            row["outgroup_refs"] = 10
            rows.append(row)
    pairs = paired_overlap(pd.DataFrame(rows))
    assert not pairs.empty
    summary = calibration_summary(pairs)
    assert set(summary["metric"]) == set(pairs["metric"])


def test_primary_metrics_do_not_combine_overlap_sources() -> None:
    import pandas as pd

    frame = pd.DataFrame([
        {"source": "hein_daily", "congress": 114, "chamber": "house", "party": "D",
         "hostility_hits": 1},
        {"source": "govinfo", "congress": 114, "chamber": "house", "party": "D",
         "hostility_hits": 99},
        {"source": "govinfo", "congress": 115, "chamber": "house", "party": "D",
         "hostility_hits": 2},
    ])
    selected = _select_primary_source(frame)
    assert selected[selected.congress.eq(114)].iloc[0]["hostility_hits"] == 1
    assert selected[selected.congress.eq(115)].iloc[0]["hostility_hits"] == 2



def test_chamber_styling_differs_on_multiple_visual_channels() -> None:
    # Chamber used to differ only by dash pattern, which was unreadable when two
    # same-party lines overlapped. Colour depth, dash and marker must all differ.
    house = theme.CHAMBER_STYLE["house"]
    senate = theme.CHAMBER_STYLE["senate"]
    assert house["linestyle"] != senate["linestyle"]
    assert house["marker"] != senate["marker"]
    for party in ("D", "R"):
        assert theme.chamber_color(party, "house") != theme.chamber_color(party, "senate")


def test_chamber_colour_keeps_party_hue_and_darkens_the_senate() -> None:
    for party in ("D", "R"):
        base = theme.PARTY_COLORS[party]
        assert theme.chamber_color(party, "house") == base
        senate = theme.chamber_color(party, "senate")
        # Same hue family, strictly darker: every channel drops toward black.
        assert all(s <= b for s, b in zip(theme._hex_to_rgb(senate), theme._hex_to_rgb(base)))
        assert sum(theme._hex_to_rgb(senate)) < sum(theme._hex_to_rgb(base))


def test_parties_remain_distinguishable_within_a_chamber() -> None:
    for chamber in ("house", "senate"):
        assert theme.chamber_color("D", chamber) != theme.chamber_color("R", chamber)


def test_shade_and_tint_are_bounded_and_ordered() -> None:
    assert theme.shade("#3D6F8C", 0.0) == "#3D6F8C"
    assert theme.tint("#3D6F8C", 0.0) == "#3D6F8C"
    # Full tint approaches white, full shade approaches near-black; both stay valid hex.
    assert theme.tint("#3D6F8C", 1.0) == "#FFFFFF"
    for value in (theme.shade("#3D6F8C", 1.0), theme.tint("#C85A3D", 0.5)):
        assert len(value) == 7 and value.startswith("#")
        assert all(0.0 <= c <= 1.0 for c in theme._hex_to_rgb(value))


def test_source_note_wraps_long_text_to_multiple_lines() -> None:
    # Figures save with bbox_inches="tight", so an unwrapped note sets the saved
    # width and leaves a band of empty space to the right of the axes.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    long_note = (
        "Sources: Stanford Hein (1873-2017) + GovInfo CREC (2017-present). "
        "House/Senate only; Extensions excluded. Units shown on y-axis. "
        "Dotted line: 2017 source boundary. GovInfo party coverage varies; "
        "see coverage/turn_coverage.csv."
    )
    fig = plt.figure()
    lines = theme.source_note(fig, long_note)
    assert lines >= 2
    drawn = [t.get_text() for t in fig.texts][0]
    assert "\n" in drawn
    # Wrapping must not drop or reorder any content.
    assert " ".join(drawn.split()) == " ".join(long_note.split())
    assert max(len(part) for part in drawn.split("\n")) <= 118
    plt.close(fig)


def test_short_source_note_stays_on_one_line() -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure()
    assert theme.source_note(fig, "Source: GovInfo.") == 1
    plt.close(fig)


def test_marker_line_draws_no_caption() -> None:
    # The publication figures no longer draw the source boundary; the helper stays
    # for the calibration diagnostic and must remain a plain, unlabelled rule.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    charts.marker_line(ax, 2017)
    assert not list(ax.texts)
    assert len(ax.lines) == 1
    plt.close(fig)


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
