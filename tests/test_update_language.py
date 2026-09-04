from __future__ import annotations

import datetime as dt
from argparse import Namespace
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from analysis.daily_language import (
    aggregate_turn_files,
    merge_long_run_payload,
    replace_daily_window,
)
from analysis.ingest.schema import ARROW_SCHEMA
from scripts.update_language import probe_windows, resolve_window


def _args(**overrides):
    values = {
        "since": None,
        "until": "2026-09-04",
        "lookback_days": 7,
    }
    values.update(overrides)
    return Namespace(**values)


def _daily(rows):
    return pd.DataFrame(
        rows,
        columns=[
            "date",
            "congress",
            "chamber",
            "party",
            "words",
            "formal_courtesy_hits",
            "gratitude_praise_hits",
            "cooperation_hits",
            "hostility_hits",
            "misconduct_hits",
            "profanity_hits",
        ],
    )


def test_first_daily_run_backfills_current_congress():
    assert resolve_window(
        _args(),
        None,
        today=dt.date(2026, 9, 4),
    ) == ("2025-01-01", "2026-09-04")


def test_daily_window_rechecks_one_week_without_crossing_congress_start():
    daily = _daily([
        ["2025-01-04", 119, "house", "D", 1, 0, 0, 0, 0, 0, 0],
    ])
    assert resolve_window(
        _args(until="2025-01-05"),
        daily,
        today=dt.date(2025, 1, 5),
    ) == ("2025-01-01", "2025-01-05")


def test_explicit_window_is_preserved():
    assert resolve_window(
        _args(since="2026-08-01", until="2026-08-31"),
        None,
    ) == ("2026-08-01", "2026-08-31")


def test_current_congress_backfill_uses_safe_probe_windows():
    windows = probe_windows("2025-01-01", "2026-09-04")
    assert windows == [
        ("2025-01-01", "2025-12-31"),
        ("2026-01-01", "2026-09-04"),
    ]


def test_recomputed_window_removes_stale_daily_rows():
    existing = _daily([
        ["2026-09-01", 119, "house", "D", 100, 1, 0, 0, 0, 0, 0],
        ["2026-09-02", 119, "house", "D", 100, 1, 0, 0, 0, 0, 0],
    ])
    fresh = _daily([
        ["2026-09-02", 119, "house", "R", 200, 2, 0, 0, 0, 0, 0],
    ])
    result = replace_daily_window(existing, fresh, "2026-09-02", "2026-09-04")
    assert list(result[["date", "party"]].itertuples(index=False, name=None)) == [
        ("2026-09-01", "D"),
        ("2026-09-02", "R"),
    ]


def test_turn_aggregation_deduplicates_and_excludes_ineligible_rows(tmp_path):
    def row(turn_id, *, party="D", chamber="house", procedural=False):
        return {
            "turn_id": turn_id,
            "source": "govinfo",
            "date": "2026-09-02",
            "congress": 119,
            "chamber": chamber,
            "speaker_name": "Member",
            "speaker_id": "",
            "bioguide": "A000001",
            "party": party,
            "state": "CA",
            "word_count": 1,
            "is_procedural": procedural,
            "text": "damn",
        }

    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    pq.write_table(
        pa.Table.from_pylist(
            [
                row("accepted"),
                row("procedural", procedural=True),
                row("independent", party="I"),
                row("extensions", chamber="extensions"),
            ],
            schema=ARROW_SCHEMA,
        ),
        first,
    )
    pq.write_table(
        pa.Table.from_pylist([row("accepted")], schema=ARROW_SCHEMA),
        second,
    )

    result = aggregate_turn_files([first, second])

    assert len(result) == 1
    assert result.iloc[0]["words"] == 1
    assert result.iloc[0]["profanity_hits"] == 1


def test_daily_metrics_replace_current_year_without_changing_history():
    base = {
        "metrics": {},
        "series": [
            {
                "year": 2023,
                "party": "D",
                "words": 10,
                "formal_courtesy_hits": 1,
                "gratitude_praise_hits": 0,
                "cooperation_hits": 0,
                "hostility_hits": 0,
                "misconduct_hits": 0,
                "profanity_hits": 0,
                "formal_courtesy_per_1k": 100.0,
                "gratitude_praise_per_1k": 0.0,
                "cooperation_per_1k": 0.0,
                "hostility_per_1k": 0.0,
                "misconduct_per_1k": 0.0,
                "profanity_per_1k": 0.0,
            },
            {
                "year": 2025,
                "party": "D",
                "words": 999,
                "formal_courtesy_hits": 999,
                "gratitude_praise_hits": 999,
                "cooperation_hits": 999,
                "hostility_hits": 999,
                "misconduct_hits": 999,
                "profanity_hits": 999,
                "formal_courtesy_per_1k": 1000.0,
                "gratitude_praise_per_1k": 1000.0,
                "cooperation_per_1k": 1000.0,
                "hostility_per_1k": 1000.0,
                "misconduct_per_1k": 1000.0,
                "profanity_per_1k": 1000.0,
            },
        ],
        "chamber_series": [
            {
                "year": 2023,
                "party": "D",
                "chamber": "house",
                "words": 10,
                "formal_courtesy_hits": 1,
                "gratitude_praise_hits": 0,
                "cooperation_hits": 0,
                "hostility_hits": 0,
                "misconduct_hits": 0,
                "profanity_hits": 0,
                "formal_courtesy_per_1k": 100.0,
                "gratitude_praise_per_1k": 0.0,
                "cooperation_per_1k": 0.0,
                "hostility_per_1k": 0.0,
                "misconduct_per_1k": 0.0,
                "profanity_per_1k": 0.0,
            },
            {
                "year": 2025,
                "party": "D",
                "chamber": "house",
                "words": 999,
                "formal_courtesy_hits": 999,
                "gratitude_praise_hits": 999,
                "cooperation_hits": 999,
                "hostility_hits": 999,
                "misconduct_hits": 999,
                "profanity_hits": 999,
                "formal_courtesy_per_1k": 1000.0,
                "gratitude_praise_per_1k": 1000.0,
                "cooperation_per_1k": 1000.0,
                "hostility_per_1k": 1000.0,
                "misconduct_per_1k": 1000.0,
                "profanity_per_1k": 1000.0,
            },
        ],
        "first_year": 2023,
        "last_year": 2025,
        "source_note": "test",
    }
    daily = _daily([
        ["2026-09-02", 119, "house", "D", 200, 2, 1, 1, 1, 1, 1],
        ["2026-09-02", 119, "senate", "D", 300, 3, 2, 2, 2, 2, 2],
    ])

    result = merge_long_run_payload(base, daily)

    historical = next(row for row in result["series"] if row["year"] == 2023)
    current = next(row for row in result["series"] if row["year"] == 2025)
    assert historical["words"] == 10
    assert current["words"] == 500
    assert current["formal_courtesy_hits"] == 5
    assert current["formal_courtesy_per_1k"] == 10.0
    assert len([row for row in result["chamber_series"] if row["year"] == 2025]) == 2


def test_daily_workflow_updates_every_data_surface_before_building():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "update-site.yml").read_text()
    assert 'cron: "20 7,19 * * *"' in workflow
    assert "scripts/update_speakers.py" in workflow
    assert "scripts/update_language.py" in workflow
    assert "scripts/update_bills.py" in workflow
    assert workflow.index("scripts/update_language.py") < workflow.index(
        "scripts/build_site.py"
    )
    assert "git pull --rebase origin master" in workflow
