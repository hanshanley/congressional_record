"""Offline coverage of PDF-only Record issues and dated member attribution."""

from __future__ import annotations

import io
import json
import zipfile

import pyarrow.parquet as pq
import pytest

from analysis.ingest import legislators
from analysis.ingest import govinfo_bulk
from analysis.ingest.govinfo import build_turns
from analysis.ingest.govinfo_bulk import _turns_from_zip, run_bulk
from analysis.ingest.govinfo_pdf import _paragraphs, _section_turns


def _legislator(bioguide, surname, state, *, chamber="rep", party="Democrat", **term):
    return {
        "id": {"bioguide": bioguide},
        "name": {"last": surname, "first": "Member"},
        "terms": [{
            "type": chamber, "start": "2025-01-03", "end": "2027-01-03",
            "state": state, "party": party, **term,
        }],
    }


def test_pdf_roster_uses_issue_date_chamber_and_party_history(monkeypatch):
    records = [
        _legislator("S1", "Smith", "CA", party="Republican", party_affiliations=[
            {"start": "2025-01-03", "end": "2026-06-01", "party": "Democrat"},
            {"start": "2026-06-02", "end": "2027-01-03", "party": "Republican"},
        ]),
        _legislator("S2", "Smith", "TX", chamber="sen"),
        _legislator("S3", "Smith", "NY", end="2025-12-31"),
        _legislator("S4", "Smith", "AZ", start="2026-10-01"),
    ]
    monkeypatch.setattr(legislators, "_load_legislators", lambda: records)

    before = legislators.members_on("2026-05-01", "house")
    after = legislators.members_on("2026-09-16", "house")
    assert [m["bioguide"] for m in before] == ["S1"]
    assert before[0]["party"] == "Democrat"
    assert after[0]["party"] == "Republican"
    assert [m["bioguide"] for m in legislators.members_on("2026-09-16", "senate")] == ["S2"]


def test_pdf_roster_preserves_compound_names_and_rejects_ambiguity(monkeypatch):
    records = [
        _legislator("S1", "Smith", "CA"),
        _legislator("S2", "Smith", "TX", party="Republican"),
        _legislator("W1", "Wasserman Schultz", "FL"),
        _legislator("L1", "Luj\u00e1n", "NM"),
    ]
    monkeypatch.setattr(legislators, "_load_legislators", lambda: records)
    turns = list(build_turns(
        "Mr. SMITH. Unresolved surname.\n"
        "Mr. SMITH of Texas. Resolved state.\n"
        "Ms. WASSERMAN SCHULTZ. Compound surname.\n"
        "Mr. LUJAN. Normalized surname.\n",
        legislators.members_on("2026-09-16", "house"),
        "CREC-2026-09-16-house", "2026-09-16", 119, "house",
    ))
    assert [t["bioguide"] for t in turns] == ["", "S2", "W1", "L1"]
    assert turns[0]["party"] == "other"


def test_pdf_roster_does_not_guess_when_date_is_uncovered(monkeypatch):
    monkeypatch.setattr(legislators, "_load_legislators", lambda: [
        _legislator("S1", "Smith", "CA"),
    ])
    with pytest.raises(ValueError, match="no legislator identities"):
        legislators.members_on("2020-01-01", "house")


def test_pdf_roster_handles_aliases_and_term_boundaries(monkeypatch):
    record = _legislator("S1", "New Surname", "CA")
    record["other_names"] = [{"last": "Old Surname", "end": "2026-06-01"}]
    record["terms"].append({
        "type": "rep", "start": "2027-01-03", "end": "2029-01-03",
        "state": "CA", "party": "Republican",
    })
    monkeypatch.setattr(legislators, "_load_legislators", lambda: [record])
    assert len(legislators.members_on("2026-05-01", "house")) == 2
    assert len(legislators.members_on("2026-09-16", "house")) == 1
    assert legislators.members_on("2027-01-03", "house")[0]["party"] == "Republican"


