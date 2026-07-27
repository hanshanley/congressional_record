"""Tests for the static site build, focused on what would break it in CI.

The site is published by a scheduled job, so the failure modes that matter are
the silent ones: CI producing a different ranking than a local build, or the
workflow referencing dependencies and files that do not exist.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.speakers import save_daily  # noqa: E402


def _load_build_site():
    spec = importlib.util.spec_from_file_location(
        "build_site_under_test", ROOT / "scripts" / "build_site.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _daily(rows):
    return pd.DataFrame(rows, columns=[
        "bioguide", "date", "chamber", "speaker_name", "party", "state", "congress",
        "turns", "words", "profanity_hits", "profanity_quoted_hits",
        "hostility_hits", "misconduct_hits",
    ])


@pytest.fixture()
def store(tmp_path: Path):
    daily = _daily([
        ["OLD", "2023-02-01", "house", "Mr. OLD", "D", "CA", 118, 20, 60_000, 60, 1, 0, 0],
        ["NEW", "2025-02-01", "house", "Mr. NEW", "R", "TX", 119, 20, 60_000, 6, 0, 0, 0],
        ["NEW2", "2025-03-01", "senate", "Ms. NEW2", "D", "NY", 119, 20, 40_000, 8, 0, 0, 0],
    ])
    path = tmp_path / "speaker_daily"
    save_daily(daily, path)
    return path, daily


# ---------------------------------------------------------------- congress selection


def test_default_congress_is_the_latest_present(store):
    _, daily = store
    module = _load_build_site()
    assert module.resolve_congress("latest", daily) == 119


def test_congress_all_spans_every_congress(store):
    _, daily = store
    module = _load_build_site()
    assert module.resolve_congress("all", daily) is None


def test_explicit_congress_is_honoured(store):
    _, daily = store
    module = _load_build_site()
    assert module.resolve_congress("118", daily) == 118
    assert module.resolve_congress(119, daily) == 119


def test_invalid_congress_fails_loudly(store):
    _, daily = store
    module = _load_build_site()
    with pytest.raises(SystemExit):
        module.resolve_congress("last-year", daily)


def test_ci_default_ranks_the_sitting_congress_only(store, tmp_path):
    # A default of "all" would make the scheduled build publish an all-time board,
    # silently differing from what a local build of the same data produces.
    path, _ = store
    module = _load_build_site()
    out = tmp_path / "site"
    assert module.main(["--daily", str(path), "--out", str(out), "--min-words", "1000"]) == 0
    meta = json.loads((out / "data" / "meta.json").read_text())
    assert meta["congress"] == 119
    board = json.loads((out / "data" / "leaderboard.json").read_text())
    assert {row["speaker_name"] for row in board} == {"Mr. NEW", "Ms. NEW2"}


# ------------------------------------------------------------------------- output


def test_build_writes_html_json_and_figures(store, tmp_path):
    path, _ = store
    module = _load_build_site()
    out = tmp_path / "site"
    assert module.main(["--daily", str(path), "--out", str(out), "--min-words", "1000"]) == 0
    for rel in ("index.html", "data/leaderboard.json", "data/timeseries.json",
                "data/meta.json", "figures/leaderboard.png", "figures/trend.png"):
        assert (out / rel).exists(), rel


def test_html_escapes_member_names(tmp_path):
    module = _load_build_site()
    daily = _daily([
        ["X", "2025-02-01", "house", "<script>alert(1)</script>", "D", "CA", 119,
         20, 60_000, 6, 0, 0, 0],
    ])
    path = tmp_path / "speaker_daily"
    save_daily(daily, path)
    out = tmp_path / "site"
    assert module.main(["--daily", str(path), "--out", str(out), "--min-words", "1000"]) == 0
    html = (out / "index.html").read_text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_missing_table_is_reported_not_crashed(tmp_path):
    module = _load_build_site()
    assert module.main(["--daily", str(tmp_path / "nope"), "--out", str(tmp_path / "s")]) == 1


def test_quoted_hits_are_shown_so_the_exclusion_is_auditable(store, tmp_path):
    path, _ = store
    module = _load_build_site()
    out = tmp_path / "site"
    module.main(["--daily", str(path), "--out", str(out), "--congress", "118",
                 "--min-words", "1000"])
    board = json.loads((out / "data" / "leaderboard.json").read_text())
    assert board[0]["profanity_quoted_hits"] == 1
    assert "Quoted" in (out / "index.html").read_text()


# ----------------------------------------------------------------------- workflow


def test_workflow_installs_every_requirements_file_it_needs():
    # The scheduled job runs pytest to gate publishing; if pytest is not installed
    # the workflow fails on every run and the site silently stops updating.
    workflow = (ROOT / ".github" / "workflows" / "update-site.yml").read_text()
    assert "requirements-dev.txt" in workflow
    assert "pytest" in (ROOT / "requirements-dev.txt").read_text()


def test_workflow_references_scripts_that_exist():
    workflow = (ROOT / ".github" / "workflows" / "update-site.yml").read_text()
    for script in ("scripts/update_speakers.py", "scripts/build_site.py"):
        assert script in workflow
        assert (ROOT / script).exists()


def test_committed_site_state_is_not_gitignored():
    # CI can only update incrementally if the state file is actually committed.
    ignore = (ROOT / ".gitignore").read_text()
    assert "!data/site/**/*.parquet" in ignore
