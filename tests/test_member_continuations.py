"""Focused coverage for source-proven unmarked floor continuations."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analysis.ingest.govinfo import (
    build_turns,
    identify_unmarked_floor_continuation,
    normalize_members,
)
from analysis.ingest.govinfo_bulk import parse_package_mods
from analysis.ingest.schema import ARROW_SCHEMA
from scripts.repair_member_continuations import main as repair_main


def _member(
    bioguide: str = "S000148",
    *,
    role: str = "SPEAKING",
    parsed_name: str = "Mr. SCHUMER",
    chamber: str = "S",
):
    return {
        "party": "D",
        "bioGuideId": bioguide,
        "state": "NY",
        "chamber": chamber,
        "role": role,
        "name": "Charles E. Schumer",
        "parsed_name": parsed_name,
    }


def test_source_proven_initial_continuation_and_resumption_are_attributed():
    members = normalize_members([_member()])
    rows = list(build_turns(
        "Nomination of Russell Vought\n\n"
        "  Mr. President, I will begin with the budget.\n\n"
        "       A printed quotation begins here.\n"
        "     It remains inserted material.\n\n"
        "  I resume my own remarks. This is damn important.",
        members,
        "CREC-2025-01-30-pt1-PgS503-3",
        "2025-01-30",
        119,
        "senate",
    ))

    assert [row["turn_id"].split("#", 1)[1] for row in rows] == [
        "0", "0-inserted", "0-resumed-1",
    ]
    own = [row for row in rows if not row["is_procedural"]]
    assert all(row["bioguide"] == "S000148" for row in own)
    assert all(row["speaker_name"] == "Mr. SCHUMER" for row in own)
    inserted = [row for row in rows if row["is_procedural"]]
    assert len(inserted) == 1
    assert not inserted[0]["bioguide"]
    assert inserted[0]["speaker_name"] == "Inserted material"


def test_explicit_named_member_is_never_overridden_by_continuation_metadata():
    members = normalize_members([
        _member(),
        {
            "party": "D",
            "bioGuideId": "D000563",
            "state": "IL",
            "chamber": "S",
            "role": "MENTIONED",
            "name": "Richard J. Durbin",
            "parsed_name": "Mr. DURBIN",
        },
    ])
    rows = list(build_turns(
        "Mr. DURBIN. Mr. President, these are my own remarks.",
        members,
        "CREC-2025-01-30-pt1-PgS503-4",
        "2025-01-30",
        119,
        "senate",
    ))
    assert len(rows) == 1
    assert rows[0]["speaker_name"] == "Mr. DURBIN"
    assert rows[0]["bioguide"] == "D000563"


@pytest.mark.parametrize(
    ("text", "members", "chamber", "non_spoken", "reason"),
    [
        (
            "Mr. President, floor remarks.",
            normalize_members([_member(), _member("D000563", parsed_name="Mr. DURBIN")]),
            "senate",
            False,
            "multiple_speaking_authors",
        ),
        (
            "Mr. President, floor remarks.",
            normalize_members([_member(role="VOTED YES")]),
            "senate",
            False,
            "no_speaking_author",
        ),
        (
            "Mr. President, floor remarks.",
            [],
            "senate",
            False,
            "no_speaking_author",
        ),
        (
            "Mr. President, floor remarks.",
            normalize_members([_member("")]),
            "senate",
            False,
            "speaking_author_without_valid_bioguide",
        ),
        (
            "Nomination of Russell Vought without a direct address.",
            normalize_members([_member()]),
            "senate",
            False,
            "no_direct_floor_address",
        ),
        (
            "Mr. President, submitted remarks.",
            normalize_members([_member()]),
            "senate",
            True,
            "non_spoken_content",
        ),
        (
            "Mr. President, digest text.",
            normalize_members([_member()]),
            "other",
            False,
            "nonfloor_chamber",
        ),
    ],
)
def test_unmarked_continuation_requires_every_authoritative_condition(
    text, members, chamber, non_spoken, reason,
):
    author, actual_reason = identify_unmarked_floor_continuation(
        text, members, chamber, non_spoken=non_spoken,
    )
    assert author is None
    assert actual_reason == reason


def test_package_mods_preserves_role_and_parsed_display_name():
    gid = "CREC-2025-01-30-pt1-PgS503-3"
    metadata = parse_package_mods(_mods_xml("CREC-2025-01-30", {
        gid: ("S503", "S503", [_member()]),
    }))
    member = metadata[gid]["members"][0]
    assert member["role"] == "SPEAKING"
    assert member["parsed_name"] == "Mr. SCHUMER"
    assert member["name"] == "Charles E. Schumer"


def _mods_xml(_package: str, granules) -> bytes:
    related = []
    for gid, (first, last, members) in granules.items():
        member_xml = []
        for member in members:
            member_xml.append(
                '<congMember bioGuideId="{bioGuideId}" chamber="{chamber}" '
                'congress="119" party="{party}" role="{role}" state="{state}">'
                '<name type="parsed">{parsed_name}</name>'
                '<name type="authority-fnf">{name}</name>'
                "</congMember>".format(**member)
            )
        related.append(
            f'<relatedItem type="constituent" ID="id-{gid}">'
            f'<extent unit="pages"><start>{first}</start><end>{last}</end></extent>'
            "<extension><granuleClass>SENATE</granuleClass>"
            "<subGranuleClass>SNOMINATIONS</subGranuleClass>"
            "<chamber>SENATE</chamber>"
            f"{''.join(member_xml)}</extension></relatedItem>"
        )
    return (
        '<mods xmlns="http://www.loc.gov/mods/v3">'
        "<extension><congress>119</congress></extension>"
        f"{''.join(related)}</mods>"
    ).encode()


def _write_turns(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(rows, schema=ARROW_SCHEMA),
        path,
        compression="zstd",
    )


def _write_coverage(audit: Path, start: str, end: str, packages) -> None:
    (audit / "source_coverage.json").write_text(json.dumps({
        "start": start,
        "end": end,
        "packages": list(packages),
    }))


def _boundary_row(congress: int, date: str):
    package = f"CREC-{date}"
    return list(build_turns(
        "Nomination at the Congress boundary\n\n"
        "  Mr. President, this out-of-scope attribution must remain untouched.",
        [],
        f"{package}-pt1-PgS1",
        date,
        congress,
        "senate",
    ))[0]


def _utility_fixture(tmp_path: Path):
    package = "CREC-2025-01-30"
    known_members = normalize_members([_member()])
    long_body = (
        "Nomination of Russell Vought\n\n"
        "  Mr. President, "
        + "these are my continuing floor remarks about the pending nomination. " * 20
    )
    gids = {
        "broad": f"{package}-pt1-PgS510-2",
        "alias": f"{package}-pt1-PgS510-3",
        "repeat": f"{package}-pt1-PgS512-2",
        "continuation": f"{package}-pt1-PgS503-3",
    }
    rows = []
    rows.extend(build_turns(
        f"Mr. SCHUMER. {long_body}",
        known_members, gids["broad"], "2025-01-30", 119, "senate",
    ))
    rows.extend(build_turns(
        long_body,
        [], gids["alias"], "2025-01-30", 119, "senate",
    ))
    rows.extend(build_turns(
        f"Mr. SCHUMER. {long_body}",
        known_members, gids["repeat"], "2025-01-30", 119, "senate",
    ))
    rows.extend(build_turns(
        "Nomination of Russell Vought\n\n"
        "  Mr. President, I begin with words that contain no profanity.\n\n"
        "       A printed quotation is not my floor speech.\n"
        "     This remains inserted material.\n\n"
        "  I resume my own remarks, and this is damn important.",
        [],
        gids["continuation"],
        "2025-01-30",
        119,
        "senate",
    ))

    turns = tmp_path / "turns"
    source = turns / "govinfo_bulk_119.parquet"
    _write_turns(source, rows)
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "summary.json").write_text(json.dumps({
        "mode": "members",
        "congress": 119,
        "turns_checked": len(rows),
        "scorer_disagreements": 0,
    }))
    cache = tmp_path / "metadata"
    cache.mkdir()
    granules = {
        gids["broad"]: ("S510", "S511", [_member()]),
        gids["alias"]: ("S510", "S510", [_member()]),
        gids["repeat"]: ("S512", "S512", [_member()]),
        gids["continuation"]: ("S503", "S503", [_member()]),
    }
    (cache / f"{package}.mods.xml").write_bytes(_mods_xml(package, granules))
    return rows, source, turns, audit, cache, gids


def test_complete_source_repair_preserves_text_and_rededuplicates_overlap(tmp_path):
    rows, source, turns, audit, cache, gids = _utility_fixture(tmp_path)
    original_bytes = source.read_bytes()
    out = tmp_path / "repaired"

    assert repair_main([
        "--turns", str(turns),
        "--audit", str(audit),
        "--metadata-cache", str(cache),
        "--out", str(out),
        "--workers", "2",
    ]) == 0

    assert source.read_bytes() == original_bytes
    repaired = pq.read_table(out / source.name).to_pylist()
    by_id = {row["turn_id"]: row for row in repaired}
    continuation = f"crec:{gids['continuation']}"
    assert by_id[f"{continuation}#0"]["bioguide"] == "S000148"
    assert by_id[f"{continuation}#0-resumed-1"]["bioguide"] == "S000148"
    inserted = by_id[f"{continuation}#0-inserted"]
    assert inserted["is_procedural"] and not inserted["bioguide"]
    assert "damn important" in by_id[f"{continuation}#0-resumed-1"]["text"]

    assert f"crec:{gids['alias']}#0" not in by_id
    assert f"crec:{gids['broad']}#0" in by_id
    assert f"crec:{gids['repeat']}#0" in by_id
    assert len(repaired) == len(rows) - 1

    proof = json.loads((out / "continuation_repair_proof.json").read_text())
    assert proof["scoped_original_turn_count"] == len(rows)
    assert len(proof["attribution_changes"]) == 3
    assert proof["original_text_preservation"]["asserted"] is True
    assert proof["input_immutability"]["asserted"] is True
    assert proof["final_row_counts"]["total"] == len(rows) - 1
    assert proof["duplicate_removals"] == [{
        "kept_provenance": {
            "page_range": ["S", 510, 511],
            "turn_id": f"crec:{gids['broad']}#0",
        },
        "removed_page_range": ["S", 510, 510],
        "removed_turn_id": f"crec:{gids['alias']}#0",
    }]


def test_candidate_only_source_is_rejected_as_incomplete(tmp_path):
    rows, _, _, audit, cache, _ = _utility_fixture(tmp_path)
    candidates = tmp_path / "candidate-only"
    _write_turns(candidates / "congress_119.parquet", [rows[-1]])
    boundary = _boundary_row(118, "2025-01-02")
    _write_turns(candidates / "congress_118.parquet", [boundary])
    _write_coverage(
        audit, "2025-01-01", "2027-01-03",
        ["CREC-2025-01-02"],
    )

    with pytest.raises(ValueError, match="incomplete scoped Congress"):
        repair_main([
            "--turns", str(candidates),
            "--audit", str(audit),
            "--metadata-cache", str(cache),
            "--out", str(tmp_path / "must-not-exist"),
        ])


def test_legitimate_adjacent_congress_boundary_is_preserved_and_proven(tmp_path):
    rows, _, turns, audit, cache, _ = _utility_fixture(tmp_path)
    boundary = _boundary_row(118, "2025-01-02")
    boundary_path = turns / "govinfo_bulk_118.parquet"
    _write_turns(boundary_path, [boundary])
    _write_coverage(
        audit, "2025-01-01", "2027-01-03",
        ["CREC-2025-01-02", "CREC-2025-01-30"],
    )
    out = tmp_path / "boundary-repaired"

    assert repair_main([
        "--turns", str(turns),
        "--audit", str(audit),
        "--metadata-cache", str(cache),
        "--out", str(out),
    ]) == 0

    repaired_boundary = pq.read_table(out / boundary_path.name).to_pylist()
    expected = [{name: boundary[name] for name in ARROW_SCHEMA.names}]
    assert repaired_boundary == expected
    proof = json.loads((out / "continuation_repair_proof.json").read_text())
    boundary_proof = proof["adjacent_congress_boundary_rows"]
    assert boundary_proof["total"] == 1
    assert boundary_proof["by_congress"] == {"118": 1}
    assert boundary_proof["by_date"] == {"2025-01-02": 1}
    assert boundary_proof["by_package"] == {"CREC-2025-01-02": 1}
    assert boundary_proof["source_coverage"]["available"] is True
    assert boundary_proof["preservation"]["asserted"] is True
    assert proof["scoped_original_turn_count"] == len(rows)


def test_arbitrary_mixed_congress_row_is_rejected(tmp_path):
    _, _, turns, audit, cache, _ = _utility_fixture(tmp_path)
    invalid = _boundary_row(117, "2025-01-02")
    _write_turns(turns / "govinfo_bulk_117.parquet", [invalid])
    _write_coverage(
        audit, "2025-01-01", "2027-01-03",
        ["CREC-2025-01-02"],
    )

    with pytest.raises(ValueError, match="non-boundary Congress row"):
        repair_main([
            "--turns", str(turns),
            "--audit", str(audit),
            "--metadata-cache", str(cache),
            "--out", str(tmp_path / "invalid-mixed"),
        ])


def test_adjacent_boundary_must_be_in_retained_coverage(tmp_path):
    _, _, turns, audit, cache, _ = _utility_fixture(tmp_path)
    boundary = _boundary_row(118, "2025-01-02")
    _write_turns(turns / "govinfo_bulk_118.parquet", [boundary])
    _write_coverage(
        audit, "2025-01-01", "2027-01-03",
        ["CREC-2025-01-03"],
    )

    with pytest.raises(ValueError, match="absent from retained source coverage"):
        repair_main([
            "--turns", str(turns),
            "--audit", str(audit),
            "--metadata-cache", str(cache),
            "--out", str(tmp_path / "invalid-coverage"),
        ])


def test_legitimate_non_speaking_metadata_stays_unknown_with_reason(tmp_path):
    package = "CREC-2025-01-30"
    gid = f"{package}-pt1-PgS503-3"
    rows = list(build_turns(
        "Nomination of Russell Vought\n\n"
        "  Mr. President, these remarks remain unidentified.",
        [], gid, "2025-01-30", 119, "senate",
    ))
    turns = tmp_path / "turns"
    source = turns / "govinfo_bulk_119.parquet"
    _write_turns(source, rows)
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "summary.json").write_text(json.dumps({
        "mode": "members",
        "congress": 119,
        "turns_checked": 1,
        "scorer_disagreements": 0,
    }))
    cache = tmp_path / "metadata"
    cache.mkdir()
    (cache / f"{package}.mods.xml").write_bytes(_mods_xml(package, {
        gid: ("S503", "S503", [_member(role="VOTED YES")]),
    }))
    out = tmp_path / "repaired"

    assert repair_main([
        "--turns", str(turns),
        "--audit", str(audit),
        "--metadata-cache", str(cache),
        "--out", str(out),
    ]) == 0

    repaired = pq.read_table(out / source.name).to_pylist()
    expected = [{name: row[name] for name in ARROW_SCHEMA.names} for row in rows]
    assert repaired == expected
    proof = json.loads((out / "continuation_repair_proof.json").read_text())
    assert proof["attribution_changes"] == []
    assert proof["unresolved_non_actionable_reasons"] == [{
        "granule_id": gid,
        "package": package,
        "reason": "no_speaking_author",
        "turn_id": f"crec:{gid}#0",
    }]