def _archive(pkg, files):
    buffer = io.BytesIO()
    related = "".join(
        f'<relatedItem type="constituent" ID="id-{pkg}-{section}">'
        f"<extension><granuleClass>{section.upper()}</granuleClass>"
        "</extension></relatedItem>"
        for section in ("house", "senate")
    )
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"{pkg}/mods.xml",
            '<mods xmlns="http://www.loc.gov/mods/v3">'
            "<extension><congress>119</congress></extension>"
            f"{related}</mods>",
        )
        for name, data in files.items():
            archive.writestr(f"{pkg}/{name}", data)
    return buffer.getvalue()


def test_bulk_refresh_replaces_pdf_turns_and_prefers_html(tmp_path, monkeypatch):
    pkg = "CREC-2026-09-16"
    bulk = tmp_path / "bulk"
    bulk.mkdir()
    out = tmp_path / "out"
    member = {"bioguide": "S1", "name": "Smith, Member", "party": "D", "state": "CA"}

    def ingest(pdf, text):
        gid = f"{pkg}-house" if pdf else f"{pkg}-pt1-PgH5835"
        rows = list(build_turns(
            f"Mr. SMITH. {text}", [member], gid, "2026-09-16", 119, "house",
        ))
        if pdf:
            for row in rows:
                row["turn_id"] = row["turn_id"].replace("#", "#pdf-")
        monkeypatch.setattr(govinfo_bulk, "_turns_from_zip", lambda *_: iter(rows))
        (bulk / f"{pkg}.zip").write_bytes(_archive(pkg, {}))
        return run_bulk([pkg], bulk, out, workers=1)

    parquet = out / "turns" / "govinfo_bulk_119.parquet"
    assert ingest(True, "Original PDF remarks.") == 1
    manifest = json.loads((out / "coverage" / "govinfo_bulk_latest.json").read_text())
    assert manifest["pdf_packages"] == [pkg]
    assert ingest(True, "Corrected PDF remarks.") == 0
    assert pq.read_table(parquet).to_pylist()[0]["text"] == "Corrected PDF remarks."
    assert ingest(False, "Official HTML remarks.") == 1
    rows = pq.read_table(parquet).to_pylist()
    assert len(rows) == 1
    assert "#pdf-" not in rows[0]["turn_id"]
    assert rows[0]["text"] == "Official HTML remarks."
    assert ingest(True, "Older PDF remarks.") == 0
    assert pq.read_table(parquet).to_pylist() == rows


def test_bulk_failed_pdf_refresh_preserves_existing_output(tmp_path, monkeypatch):
    pkg = "CREC-2026-09-16"
    bulk, out = tmp_path / "bulk", tmp_path / "out"
    bulk.mkdir()
    rows = list(build_turns(
        "Mr. SMITH. Existing remarks.", [], f"{pkg}-house",
        "2026-09-16", 119, "house",
    ))
    rows[0]["turn_id"] = rows[0]["turn_id"].replace("#", "#pdf-")
    monkeypatch.setattr(govinfo_bulk, "_turns_from_zip", lambda *_: iter(rows))
    (bulk / f"{pkg}.zip").write_bytes(_archive(pkg, {}))
    run_bulk([pkg], bulk, out, workers=1)
    parquet = out / "turns" / "govinfo_bulk_119.parquet"
    original = parquet.read_bytes()

    def broken(*_):
        raise ValueError("PDF contains no extractable text")

    monkeypatch.setattr(govinfo_bulk, "_turns_from_zip", broken)
    (bulk / f"{pkg}.zip").write_bytes(_archive(pkg, {}))
    with pytest.raises(ValueError, match="no extractable text"):
        run_bulk([pkg], bulk, out, workers=1)
    assert parquet.read_bytes() == original
    manifest = json.loads((out / "coverage" / "govinfo_bulk_latest.json").read_text())
    assert manifest["published"] is False


def test_bulk_failure_identifies_package_and_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(govinfo_bulk, "_download", lambda *_: None)
    with pytest.raises(RuntimeError, match=r"CREC-2026-09-16 \(download_failed\)"):
        run_bulk(["CREC-2026-09-16"], tmp_path / "bulk", tmp_path / "out", workers=1)


