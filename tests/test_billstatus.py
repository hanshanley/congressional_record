"""Offline tests for the public GovInfo Bill Status bulk adapter."""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import sys
import zipfile
from html import escape
from pathlib import Path

import pandas as pd
import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.bills import load_bills, save_bills  # noqa: E402
from analysis.ingest.billstatus import (  # noqa: E402
    BASE_URL,
    BillStatusError,
    GovInfoNotFoundError,
    GovInfoBulkClient,
    bill_type_zip_url,
    discover_bill_files,
    parse_bill_type_zip,
    parse_bill_xml,
    update_bill_status,
)


def _bill_xml(
    *,
    congress: int = 119,
    number: int = 1,
    bill_type: str = "HR",
    title: str = "A test bill",
    update: str = "2026-08-03T14:00:00Z",
    sponsor: bool = True,
    actions: tuple[tuple[str, str, str], ...] = (),
    laws: tuple[tuple[str, str], ...] = (),
) -> bytes:
    chamber = "House" if bill_type == "HR" else "Senate"
    chamber_code = "H" if bill_type == "HR" else "S"
    sponsor_xml = """
      <sponsors><item>
        <bioguideId>A000001</bioguideId>
        <fullName>Rep. Example, Alex [D-CA-1]</fullName>
        <party>D</party><state>CA</state><isByRequest>N</isByRequest>
      </item></sponsors>""" if sponsor else ""
    actions_xml = "".join(
        f"""<item><actionDate>{escape(date)}</actionDate>
        <actionCode>{escape(code)}</actionCode><text>{escape(text)}</text></item>"""
        for date, code, text in actions
    )
    laws_xml = "".join(
        f"<item><type>{escape(kind)}</type><number>{escape(number_)}</number></item>"
        for kind, number_ in laws
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<billStatus><bill>
  <number>{number}</number><updateDate>2026-08-03T13:00:00Z</updateDate>
  <updateDateIncludingText>{escape(update)}</updateDateIncludingText>
  <originChamber>{chamber}</originChamber><originChamberCode>{chamber_code}</originChamberCode>
  <type>{bill_type}</type><introducedDate>2025-01-03</introducedDate>
  <congress>{congress}</congress>{sponsor_xml}
  <title>{escape(title)}</title>
  <actions>{actions_xml}</actions><laws>{laws_xml}</laws>
</bill></billStatus>""".encode()


def _listing(entries: tuple[tuple[str, str], ...]) -> bytes:
    files = "".join(
        f"""<file><folder>false</folder><name>{escape(name)}</name>
        <formattedLastModifiedTime>{escape(modified)}</formattedLastModifiedTime></file>"""
        for name, modified in entries
    )
    return f"<data><files>{files}</files></data>".encode()


def _bill_zip(
    entries: tuple[tuple[str, bytes], ...],
    *,
    modified: tuple[int, int, int, int, int, int] = (2026, 8, 3, 16, 18, 0),
) -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, xml in entries:
            info = zipfile.ZipInfo(name, date_time=modified)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, xml)
    return data.getvalue()


class FakeClient:
    def __init__(self, responses: dict[str, bytes | Exception]):
        self.responses = responses
        self.requested: list[str] = []

    def get_bytes(self, url: str, *, max_bytes: int) -> bytes:
        self.requested.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def _url(bill_type: str, number: int, congress: int = 119) -> str:
    return (
        f"{BASE_URL}/{congress}/{bill_type}/"
        f"BILLSTATUS-{congress}{bill_type}{number}.xml"
    )


def test_sponsored_only_bill_maps_source_and_identity_fields():
    row = parse_bill_xml(
        _bill_xml(title="Sponsored only"),
        source_url=_url("hr", 1),
    )
    assert row["bill_id"] == "119-hr-1"
    assert row["source"] == "govinfo"
    assert row["source_updated_at"] == "2026-08-03T14:00:00Z"
    assert row["title"] == "Sponsored only"
    assert row["origin_chamber"] == "house"
    assert row["introduced_date"] == "2025-01-03"
    assert row["sponsor_bioguide"] == "A000001"
    assert row["sponsor_party"] == "D"
    assert not row["passed_any_chamber"]
    assert not row["became_law"]


def test_legacy_billstatus_field_aliases_are_supported():
    data = _bill_xml().replace(b"<number>", b"<billNumber>").replace(
        b"</number>", b"</billNumber>"
    ).replace(b"<type>HR</type>", b"<billType>HR</billType>", 1)
    row = parse_bill_xml(data, source_url=_url("hr", 1))
    assert row["bill_id"] == "119-hr-1"


def test_house_passage_uses_official_8000_code():
    row = parse_bill_xml(
        _bill_xml(actions=(("2025-02-01", "8000", "Passed House"),)),
        source_url=_url("hr", 1),
    )
    assert row["passed_house"]
    assert row["passed_house_date"] == "2025-02-01"
    assert not row["passed_senate"]
    assert json.loads(row["matched_action_codes"]) == ["8000"]


def test_senate_passage_uses_official_17000_code():
    row = parse_bill_xml(
        _bill_xml(
            bill_type="S",
            actions=(("2025-03-01", "17000", "Passed Senate"),),
        ),
        source_url=_url("s", 1),
    )
    assert row["origin_chamber"] == "senate"
    assert row["passed_senate"]
    assert row["passed_senate_date"] == "2025-03-01"
    assert not row["passed_house"]


def test_both_chambers_are_preserved():
    row = parse_bill_xml(
        _bill_xml(
            actions=(
                ("2025-02-01", "8000", "Passed House"),
                ("2025-03-01", "17000", "Passed Senate"),
            )
        ),
        source_url=_url("hr", 1),
    )
    assert row["passed_house"] and row["passed_senate"]
    assert row["passed_any_chamber"]
    assert json.loads(row["matched_action_codes"]) == ["17000", "8000"]


def test_enactment_is_driven_by_laws_even_without_law_action_code():
    row = parse_bill_xml(
        _bill_xml(
            actions=(("2025-02-01", "8000", "Passed House"),),
            laws=(("Public Law", "119-7"),),
        ),
        source_url=_url("hr", 1),
    )
    assert row["became_law"]
    assert row["law_type"] == "Public Law"
    assert row["law_number"] == "119-7"
    assert row["became_law_date"] == ""


def test_missing_sponsor_produces_empty_sponsor_fields():
    row = parse_bill_xml(
        _bill_xml(sponsor=False),
        source_url=_url("hr", 1),
    )
    assert row["sponsor_bioguide"] == ""
    assert row["sponsor_name"] == ""
    assert row["sponsor_party"] == ""
    assert not row["is_by_request"]


@pytest.mark.parametrize(
    "data",
    [
        b"<billStatus><bill>",
        b'<!DOCTYPE x [<!ENTITY y "unsafe">]><billStatus><bill>&y;</bill></billStatus>',
    ],
)
def test_malformed_or_unsafe_xml_is_rejected(data: bytes):
    with pytest.raises(BillStatusError, match="malformed or unsafe XML"):
        parse_bill_xml(data, source_url=_url("hr", 1))


def test_directory_discovery_includes_only_hr_and_s_with_timestamps():
    hr_listing = f"{BASE_URL}/119/hr/"
    s_listing = f"{BASE_URL}/119/s/"
    client = FakeClient(
        {
            hr_listing: _listing(
                (
                    ("BILLSTATUS-119hr2.xml", "03-Aug-2026 16:18"),
                    ("BILLSTATUS-119hres2.xml", "03-Aug-2026 16:18"),
                )
            ),
            s_listing: _listing(
                (("BILLSTATUS-119s4.xml", "02-Aug-2026 12:05"),)
            ),
        }
    )
    records = discover_bill_files(119, client=client)
    assert [(r.bill_type, r.bill_number) for r in records] == [("HR", 2), ("S", 4)]
    assert records[0].modified_at == "2026-08-03T16:18:00"
    assert records[1].url == _url("s", 4)


def test_bulk_client_explicitly_requests_xml_listings():
    session = requests.Session()
    assert session.headers["Accept"] == "*/*"
    GovInfoBulkClient(session=session)
    assert session.headers["Accept"] == "application/xml"


def test_bulk_client_preserves_not_found_as_a_distinct_error(monkeypatch):
    session = requests.Session()
    response = requests.Response()
    response.status_code = 404
    response._content = b""
    monkeypatch.setattr(session, "get", lambda url, timeout: response)
    client = GovInfoBulkClient(session=session)
    listing = f"{BASE_URL}/120/hr/"

    with pytest.raises(GovInfoNotFoundError) as exc_info:
        client.get_bytes(listing, max_bytes=1024)

    assert exc_info.value.url == listing


def test_full_update_uses_bill_type_zips_and_replaces_target_congress(tmp_path: Path):
    store = tmp_path / "bills"
    stale = parse_bill_xml(
        _bill_xml(number=99, title="Stale"),
        source_url=_url("hr", 99),
    )
    save_bills(pd.DataFrame([stale]), store)
    hr_zip_url = bill_type_zip_url(119, "HR")
    s_zip_url = bill_type_zip_url(119, "S")
    client = FakeClient(
        {
            hr_zip_url: _bill_zip(
                (("nested/BILLSTATUS-119hr1.xml", _bill_xml(title="House ZIP")),)
            ),
            s_zip_url: _bill_zip(
                (
                    (
                        "BILLSTATUS-119s2.xml",
                        _bill_xml(number=2, bill_type="S", title="Senate ZIP"),
                    ),
                )
            ),
            f"{BASE_URL}/119/hr/": _listing(
                (("BILLSTATUS-119hr1.xml", "01-Aug-2026 10:00"),)
            ),
            f"{BASE_URL}/119/s/": _listing(
                (("BILLSTATUS-119s2.xml", "03-Aug-2026 16:18"),)
            ),
        }
    )

    result = update_bill_status((119,), store, full=True, client=client, workers=2)

    assert set(client.requested) == {hr_zip_url, s_zip_url}
    assert result.discovered == result.selected == result.fetched == 2
    assert set(result.bills["bill_id"]) == {"119-hr-1", "119-s-2"}
    assert set(result.bills["source_updated_at"]) == {"2026-08-03T16:18:00"}
    assert result.bills.set_index("bill_id").loc["119-hr-1", "source_url"] == _url("hr", 1)

    incremental = update_bill_status((119,), store, client=client)
    assert incremental.selected == 0
    assert sum(url.endswith(".xml") for url in client.requested) == 0


def test_invalid_bill_type_zip_is_rejected():
    with pytest.raises(BillStatusError, match="invalid Bill Status ZIP"):
        parse_bill_type_zip(b"not a zip", congress=119, bill_type="HR")


def test_incremental_update_fetches_only_missing_and_changed_records(tmp_path: Path):
    store = tmp_path / "bills"
    unchanged_url = _url("hr", 1)
    missing_url = _url("hr", 3)
    changed_url = _url("s", 2)
    unchanged = parse_bill_xml(
        _bill_xml(number=1, title="Unchanged"),
        source_url=unchanged_url,
        source_updated_at="2026-08-01T10:00:00",
    )
    changed = parse_bill_xml(
        _bill_xml(number=2, bill_type="S", title="Old"),
        source_url=changed_url,
        source_updated_at="2026-08-01T10:00:00",
    )
    save_bills(pd.DataFrame([unchanged, changed]), store)

    hr_listing = f"{BASE_URL}/119/hr/"
    s_listing = f"{BASE_URL}/119/s/"
    client = FakeClient(
        {
            hr_listing: _listing(
                (
                    ("BILLSTATUS-119hr1.xml", "01-Aug-2026 10:00"),
                    ("BILLSTATUS-119hr3.xml", "03-Aug-2026 12:00"),
                )
            ),
            s_listing: _listing(
                (("BILLSTATUS-119s2.xml", "03-Aug-2026 12:00"),)
            ),
            missing_url: _bill_xml(number=3, title="New"),
            changed_url: _bill_xml(number=2, bill_type="S", title="Changed"),
        }
    )
    result = update_bill_status((119,), store, client=client, workers=2)
    fetched_urls = {url for url in client.requested if url.endswith(".xml")}
    assert result.selected == result.fetched == 2
    assert fetched_urls == {missing_url, changed_url}
    assert len(result.bills) == 3
    assert result.bills.set_index("bill_id").loc["119-s-2", "title"] == "Changed"


def test_rollover_updates_outgoing_when_incoming_listing_is_missing(tmp_path: Path):
    store = tmp_path / "bills"
    outgoing_url = _url("hr", 1)
    old = parse_bill_xml(
        _bill_xml(title="Before rollover"),
        source_url=outgoing_url,
        source_updated_at="2026-12-31T10:00:00",
    )
    save_bills(pd.DataFrame([old]), store)
    incoming_hr_listing = f"{BASE_URL}/120/hr/"
    client = FakeClient(
        {
            f"{BASE_URL}/119/hr/": _listing(
                (("BILLSTATUS-119hr1.xml", "02-Jan-2027 12:00"),)
            ),
            f"{BASE_URL}/119/s/": _listing(()),
            outgoing_url: _bill_xml(
                title="Final outgoing update",
                update="2027-01-02T12:00:00Z",
            ),
            incoming_hr_listing: GovInfoNotFoundError(incoming_hr_listing),
        }
    )

    result = update_bill_status(
        (119, 120),
        store,
        allow_missing_listings_for=(120,),
        client=client,
    )
    loaded = load_bills(store)

    assert result.selected == result.fetched == 1
    assert loaded is not None
    assert loaded.set_index("bill_id").loc["119-hr-1", "title"] == (
        "Final outgoing update"
    )
    assert incoming_hr_listing in client.requested
    assert f"{BASE_URL}/120/s/" not in client.requested


def test_missing_listing_is_not_ignored_without_rollover_allowance(tmp_path: Path):
    listing = f"{BASE_URL}/120/hr/"
    client = FakeClient({listing: GovInfoNotFoundError(listing)})

    with pytest.raises(GovInfoNotFoundError):
        update_bill_status((120,), tmp_path / "bills", client=client)


def test_optional_listing_does_not_hide_parser_errors(tmp_path: Path):
    client = FakeClient({f"{BASE_URL}/120/hr/": b"<not-xml"})

    with pytest.raises(BillStatusError, match="malformed or unsafe XML"):
        update_bill_status(
            (120,),
            tmp_path / "bills",
            allow_missing_listings_for=(120,),
            client=client,
        )


def test_replacement_is_idempotent_across_repeated_updates(tmp_path: Path):
    store = tmp_path / "bills"
    bill_url = _url("hr", 1)
    old = parse_bill_xml(
        _bill_xml(title="Old"),
        source_url=bill_url,
        source_updated_at="2026-08-01T10:00:00",
    )
    save_bills(pd.DataFrame([old]), store)
    hr_listing = f"{BASE_URL}/119/hr/"
    s_listing = f"{BASE_URL}/119/s/"
    client = FakeClient(
        {
            hr_listing: _listing(
                (("BILLSTATUS-119hr1.xml", "03-Aug-2026 12:00"),)
            ),
            s_listing: _listing(()),
            bill_url: _bill_xml(title="Replacement"),
        }
    )

    first = update_bill_status((119,), store, client=client)
    xml_requests_after_first = sum(url.endswith(".xml") for url in client.requested)
    second = update_bill_status((119,), store, client=client)
    loaded = load_bills(store)

    assert first.selected == 1
    assert second.selected == 0
    assert second.written == ()
    assert sum(url.endswith(".xml") for url in client.requested) == xml_requests_after_first
    assert loaded is not None and len(loaded) == 1
    assert loaded.iloc[0]["title"] == "Replacement"


def _load_update_script():
    spec = importlib.util.spec_from_file_location(
        "update_bills_under_test", ROOT / "scripts" / "update_bills.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_scheduled_default_congress_comes_from_today():
    update = _load_update_script()
    assert update.current_congress(dt.date(2026, 8, 3)) == 119


def test_scheduled_rollover_targets_outgoing_and_incoming_congresses():
    update = _load_update_script()
    assert update.routine_congresses(dt.date(2027, 1, 1)) == (119, 120)
    assert update.routine_congresses(dt.date(2027, 1, 14)) == (119, 120)


def test_no_argument_cli_uses_rollover_targets(monkeypatch):
    update = _load_update_script()
    real_date = dt.date

    class RolloverDate(real_date):
        @classmethod
        def today(cls):
            return cls(2027, 1, 2)

    monkeypatch.setattr(update.dt, "date", RolloverDate)

    routine_args = update.parse_args([])
    explicit_args = update.parse_args(["--congress", "120"])

    assert update.congresses_for(routine_args) == (119, 120)
    assert update.congresses_for(explicit_args) == (120,)


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (dt.date(2026, 1, 1), (119,)),
        (dt.date(2027, 1, 15), (120,)),
        (dt.date(2027, 2, 1), (120,)),
    ],
)
def test_scheduled_normal_dates_target_only_current_congress(today, expected):
    update = _load_update_script()
    assert update.routine_congresses(today) == expected


def test_historical_and_invalid_inputs_require_explicit_valid_modes(monkeypatch):
    update = _load_update_script()
    monkeypatch.setattr(update, "current_congress", lambda today=None: 119)
    with pytest.raises(SystemExit):
        update.parse_args(["--congress", "118"])
    with pytest.raises(SystemExit):
        update.parse_args(["--congress", "107", "--full"])
    with pytest.raises(SystemExit):
        update.parse_args(["--workers", "0"])
    args = update.parse_args(["--backfill", "108", "--congress", "110"])
    assert update.congresses_for(args) == (108, 109, 110)
