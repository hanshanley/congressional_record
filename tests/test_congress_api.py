"""Offline tests for the Congress.gov legacy bill seed adapter."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import traceback
from pathlib import Path
from urllib.parse import urlsplit

import pandas as pd
import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.bills import BILL_COLUMNS, load_bills  # noqa: E402
from analysis.ingest.congress_api import (  # noqa: E402
    CongressAPIClient,
    canonicalize_bill,
    load_completed,
    seed_legacy_bills,
)
import scripts.seed_legacy_bills as seed_cli  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, routes):
        self.routes = {key: list(value) for key, value in routes.items()}
        self.calls = []

    def get(self, url, *, params, timeout):
        path = urlsplit(url).path
        self.calls.append((path, dict(params), timeout))
        response = self.routes[path].pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def bill_payload(number=1, bill_type="HR"):
    return {
        "congress": 103,
        "type": bill_type,
        "number": str(number),
        "title": f"Test bill {number}",
        "originChamber": "House" if bill_type == "HR" else "Senate",
        "introducedDate": "1993-01-05",
        "updateDate": "1993-04-01T12:00:00Z",
        "url": f"https://api.congress.gov/v3/bill/103/{bill_type.lower()}/{number}"
        "?api_key=must-not-persist",
        "sponsors": [
            {
                "bioguideId": "A000001",
                "fullName": "Representative Example",
                "party": "D",
                "state": "CA",
                "isByRequest": "Y",
            }
        ],
        "laws": [],
    }


def test_canonical_mapping_and_passage_compatibility():
    row = canonicalize_bill(
        bill_payload(),
        [
            {"actionDate": "1993-02-01", "actionCode": "8000", "text": "Passed House"},
            {
                "actionDate": "1993-03-01",
                "text": "Passed Senate without amendment by voice vote.",
            },
        ],
    )
    assert set(row) == set(BILL_COLUMNS)
    assert row["bill_id"] == "103-hr-1"
    assert row["origin_chamber"] == "house"
    assert row["sponsor_bioguide"] == "A000001"
    assert row["is_by_request"] is True
    assert row["passed_house"] is True
    assert row["passed_senate"] is True
    assert row["passed_any_chamber"] is True
    assert "api_key" not in row["source_url"]


def test_enactment_mapping():
    item = bill_payload()
    item["laws"] = [{"type": "Public Law", "number": "103-1"}]
    row = canonicalize_bill(
        item,
        [
            {"actionDate": "1993-02-01", "actionCode": "8000", "text": ""},
            {"actionDate": "1993-02-02", "actionCode": "17000", "text": ""},
            {
                "actionDate": "1993-02-03",
                "actionCode": "36000",
                "text": "Became Public Law No: 103-1.",
            },
        ],
    )
    assert row["became_law"] is True
    assert row["law_type"] == "Public Law"
    assert row["law_number"] == "103-1"
    assert row["became_law_date"] == "1993-02-03"


def test_bill_and_action_pagination():
    session = FakeSession(
        {
            "/v3/bill/103/hr": [
                FakeResponse(
                    {
                        "bills": [{"number": "1"}],
                        "pagination": {
                            "next": "https://api.congress.gov/v3/bill/103/hr?offset=1"
                        },
                    }
                ),
                FakeResponse({"bills": [{"number": "2"}], "pagination": {}}),
            ],
            "/v3/bill/103/hr/1/actions": [
                FakeResponse(
                    {
                        "actions": [{"text": "Introduced"}],
                        "pagination": {
                            "next": "https://api.congress.gov/v3/bill/103/hr/1/actions"
                            "?offset=1&api_key=leak"
                        },
                    }
                ),
                FakeResponse(
                    {"actions": [{"text": "Passed House"}], "pagination": {}}
                ),
            ],
        }
    )
    client = CongressAPIClient(
        "secret", session=session, min_interval=0, sleep=lambda _: None
    )
    assert [item["number"] for item in client.iter_bill_summaries(103, "HR")] == [
        "1",
        "2",
    ]
    assert len(list(client.iter_actions(103, "HR", 1))) == 2
    assert all(call[1]["api_key"] == "secret" for call in session.calls)
    assert all("api_key" not in call[0] for call in session.calls)


def seed_routes():
    return {
        "/v3/bill/103/hr": [
            FakeResponse(
                {
                    "bills": [{"number": "1"}, {"number": "2"}],
                    "pagination": {},
                }
            )
        ],
        "/v3/bill/103/hr/1": [FakeResponse({"bill": bill_payload(1)})],
        "/v3/bill/103/hr/1/actions": [
            FakeResponse(
                {
                    "actions": [
                        {"actionDate": "1993-02-01", "text": "Passed House"}
                    ],
                    "pagination": {},
                }
            )
        ],
        "/v3/bill/103/hr/2": [FakeResponse({"bill": bill_payload(2)})],
        "/v3/bill/103/hr/2/actions": [
            FakeResponse(
                {
                    "actions": [
                        {"actionDate": "1993-02-02", "text": "Passed House"}
                    ],
                    "pagination": {},
                }
            )
        ],
    }


def test_checkpoint_resume_and_idempotent_merge(tmp_path: Path):
    output = tmp_path / "bills"
    state = tmp_path / "state" / "seed.json"
    interrupted_routes = seed_routes()
    interrupted_routes["/v3/bill/103/hr/2"] = [requests.Timeout()]
    with pytest.raises(RuntimeError):
        seed_legacy_bills(
            CongressAPIClient(
                "secret",
                session=FakeSession(interrupted_routes),
                min_interval=0,
                max_retries=0,
            ),
            output_path=output,
            state_path=state,
            congresses=[103],
            bill_types=["HR"],
            batch_size=1,
        )
    assert load_completed(state) == {"103-hr-1"}
    assert list(load_bills(output)["bill_id"]) == ["103-hr-1"]

    resume_routes = seed_routes()
    resume_routes.pop("/v3/bill/103/hr/1")
    resume_routes.pop("/v3/bill/103/hr/1/actions")
    resumed = seed_legacy_bills(
        CongressAPIClient("secret", session=FakeSession(resume_routes), min_interval=0),
        output_path=output,
        state_path=state,
        congresses=[103],
        bill_types=["HR"],
        batch_size=1,
    )
    assert resumed.fetched == 1 and resumed.skipped == 1
    assert load_completed(state) == {"103-hr-1", "103-hr-2"}
    original = load_bills(output)
    assert list(original["bill_id"]) == ["103-hr-1", "103-hr-2"]

    repeat_session = FakeSession(
        {"/v3/bill/103/hr": seed_routes()["/v3/bill/103/hr"]}
    )
    second = seed_legacy_bills(
        CongressAPIClient("secret", session=repeat_session, min_interval=0),
        output_path=output,
        state_path=state,
        congresses=[103],
        bill_types=["HR"],
    )
    assert second.fetched == 0 and second.skipped == 2
    pd.testing.assert_frame_equal(load_bills(output), original)


def test_checkpoint_contains_no_credentials_or_payload(tmp_path: Path):
    state = tmp_path / "state.json"
    routes = seed_routes()
    routes["/v3/bill/103/hr"][0].payload["bills"] = [{"number": "1"}]
    seed_legacy_bills(
        CongressAPIClient("super-secret", session=FakeSession(routes), min_interval=0),
        output_path=tmp_path / "bills",
        state_path=state,
        congresses=[103],
        bill_types=["HR"],
    )
    saved = state.read_text()
    assert "super-secret" not in saved
    assert "Test bill" not in saved
    assert json.loads(saved)["completed_bill_ids"] == ["103-hr-1"]


def test_checkpoint_does_not_skip_a_missing_partition(tmp_path: Path):
    output = tmp_path / "bills"
    state = tmp_path / "state.json"
    state.write_text('{"version": 1, "completed_bill_ids": ["103-hr-1"]}\n')
    routes = seed_routes()
    routes["/v3/bill/103/hr"][0].payload["bills"] = [{"number": "1"}]
    result = seed_legacy_bills(
        CongressAPIClient("secret", session=FakeSession(routes), min_interval=0),
        output_path=output,
        state_path=state,
        congresses=[103],
        bill_types=["HR"],
    )
    assert result.fetched == 1
    assert list(load_bills(output)["bill_id"]) == ["103-hr-1"]


def test_missing_key_error(monkeypatch):
    monkeypatch.delenv("CONGRESS_API_KEY", raising=False)
    monkeypatch.delenv("GOVINFO_API_KEY", raising=False)
    with pytest.raises(ValueError, match="CONGRESS_API_KEY"):
        CongressAPIClient()


def test_api_key_environment_precedence_and_fallback(monkeypatch):
    monkeypatch.setenv("CONGRESS_API_KEY", "congress-key")
    monkeypatch.setenv("GOVINFO_API_KEY", "govinfo-key")
    assert CongressAPIClient().api_key == "congress-key"

    monkeypatch.delenv("CONGRESS_API_KEY")
    assert CongressAPIClient().api_key == "govinfo-key"


def test_cli_help_documents_api_key_fallback():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "seed_legacy_bills.py"), "--help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "CONGRESS_API_KEY" in result.stdout
    assert "GOVINFO_API_KEY" in result.stdout


def test_seed_checkpoint_is_not_committed():
    ignore = (ROOT / ".gitignore").read_text()
    assert "data/site/bills_seed_state.json" in ignore


def test_request_failure_hides_api_key_and_retry_is_bounded(
    monkeypatch, caplog, capsys, tmp_path: Path
):
    secret = "SECRET_TEST_KEY"
    sensitive_url = (
        "https://api.congress.gov/v3/bill/103/hr"
        f"?api_key={secret}&format=json"
    )
    request = requests.Request("GET", sensitive_url).prepare()
    failure = requests.ConnectionError(
        f"request failed for {sensitive_url}", request=request
    )
    session = FakeSession(
        {"/v3/bill/103/hr": [failure, failure]}
    )
    client = CongressAPIClient(
        secret,
        session=session,
        min_interval=0,
        max_retries=1,
        sleep=lambda _: None,
    )
    with pytest.raises(RuntimeError, match="2 attempts") as caught:
        list(client.iter_bill_summaries(103, "HR"))
    exception_output = "".join(
        traceback.format_exception(
            type(caught.value), caught.value, caught.value.__traceback__
        )
    )
    assert len(session.calls) == 2
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in exception_output

    cli_session = FakeSession({"/v3/bill/103/hr": [failure]})

    def client_factory(api_key, **kwargs):
        return CongressAPIClient(
            api_key,
            session=cli_session,
            min_interval=0,
            max_retries=kwargs["max_retries"],
            sleep=lambda _: None,
        )

    monkeypatch.setattr(seed_cli, "CongressAPIClient", client_factory)
    caplog.set_level(logging.ERROR, logger="seed_legacy_bills")
    exit_code = seed_cli.main(
        [
            "--api-key",
            secret,
            "--congress-start",
            "103",
            "--congress-end",
            "103",
            "--bill-type",
            "HR",
            "--max-retries",
            "0",
            "--request-interval",
            "0",
            "--list-only",
            "--output",
            str(tmp_path / "bills"),
            "--state",
            str(tmp_path / "state.json"),
        ]
    )
    captured = capsys.readouterr()
    cli_output = captured.out + captured.err + caplog.text
    assert exit_code != 0
    assert len(cli_session.calls) == 1
    assert "Congress.gov request failed after 1 attempt" in cli_output
    assert secret not in cli_output


def test_rate_limit_honors_long_retry_after():
    sleeps = []
    session = FakeSession(
        {
            "/v3/bill/103/hr": [
                FakeResponse(
                    {"error": "over rate limit"},
                    status_code=429,
                    headers={"Retry-After": "2903"},
                ),
                FakeResponse(
                    {"bills": [{"number": "1"}], "pagination": {}}
                ),
            ]
        }
    )
    client = CongressAPIClient(
        "secret",
        session=session,
        min_interval=0,
        max_retries=1,
        sleep=sleeps.append,
    )
    assert [row["number"] for row in client.iter_bill_summaries(103, "HR")] == ["1"]
    assert sleeps == [2903.0]


@pytest.mark.parametrize(
    ("number", "sponsor_bioguide", "sponsor_name"),
    [
        (2842, "B001149", "Rep. Burton, Dan [R-IN-5]"),
        (2843, "E000179", "Rep. Engel, Eliot L. [D-NY-17]"),
    ],
)
def test_known_broken_bill_detail_uses_official_metadata_override(
    number, sponsor_bioguide, sponsor_name
):
    summary = {
        "congress": 107,
        "type": "HR",
        "number": str(number),
        "title": "A bill with a broken Congress.gov detail endpoint.",
        "originChamber": "House",
        "originChamberCode": "H",
        "updateDate": "2025-01-02",
        "url": f"https://api.congress.gov/v3/bill/107/hr/{number}?format=json",
    }
    session = FakeSession(
        {
            f"/v3/bill/107/hr/{number}": [
                FakeResponse({"error": "broken MemberTerm"}, status_code=500)
            ],
            f"/v3/bill/107/hr/{number}/actions": [
                FakeResponse(
                    {
                        "actions": [
                            {
                                "actionDate": "2001-09-05",
                                "actionCode": "1000",
                                "text": "Introduced in House",
                            }
                        ],
                        "pagination": {},
                    }
                )
            ],
        }
    )
    row = CongressAPIClient(
        "secret", session=session, min_interval=0, max_retries=0
    ).fetch_canonical_bill(107, "HR", number, summary=summary)
    assert row["bill_id"] == f"107-hr-{number}"
    assert row["sponsor_bioguide"] == sponsor_bioguide
    assert row["sponsor_name"] == sponsor_name