def test_html_archive_does_not_invoke_pdf_fallback(monkeypatch):
    from analysis.ingest import govinfo_pdf

    def fail(*args):
        pytest.fail("HTML should take precedence over duplicate PDF renditions")

    monkeypatch.setattr(govinfo_pdf, "turns_from_pdfs", fail)
    pkg = "CREC-2026-09-16"
    rows = list(_turns_from_zip(_archive(pkg, {
        f"html/{pkg}-house.htm": "<pre>Mr. SMITH. HTML remarks.</pre>",
        f"pdf/{pkg}-house.pdf": b"must not be read",
    }), pkg))
    assert len(rows) == 1
    assert rows[0]["text"] == "HTML remarks."
    assert "#pdf-" not in rows[0]["turn_id"]


def _pdf(pages, *, width=612, height=792):
    """Minimal text PDFs keep layout tests independent of a PDF-writing library."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Times-Roman >>",
    ]
    kids = []
    for lines in pages:
        page_id = len(objects) + 1
        content_id = page_id + 1
        kids.append(f"{page_id} 0 R")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        stream = []
        for x, top, text, size in lines:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream.append(
                f"BT /F1 {size} Tf {x} {height - top - size} Td ({escaped}) Tj ET\n".encode()
            )
        data = b"".join(stream)
        objects.append(
            f"<< /Length {len(data)} >>\nstream\n".encode() + data + b"endstream"
        )
    objects[1] = f"<< /Type /Pages /Count {len(kids)} /Kids [{' '.join(kids)}] >>".encode()
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(document))
        document.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode())
    document.extend(
        f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return bytes(document)


def _mock_roster(monkeypatch):
    monkeypatch.setattr("analysis.ingest.govinfo_pdf.members_on", lambda *_: [
        {"bioguide": "S1", "name": "Smith, Member", "party": "D", "state": "CA"},
        {"bioguide": "J1", "name": "Jones, Member", "party": "R", "state": "TX"},
    ])


def test_pdf_columns_wrapping_and_publication_headers(monkeypatch):
    _mock_roster(monkeypatch)
    data = _pdf([
        [
            (45, 20, "CONGRESSIONAL RECORD HOUSE", 10),
            (53, 210, "Mr. SMITH. First column.", 8),
            (45, 221, "Student-athletes support co-", 8),
            (222, 210, "operation and student-", 8),
            (222, 221, "athletes.", 8),
            (230, 232, "Ms. JONES. Third speaker.", 8),
            (399, 210, "Continuation across columns.", 8),
            (45, 768, "VerDate production footer", 6),
        ],
        [
            (45, 20, "CONGRESSIONAL RECORD HOUSE", 10),
            (45, 40, "And across pages.", 8),
        ],
    ])
    rows = list(_section_turns(data, "CREC-2026-09-16-house", "2026-09-16", 119, "house"))
    assert [r["bioguide"] for r in rows] == ["S1", "J1"]
    assert rows[0]["text"] == "First column. Student-athletes support cooperation and student-athletes."
    assert rows[1]["text"] == "Third speaker. Continuation across columns. And across pages."
    assert all("#pdf-" in r["turn_id"] for r in rows)


def test_pdf_section_boundaries_and_insertions_end_member_attribution(monkeypatch):
    _mock_roster(monkeypatch)
    data = _pdf([[
        (53, 210, "Mr. SMITH. Spoken remarks.", 8),
        (100, 223, "f", 8),
        (80, 237, "NEW BUSINESS", 8),
        (45, 250, "Unattributed administrative material.", 8),
        (53, 263, "Ms. JONES. I offer the bill.", 8),
        (53, 276, "The text of the bill is as follows:", 8),
        (45, 289, "Printed legislative provisions.", 7),
        (53, 302, "Mr. SMITH. Printed quotation.", 7),
        (53, 315, "Mr. SMITH. Back on the floor.", 8),
        (53, 328, "[Roll No. 308]", 7),
        (45, 341, "Smith Jones Other Names", 7),
        (53, 354, "Mr. JONES changed his vote.", 8),
        (53, 367, "Ms. JONES. After the vote.", 8),
    ]])
    rows = list(_section_turns(data, "CREC-2026-09-16-house", "2026-09-16", 119, "house"))
    eligible = [r for r in rows if r["bioguide"] and not r["is_procedural"]]
    assert [r["text"] for r in eligible] == [
        "Spoken remarks.", "I offer the bill.", "Back on the floor.", "After the vote.",
    ]


def test_pdf_bill_suppression_survives_full_width_tables_and_pages(monkeypatch):
    _mock_roster(monkeypatch)
    data = _pdf([
        [
            (53, 210, "Mr. SMITH. I offer this bill.", 8),
            (53, 222, "The text of the bill, as amended,", 8),
            (45, 233, "is as follows:", 8),
            (45, 245, "Inserted legislative language.", 7),
        ],
        [(210, top, "Table crossing the gutter.", 7) for top in (40, 52, 64)],
        [
            (45, 40, "More inserted legislative language.", 7),
            (53, 55, "Ms. JONES. Back on the floor.", 8),
        ],
    ])
    rows = list(_section_turns(data, "CREC-2026-09-16-house", "2026-09-16", 119, "house"))
    eligible = [r for r in rows if r["bioguide"] and not r["is_procedural"]]
    assert [r["text"] for r in eligible] == ["I offer this bill.", "Back on the floor."]


def test_pdf_non_direct_and_small_print_markers_cannot_impersonate_members(monkeypatch):
    _mock_roster(monkeypatch)
    data = _pdf([[
        (53, 210, "Mr. SMITH. Floor remarks.", 8),
        (70, 224, "Mr. JONES. An indented quotation.", 8),
        (53, 240, "Ms. JONES. An inserted quotation.", 7),
        (53, 256, "Ms. JONES. Real floor remarks.", 8),
    ]])
    rows = list(_section_turns(data, "CREC-2026-09-16-house", "2026-09-16", 119, "house"))
    smith = [r for r in rows if r["bioguide"] == "S1"]
    assert len(smith) == 1
    assert smith[0]["text"] == "Floor remarks."
    jones = [r for r in rows if r["bioguide"] == "J1"]
    assert len(jones) == 1
    assert jones[0]["text"] == "Real floor remarks."


def test_pdf_archive_missing_a_declared_section_fails():
    pkg = "CREC-2026-09-16"
    with pytest.raises(ValueError, match="missing or unsupported house section PDF"):
        list(_turns_from_zip(_archive(pkg, {}), pkg))


def test_pdf_rejects_image_only_and_unsupported_layout(monkeypatch):
    _mock_roster(monkeypatch)
    with pytest.raises(ValueError, match="no extractable text"):
        _paragraphs(_pdf([[]]), "house", "fixture")
    with pytest.raises(ValueError, match="unsupported PDF page size"):
        _paragraphs(_pdf([[(53, 210, "Text.", 8)]], width=700), "house", "fixture")
    with pytest.raises(ValueError, match="non-columnar layout"):
        list(_section_turns(
            _pdf([
                [(53, 210, "Mr. SMITH. Floor remarks.", 8)],
                [(210, top, "Crossing the gutter.", 8) for top in (40, 52, 64)],
            ]),
            "CREC-2026-09-16-house", "2026-09-16", 119, "house",
        ))


def test_pdf_bulk_archive_scores_both_update_surfaces(tmp_path, monkeypatch):
    from analysis.daily_language import aggregate_turn_files
    from analysis.speakers import speaker_counts

    _mock_roster(monkeypatch)
    pkg = "CREC-2026-09-16"
    bulk, out = tmp_path / "bulk", tmp_path / "out"
    bulk.mkdir()
    (bulk / f"{pkg}.zip").write_bytes(_archive(pkg, {
        f"pdf/{pkg}-house.pdf": _pdf([[(53, 210, "Mr. SMITH. I thank my colleague.", 8)]]),
        f"pdf/{pkg}-senate.pdf": _pdf([[(53, 210, "Ms. JONES. I thank my colleague.", 8)]]),
        f"pdf/{pkg}.pdf": b"duplicate full issue must not be read",
    }))
    assert run_bulk([pkg], bulk, out, workers=1) == 2
    files = list((out / "turns").glob("*.parquet"))
    speakers = speaker_counts(files)
    aggregate = aggregate_turn_files(files)
    assert set(speakers["bioguide"]) == {"S1", "J1"}
    assert set(aggregate["chamber"]) == {"house", "senate"}
    assert aggregate["words"].sum() == speakers["words"].sum() == 8
