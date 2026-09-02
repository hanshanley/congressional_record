"""Tests for the static site build, focused on what would break it in CI.

The site is published by a scheduled job, so the failure modes that matter are
the silent ones: CI producing a different ranking than a local build, or the
workflow referencing dependencies and files that do not exist.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.bills import canonical_bill, save_bills  # noqa: E402
from analysis.ingest.govinfo_bulk import run_bulk  # noqa: E402
from analysis.speakers import save_daily, speaker_counts  # noqa: E402


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
    daily["profanity_terms"] = [
        '{"damn":60}', '{"damn":6}', '{"shit":8}',
    ]
    path = tmp_path / "speaker_daily"
    save_daily(daily, path)
    bills = pd.DataFrame([
        canonical_bill(
            source="test",
            source_url="https://example.test/old",
            source_updated_at="",
            congress=118,
            bill_type="HR",
            bill_number=1,
            title="Old bill",
            origin_chamber="House",
            introduced_date="2023-01-01",
            sponsors=[{"bioguideId": "OLD", "fullName": "Mr. OLD", "party": "D", "state": "CA"}],
            actions=[{"actionCode": "8000", "actionDate": "2023-02-01"}],
            laws=[],
        ),
        canonical_bill(
            source="test",
            source_url="https://example.test/new",
            source_updated_at="",
            congress=119,
            bill_type="HR",
            bill_number=1,
            title="New bill",
            origin_chamber="House",
            introduced_date="2025-01-01",
            sponsors=[{"bioguideId": "NEW", "fullName": "Mr. NEW", "party": "R", "state": "TX"}],
            actions=[
                {"actionCode": "8000", "actionDate": "2025-02-01"},
                {"actionCode": "36000", "actionDate": "2025-03-01"},
            ],
            laws=[{"type": "Public Law", "number": "119-1"}],
        ),
    ])
    bill_path = tmp_path / "bills"
    save_bills(bills, bill_path)
    return path, daily, bill_path


# ---------------------------------------------------------------- congress selection


def test_default_congress_is_the_latest_present(store):
    _, daily, _ = store
    module = _load_build_site()
    assert module.resolve_congress("latest", daily) == 119


def test_congress_all_spans_every_congress(store):
    _, daily, _ = store
    module = _load_build_site()
    assert module.resolve_congress("all", daily) is None


def test_explicit_congress_is_honoured(store):
    _, daily, _ = store
    module = _load_build_site()
    assert module.resolve_congress("118", daily) == 118
    assert module.resolve_congress(119, daily) == 119


def test_invalid_congress_fails_loudly(store):
    _, daily, _ = store
    module = _load_build_site()
    with pytest.raises(SystemExit):
        module.resolve_congress("last-year", daily)


def test_ci_default_ranks_the_sitting_congress_only(store, tmp_path):
    # A default of "all" would make the scheduled build publish an all-time board,
    # silently differing from what a local build of the same data produces.
    path, _, bills = store
    module = _load_build_site()
    out = tmp_path / "site"
    assert module.main([
        "--daily", str(path), "--bills", str(bills), "--out", str(out),
        "--min-words", "1000",
    ]) == 0
    meta = json.loads((out / "data" / "meta.json").read_text())
    assert meta["congress"] == 119
    board = json.loads((out / "data" / "leaderboard.json").read_text())
    assert {row["speaker_name"] for row in board} == {"Mr. NEW", "Ms. NEW2"}


# ------------------------------------------------------------------------- output


def test_build_writes_html_json_and_figures(store, tmp_path):
    path, _, bills = store
    module = _load_build_site()
    out = tmp_path / "site"
    assert module.main([
        "--daily", str(path), "--bills", str(bills), "--out", str(out),
        "--min-words", "1000",
    ]) == 0
    for rel in ("index.html", "activity.html", "activity/index.html",
                "data/leaderboard.json", "data/timeseries.json",
                "data/meta.json", "data/congresses.json", "data/congress_119.json",
                "data/long_run_language.json",
                "figures/leaderboard.png", "figures/trend.png",
                "figures/language_trends.png", "figures/language_members.png"):
        assert (out / rel).exists(), rel


def test_legacy_leaderboard_dates_use_selected_congress_scope(store, tmp_path):
    path, daily, bills = store
    daily = pd.concat([
        daily,
        _daily([
            ["NEW", "2023-04-01", "house", "Mr. NEW", "R", "TX", 118,
             1, 1_000, 0, 0, 0, 0],
            ["NEW", "2025-04-01", "house", "Mr. NEW", "R", "TX", 119,
             1, 1_000, 0, 0, 0, 0],
        ]),
    ], ignore_index=True)
    save_daily(daily, path)
    module = _load_build_site()
    out = tmp_path / "site"

    assert module.main([
        "--daily", str(path), "--bills", str(bills), "--out", str(out),
        "--congress", "119", "--min-words", "1000",
    ]) == 0

    legacy = json.loads((out / "data" / "leaderboard.json").read_text())
    new_row = next(row for row in legacy if row["bioguide"] == "NEW")
    assert new_row["first_date"] == "2025-02-01"
    assert new_row["last_date"] == "2025-04-01"

    payload = json.loads((out / "data" / "congress_119.json").read_text())
    payload_row = next(
        row for row in payload["leaderboards"]["profanity"]
        if row["bioguide"] == "NEW"
    )
    assert "first_date" not in payload_row
    assert "last_date" not in payload_row


def test_legacy_leaderboard_dates_span_all_congresses(store, tmp_path):
    path, daily, bills = store
    daily = pd.concat([
        daily,
        _daily([
            ["NEW", "2023-04-01", "house", "Mr. NEW", "R", "TX", 118,
             1, 1_000, 0, 0, 0, 0],
            ["NEW", "2025-04-01", "house", "Mr. NEW", "R", "TX", 119,
             1, 1_000, 0, 0, 0, 0],
        ]),
    ], ignore_index=True)
    save_daily(daily, path)
    module = _load_build_site()
    out = tmp_path / "site"

    assert module.main([
        "--daily", str(path), "--bills", str(bills), "--out", str(out),
        "--congress", "all", "--min-words", "1000",
    ]) == 0

    legacy = json.loads((out / "data" / "leaderboard.json").read_text())
    new_row = next(row for row in legacy if row["bioguide"] == "NEW")
    assert new_row["first_date"] == "2023-04-01"
    assert new_row["last_date"] == "2025-04-01"


def test_html_escapes_member_names(tmp_path):
    module = _load_build_site()
    daily = _daily([
        ["X", "2025-02-01", "house", "<script>alert(1)</script>",
         "<a href='javascript:alert(2)'>D</a>", "CA", 119,
         20, 60_000, 6, 0, 0, 0],
    ])
    daily["profanity_terms"] = ['{"damn":6}']
    path = tmp_path / "speaker_daily"
    save_daily(daily, path)
    bills = pd.DataFrame([
        canonical_bill(
            source="test", source_url="https://example.test/x", source_updated_at="",
            congress=119, bill_type="HR", bill_number=1, title="<b>bill</b>",
            origin_chamber="House", introduced_date="2025-01-01",
            sponsors=[{
                "bioguideId": "X", "fullName": "<script>alert(1)</script>",
                "party": "D", "state": "CA",
            }],
            actions=[], laws=[],
        )
    ])
    bill_path = tmp_path / "bills"
    save_bills(bills, bill_path)
    out = tmp_path / "site"
    assert module.main([
        "--daily", str(path), "--bills", str(bill_path), "--out", str(out),
        "--min-words", "1000",
    ]) == 0
    html = (out / "index.html").read_text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "href='javascript:alert(2)'" not in html
    assert "&lt;a href=&#x27;javascript:alert(2)&#x27;&gt;D&lt;/a&gt;" in html


def test_missing_table_is_reported_not_crashed(tmp_path):
    module = _load_build_site()
    assert module.main(["--daily", str(tmp_path / "nope"), "--out", str(tmp_path / "s")]) == 1


def test_quoted_hits_are_shown_so_the_exclusion_is_auditable(store, tmp_path):
    path, _, bills = store
    module = _load_build_site()
    out = tmp_path / "site"
    module.main([
        "--daily", str(path), "--bills", str(bills), "--out", str(out),
        "--congress", "118", "--min-words", "1000",
    ])
    board = json.loads((out / "data" / "leaderboard.json").read_text())
    assert board[0]["profanity_quoted_hits"] == 1
    assert "Quoted" in (out / "activity" / "index.html").read_text()


def test_payload_contains_all_five_transparent_leaderboards(store, tmp_path):
    path, _, bills = store
    module = _load_build_site()
    out = tmp_path / "site"
    assert module.main([
        "--daily", str(path), "--bills", str(bills), "--out", str(out),
        "--min-words", "1000",
    ]) == 0
    payload = json.loads((out / "data" / "congress_119.json").read_text())
    assert set(payload["leaderboards"]) == {
        "speech", "sponsored", "passed", "enacted", "profanity",
    }
    assert payload["leaderboards"]["enacted"][0]["bioguide"] == "NEW"
    assert payload["leaderboards"]["enacted"][0]["examples"][0]["url"] == (
        "https://www.congress.gov/bill/119th-congress/house-bill/1"
    )


def test_profanity_tables_show_each_members_most_used_term(store, tmp_path):
    path, daily, bills = store
    daily["profanity_terms"] = ["{}", '{"damn":4,"crap":2}', '{"shit":8}']
    save_daily(daily, path)
    module = _load_build_site()
    out = tmp_path / "site"
    assert module.main([
        "--daily", str(path), "--bills", str(bills), "--out", str(out),
        "--min-words", "1000",
    ]) == 0

    payload = json.loads((out / "data" / "congress_119.json").read_text())
    terms = {
        row["bioguide"]: row["favorite_profanity_term"]
        for row in payload["leaderboards"]["profanity"]
    }
    assert terms == {"NEW": "damn", "NEW2": "shit"}
    assert "Most-used term" in (out / "index.html").read_text()
    assert "Most-used term" in (out / "activity" / "index.html").read_text()
    assert "not a claim of preference" in (out / "index.html").read_text()
    term_leaders = {
        row["term"]: row for row in payload["language"]["profanity_term_leaders"]
    }
    assert term_leaders["damn"]["leaders"][0]["speaker_name"] == "Mr. NEW"
    assert term_leaders["shit"]["leaders"][0]["speaker_name"] == "Ms. NEW2"
    assert set(payload["language"]["profanity_term_leaders_by_chamber"]) == {
        "house", "senate",
    }
    assert payload["language"]["profanity_term_detail_available"] is True
    assert payload["language"]["profanity_term_detail_available_by_chamber"] == {
        "house": True, "senate": True,
    }
    breakdown = payload["language"]["profanity_term_member_counts"]
    assert {
        (row["bioguide"], row["party"], row["state"], row["chamber"], row["term"])
        for row in breakdown
    } == {
        ("NEW", "R", "TX", "house", "damn"),
        ("NEW", "R", "TX", "house", "crap"),
        ("NEW2", "D", "NY", "senate", "shit"),
    }
    page = (out / "index.html").read_text()
    assert "Who uses each term the most?" in page
    assert 'id="term-leaders-table"' in page
    assert "renderTermExplorer(language)" in page
    assert "recentTermDetailAvailable" in page
    assert 'id="term-view"' in page
    assert 'id="term-party"' in page
    assert 'id="term-chamber"' in page
    assert 'id="state-term-map"' in page
    assert "Most-used term by state" in page


def test_builds_combined_last_five_congresses_payload(store, tmp_path):
    path, daily, bills = store
    earlier = _daily([
        ["C115", "2017-01-03", "house", "Member 115", "D", "CA", 115,
         1, 30_000, 1, 0, 0, 0],
        ["C116", "2019-01-03", "house", "Member 116", "R", "TX", 116,
         1, 30_000, 1, 0, 0, 0],
        ["C117", "2021-01-03", "senate", "Member 117", "D", "NY", 117,
         1, 30_000, 1, 0, 0, 0],
    ])
    earlier["profanity_terms"] = ['{"damned":1}', '{"damn":1}', '{"damn":1}']
    save_daily(pd.concat([earlier, daily], ignore_index=True), path)
    module = _load_build_site()
    out = tmp_path / "site"

    assert module.main([
        "--daily", str(path), "--bills", str(bills), "--out", str(out),
        "--min-words", "1000",
    ]) == 0

    payload = json.loads((out / "data" / "congress_recent5.json").read_text())
    assert payload["label"] == "Last 5 Congresses (115–119)"
    assert payload["language"]["scope_label"] == "Last 5 Congresses (115–119)"
    assert "Last 5 Congresses" in (out / "index.html").read_text()
    assert "damn” and “damned" in (out / "index.html").read_text()


def test_build_refuses_incomplete_current_congress_term_counts(store, tmp_path):
    path, daily, bills = store
    daily.loc[daily["bioguide"] == "NEW", "profanity_terms"] = "{}"
    save_daily(daily, path)
    module = _load_build_site()
    assert module.main([
        "--daily", str(path), "--bills", str(bills), "--out", str(tmp_path / "site"),
        "--min-words", "1000",
    ]) == 1


def test_payload_and_html_expose_selector_aware_language_graphs(store, tmp_path):
    path, _, bills = store
    module = _load_build_site()
    out = tmp_path / "site"
    assert module.main([
        "--daily", str(path), "--bills", str(bills), "--out", str(out),
        "--min-words", "1000",
    ]) == 0
    payload = json.loads((out / "data" / "congress_119.json").read_text())
    language = payload["language"]
    assert set(language["metrics"]) == {"profanity", "hostility", "misconduct"}
    assert language["granularity"] == "month"
    assert {row["party"] for row in language["series"]} <= {"D", "R"}
    assert set(language["members"]) == {"profanity", "hostility", "misconduct"}
    assert language["series"]
    assert len(language["highlights"][0]["top_members"]) <= 3
    assert {row["chamber"] for row in language["chamber_series"]} == {"house", "senate"}
    assert set(language["members_by_chamber"]) == {"house", "senate"}
    assert "per 100,000 attributed spoken words" in language["explanation"]["shown"]
    assert "does not prove misconduct" in language["explanation"]["limitation"]

    all_payload = json.loads((out / "data" / "congress_all.json").read_text())
    assert all_payload["language"]["granularity"] == "year"

    page = (out / "index.html").read_text()
    for text in (
        "The Language of Congress", "The long-run picture",
        "Recent language on the floor", "What is shown", "What is examined",
        "Methodology and limitations", 'id="language-highlight"',
        'id="recent-visual"', 'id="recent-metric"', 'id="recent-view"',
        'id="recent-chamber"', 'id="long-run-chamber"',
        "renderLanguage(payload.language)", "renderTrendPanel",
        "renderMemberPanel", "renderRecentFocus", "syncSelect",
        "selectedRecentView", "bindTooltip", "chart-toggle",
        "renderLongRun(longRunLanguage)", "renderLongRunPanel",
        "selectedLongRunMetric = 'profanity_per_1k'",
        "URLSearchParams", "loadSequence", "addAccessibleTable",
        'className = \'sr-only\'', 'role="alert"', "activity/",
        'id="long-run-chart"', 'id="long-run-metric"', 'data-language-metric="profanity"',
    ):
        assert text in page
    assert "min-width:42rem" not in page
    assert ".mini-chart { position:relative; border-top:1px solid var(--grid);" in page
    assert "Members below the word threshold are omitted." not in page
    long_run = json.loads((out / "data" / "long_run_language.json").read_text())
    assert len(long_run["metrics"]) == 6
    assert {row["party"] for row in long_run["series"]} == {"D", "R"}
    assert {row["chamber"] for row in long_run["chamber_series"]} == {"house", "senate"}
    activity_page = (out / "activity" / "index.html").read_text()
    assert "Congressional member activity and bills" in activity_page
    assert (
        '<link rel="canonical" '
        'href="https://www.themarginoferror.com/professional_profanity/">'
    ) in page
    assert (
        '<link rel="canonical" '
        'href="https://www.themarginoferror.com/professional_profanity/activity/">'
    ) in activity_page
    assert "Who sponsors the most bills" in activity_page
    assert "The Language of Congress" in activity_page
    assert 'id="activity-metric"' in activity_page
    assert "selectActivityMetric" in activity_page
    assert "activityMetrics" in activity_page
    assert 'id="leaderboards"' in activity_page
    activity_redirect = (out / "activity.html").read_text()
    assert "url=activity/" in activity_redirect
    assert 'name="description"' in activity_redirect
    assert "<h1>Congressional member activity and bills</h1>" in activity_redirect
    assert (
        'href="https://www.themarginoferror.com/'
        'professional_profanity/activity/"'
    ) in activity_redirect


def test_nonselected_extension_only_congress_does_not_break_charts(store, tmp_path):
    path, daily, bills = store
    extension_only = _daily([
        ["EXT", "2021-02-01", "extensions", "Ms. EXTENSION", "D", "CA", 117,
         5, 30_000, 1, 0, 2, 3],
    ])
    save_daily(pd.concat([daily, extension_only], ignore_index=True), path)
    module = _load_build_site()
    out = tmp_path / "site"
    assert module.main([
        "--daily", str(path), "--bills", str(bills), "--out", str(out),
        "--min-words", "1000",
    ]) == 0
    payload = json.loads((out / "data" / "congress_117.json").read_text())
    assert payload["language"]["series"] == []
    assert (out / "figures" / "language_trends.png").exists()


def test_unseeded_congress_is_labeled_unavailable_not_zero(store, tmp_path):
    path, daily, bills = store
    older = _daily([
        ["LEGACY", "1994-01-25", "house", "Mr. LEGACY", "D", "CA", 103,
         10, 30_000, 1, 0, 0, 0],
    ])
    save_daily(pd.concat([daily, older], ignore_index=True), path)
    module = _load_build_site()
    out = tmp_path / "site"
    assert module.main([
        "--daily", str(path), "--bills", str(bills), "--out", str(out),
        "--min-words", "1000",
    ]) == 0
    payload = json.loads((out / "data" / "congress_103.json").read_text())
    assert "has not been seeded" in payload["coverage"]["warning"]
    assert "partial" in payload["coverage"]["warning"]


def test_default_full_site_build_is_reproducible(store, tmp_path):
    path, _, bills = store
    module = _load_build_site()
    outputs = [tmp_path / "site-a", tmp_path / "site-b"]
    for out in outputs:
        assert module.main([
            "--daily", str(path), "--bills", str(bills), "--out", str(out),
            "--min-words", "1000",
        ]) == 0
    first = {
        path.relative_to(outputs[0]): path.read_bytes()
        for path in outputs[0].rglob("*")
        if path.is_file()
    }
    second = {
        path.relative_to(outputs[1]): path.read_bytes()
        for path in outputs[1].rglob("*")
        if path.is_file()
    }
    assert first == second
    meta = json.loads((outputs[0] / "data" / "meta.json").read_text())
    assert meta["generated_utc"] == "2025-03-01T00:00:00+00:00"


def test_source_date_epoch_overrides_input_snapshot(monkeypatch):
    module = _load_build_site()
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1786147200")
    daily = pd.DataFrame({"date": ["2025-03-01"]})
    bills = pd.DataFrame({"source_updated_at": ["2025-03-02T12:00:00"]})
    expected = pd.Timestamp(1786147200, unit="s", tz="UTC").isoformat()
    assert module.resolve_generated_utc(None, daily, bills) == expected


def test_default_snapshot_uses_newest_mixed_format_input():
    module = _load_build_site()
    daily = pd.DataFrame({"date": ["2026-08-06"]})
    bills = pd.DataFrame({
        "source_updated_at": ["2026-08-05", "2026-08-08T18:50:00"],
    })
    assert module.resolve_generated_utc(
        None, daily, bills
    ) == "2026-08-08T18:50:00+00:00"


def test_govinfo_to_site_pipeline_is_reproducible(tmp_path):
    pkg = "CREC-2025-07-23"
    parent = f"{pkg}-pt1-PgH3572"
    child = f"{parent}-2"
    long_speech = " ".join(["substantive remarks"] * 120)
    inserted = " ".join(["legislative text"] * 1_000)

    def build_package(bulk: Path) -> None:
        bulk.mkdir(parents=True)
        related = "".join(
            f"""
            <relatedItem type="constituent" ID="id-{gid}"><extension>
              <granuleClass>HOUSE</granuleClass>
              <congMember bioGuideId="R000575" party="R" state="AL">
                <name type="authority-fnf">Mike Rogers</name>
              </congMember>
            </extension></relatedItem>
            """
            for gid in (parent, child)
        )
        mods = (
            '<mods xmlns="http://www.loc.gov/mods/v3">'
            "<extension><congress>119</congress></extension>"
            f"{related}</mods>"
        )
        texts = {
            parent: (
                f"Mr. ROGERS of Alabama. {long_speech} [[Page H3573]]\n"
                "The SPEAKER pro tempore (Mr. Simpson). "
                f"The text of the bill is as follows: {inserted}"
            ),
            child: (
                f"Mr. ROGERS of Alabama. {long_speech}\n"
                "The SPEAKER pro tempore (Mr. Simpson). "
                f"The text of the bill is as follows: {inserted}"
            ),
        }
        with zipfile.ZipFile(bulk / f"{pkg}.zip", "w") as archive:
            archive.writestr(f"{pkg}/mods.xml", mods)
            for gid, text in texts.items():
                archive.writestr(
                    f"{pkg}/{gid}.htm",
                    f"<html><body><pre>{text}</pre></body></html>",
                )

    daily_paths = []
    for name in ("first", "second"):
        bulk = tmp_path / name / "bulk"
        out = tmp_path / name / "out"
        build_package(bulk)
        assert run_bulk([pkg], bulk, out, workers=1) == 2
        turns = out / "turns" / "govinfo_bulk_119.parquet"
        daily = speaker_counts([turns])
        assert len(daily) == 1
        assert daily.iloc[0]["turns"] == 1
        assert daily.iloc[0]["words"] < 500
        daily_path = tmp_path / name / "daily"
        save_daily(daily, daily_path)
        daily_paths.append(daily_path)

    bills = pd.DataFrame([
        canonical_bill(
            source="test",
            source_url="https://example.test/bill",
            source_updated_at="2025-07-23T12:00:00",
            congress=119,
            bill_type="HR",
            bill_number=1,
            title="Fixture bill",
            origin_chamber="House",
            introduced_date="2025-07-23",
            sponsors=[{
                "bioguideId": "R000575",
                "fullName": "Mr. ROGERS of Alabama",
                "party": "R",
                "state": "AL",
            }],
            actions=[],
            laws=[],
        )
    ])
    bill_path = tmp_path / "bills"
    save_bills(bills, bill_path)

    module = _load_build_site()
    sites = [tmp_path / "site-first", tmp_path / "site-second"]
    for daily_path, site in zip(daily_paths, sites):
        assert module.main([
            "--daily", str(daily_path),
            "--bills", str(bill_path),
            "--out", str(site),
            "--min-words", "1",
        ]) == 0
    first = {
        path.relative_to(sites[0]): path.read_bytes()
        for path in sites[0].rglob("*")
        if path.is_file()
    }
    second = {
        path.relative_to(sites[1]): path.read_bytes()
        for path in sites[1].rglob("*")
        if path.is_file()
    }
    assert first == second


# ----------------------------------------------------------------------- workflow


def test_workflow_installs_every_requirements_file_it_needs():
    # The scheduled job runs pytest to gate publishing; if pytest is not installed
    # the workflow fails on every run and the site silently stops updating.
    workflow = (ROOT / ".github" / "workflows" / "update-site.yml").read_text()
    assert "requirements-dev.txt" in workflow
    assert "pytest" in (ROOT / "requirements-dev.txt").read_text()


def test_workflow_references_scripts_that_exist():
    workflow = (ROOT / ".github" / "workflows" / "update-site.yml").read_text()
    for script in (
        "scripts/update_speakers.py", "scripts/update_bills.py", "scripts/build_site.py",
    ):
        assert script in workflow
        assert (ROOT / script).exists()


def test_workflow_needs_no_api_key_secret():
    # The scheduled job discovers issues by probing public bulk URLs, so it must not
    # depend on a repository secret. The user cannot publish one for a public repo,
    # and the DEMO_KEY fallback (~50 requests/day) would eventually break the job.
    from analysis.ingest import govinfo_bulk

    updater = (ROOT / "scripts" / "update_speakers.py").read_text()
    assert "probe_packages" in updater
    assert "iter_packages" not in updater, "discovery must not use the API-key path"
    assert hasattr(govinfo_bulk, "probe_packages")

    workflow = (ROOT / ".github" / "workflows" / "update-site.yml").read_text()
    assert "GOVINFO_API_KEY" not in workflow
    assert "CONGRESS_API_KEY" not in workflow


def test_probe_rejects_an_absurdly_wide_window():
    # Probing costs one request per day, so a multi-year window must fail loudly
    # rather than silently issuing thousands of requests.
    from analysis.ingest.govinfo_bulk import probe_packages

    with pytest.raises(ValueError):
        probe_packages("1994-01-01", "2026-01-01")


def test_probe_handles_an_inverted_range():
    from analysis.ingest.govinfo_bulk import probe_packages

    assert probe_packages("2026-07-28", "2026-07-20") == []


def test_probe_fails_loudly_when_govinfo_status_is_unknown():
    from analysis.ingest.govinfo_bulk import probe_packages

    result = subprocess.CompletedProcess([], 0, stdout="503", stderr="")
    with patch("analysis.ingest.govinfo_bulk.subprocess.run", return_value=result):
        with pytest.raises(RuntimeError, match="could not determine"):
            probe_packages("2026-07-20", "2026-07-20", workers=1)


def test_probe_distinguishes_published_and_missing_issues():
    from analysis.ingest.govinfo_bulk import probe_packages

    responses = iter([
        subprocess.CompletedProcess([], 0, stdout="200", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="302", stderr=""),
    ])
    with patch(
        "analysis.ingest.govinfo_bulk.subprocess.run",
        side_effect=lambda *args, **kwargs: next(responses),
    ) as run:
        assert probe_packages(
            "2026-07-20", "2026-07-21", workers=1
        ) == ["CREC-2026-07-20"]
    assert all("--retry-all-errors" in call.args[0] for call in run.call_args_list)


def test_committed_site_state_is_not_gitignored():
    # CI can only update incrementally if the state file is actually committed.
    ignore = (ROOT / ".gitignore").read_text()
    assert "!data/site/**/*.parquet" in ignore
