from __future__ import annotations

import datetime as dt
from argparse import Namespace
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analysis.daily_language import (
    restrict_to_complete_metrics,
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
            "profanity_hits",
            "slur_hits",
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
        ["2025-01-04", 119, "house", "D", 1, 0, 0, 0],
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
        ["2026-09-01", 119, "house", "D", 100, 1, 0, 0],
        ["2026-09-02", 119, "house", "D", 100, 1, 0, 0],
    ])
    fresh = _daily([
        ["2026-09-02", 119, "house", "R", 200, 2, 0, 0],
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
                "profanity_hits": 0,
                "formal_courtesy_per_1k": 100.0,
                "profanity_per_1k": 0.0,
            },
            {
                "year": 2025,
                "party": "D",
                "words": 999,
                "formal_courtesy_hits": 999,
                "profanity_hits": 999,
                "formal_courtesy_per_1k": 1000.0,
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
                "profanity_hits": 0,
                "formal_courtesy_per_1k": 100.0,
                "profanity_per_1k": 0.0,
            },
            {
                "year": 2025,
                "party": "D",
                "chamber": "house",
                "words": 999,
                "formal_courtesy_hits": 999,
                "profanity_hits": 999,
                "formal_courtesy_per_1k": 1000.0,
                "profanity_per_1k": 1000.0,
            },
        ],
        "first_year": 2023,
        "last_year": 2025,
        "source_note": "test",
    }
    daily = _daily([
        ["2026-09-02", 119, "house", "D", 200, 2, 1, 0],
        ["2026-09-02", 119, "senate", "D", 300, 3, 2, 1],
    ])

    result = merge_long_run_payload(base, daily)

    historical = next(row for row in result["series"] if row["year"] == 2023)
    current = next(row for row in result["series"] if row["year"] == 2025)
    assert historical == base["series"][0]
    assert current["words"] == 500
    assert current["formal_courtesy_hits"] == 5
    assert current["formal_courtesy_per_1k"] == 10.0
    assert len([row for row in result["chamber_series"] if row["year"] == 2025]) == 2


def test_long_run_payload_omits_measures_missing_from_any_row():
    payload = {
        "metrics": {"retired_per_1k": {}},
        "series": [
            {"profanity_hits": 1, "profanity_per_1k": 0.1,
             "slur_hits": None, "slurs_per_1k": None},
            {"profanity_hits": 2, "profanity_per_1k": 0.2,
             "slur_hits": 0, "slurs_per_1k": 0.0},
        ],
        "chamber_series": [],
    }
    restricted = restrict_to_complete_metrics(payload)
    assert list(restricted["metrics"]) == ["profanity_per_1k"]
    assert set(restricted["series"][1]) == {"profanity_hits", "profanity_per_1k"}
    payload["series"][0].update(slur_hits=0, slurs_per_1k=0.0)
    assert set(restrict_to_complete_metrics(payload)["metrics"]) == {
        "profanity_per_1k", "slurs_per_1k",
    }


def test_daily_workflow_updates_every_data_surface_before_building():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "update-site.yml").read_text()
    assert 'cron: "20 7,19 * * *"' in workflow
    assert "scripts/update_speakers.py" in workflow
    assert "scripts/update_language.py" in workflow
    assert "scripts/update_bills.py" in workflow
    assert "scripts/build_term_graphs.py" in workflow
    assert "git add site outputs/figures/profanity_terms" in workflow
    assert workflow.index("scripts/update_language.py") < workflow.index(
        "scripts/build_site.py"
    )
    assert "git pull --rebase origin master" in workflow


def test_daily_workflow_does_not_inject_author_trailers():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "update-site.yml").read_text()
    assert "Co-authored-by:" not in workflow
    assert "Copilot-Session:" not in workflow


@pytest.mark.parametrize("concurrent_update", [True, False])
def test_backfill_publication_rebuilds_after_branch_advance_and_stops_other_push_failures(
    tmp_path, concurrent_update,
):
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/backfill-all-speaker-terms.yml").read_text()
    publish = textwrap.dedent(
        workflow.split("      - name: Rebuild and commit site\n        run: |\n", 1)[1]
    )
    remote = tmp_path / "remote.git"
    publisher = tmp_path / "publisher"
    editor = tmp_path / "editor"

    def git(*args, cwd=tmp_path):
        return subprocess.run(
            ["git", *map(str, args)], cwd=cwd, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    git("init", "--bare", "--initial-branch=master", remote)
    git("clone", remote, publisher)
    git("config", "user.name", "Test", cwd=publisher)
    git("config", "user.email", "test@example.com", cwd=publisher)
    git("config", "commit.gpgsign", "false", cwd=publisher)
    (publisher / "template.txt").write_text("original style")
    scripts = publisher / "scripts"
    scripts.mkdir()
    (scripts / "build_site.py").write_text(
        "from pathlib import Path\n"
        "Path('site').mkdir(exist_ok=True)\n"
        "Path('site/index.html').write_text(Path('template.txt').read_text())\n"
    )
    (scripts / "build_term_graphs.py").write_text(
        "from pathlib import Path\n"
        "out = Path('outputs/figures/profanity_terms')\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'index.json').write_text('{}')\n"
    )
    git("add", ".", cwd=publisher)
    git("commit", "-m", "Initial", cwd=publisher)
    git("push", "origin", "master", cwd=publisher)
    git("clone", remote, editor)
    git("config", "user.name", "Editor", cwd=editor)
    git("config", "user.email", "editor@example.com", cwd=editor)
    git("config", "commit.gpgsign", "false", cwd=editor)
    scores = publisher / "data/site/speaker_daily"
    scores.mkdir(parents=True)
    (scores / "congress_119.parquet").write_text("rescored data")

    hook = publisher / ".git/hooks/pre-push"
    if concurrent_update:
        hook.write_text(
            "#!/bin/sh\nset -e\n"
            "if [ ! -f .git/simulated-update ]; then\n"
            "  touch .git/simulated-update\n"
            f"  cd '{editor}'\n"
            "  printf 'new style' > template.txt\n"
            "  git add template.txt\n"
            "  git commit -m 'New presentation'\n"
            "  git push origin master\n"
            "fi\n"
        )
    else:
        hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python").symlink_to(sys.executable)

    result = subprocess.run(
        ["bash", "-e", "-c", publish], cwd=publisher,
        env={**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]},
        capture_output=True, text=True,
    )
    if concurrent_update:
        assert result.returncode == 0, result.stdout + result.stderr
        git("pull", "--ff-only", cwd=editor)
        assert (editor / "site/index.html").read_text() == "new style"
        assert (editor / "data/site/speaker_daily/congress_119.parquet").read_text() == "rescored data"
        messages = git("log", "--format=%B", cwd=editor)
        assert "New presentation" in messages
        assert "Co-authored-by:" not in messages
        assert "Copilot-Session:" not in messages
    else:
        assert result.returncode != 0
        assert "Push failed without a concurrent branch update" in result.stdout
        assert "master advanced during publication" not in result.stdout


def test_backfill_recovery_rejects_missing_requested_artifact_even_with_committed_data(
    tmp_path, monkeypatch,
):
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/backfill-all-speaker-terms.yml").read_text()
    validation = textwrap.dedent(
        workflow.split("          python - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    )
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    committed = tmp_path / "data/site/speaker_daily"
    committed.mkdir(parents=True)
    existing = committed / "congress_119.parquet"
    existing.write_text("old committed data")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BACKFILL_CONGRESSES", "[119]")
    monkeypatch.setenv("RESCORED_SPEAKERS", str(artifacts))
    with pytest.raises(FileNotFoundError, match="congress_119"):
        exec(compile(validation, "<workflow-validation>", "exec"), {})
    assert existing.read_text() == "old committed data"
