"""Offline regression tests for the bug-hunter hardening fixes (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crec.api import _redact  # noqa: E402
from crec.download import (  # noqa: E402
    GovInfoError,
    build_manifest_row,
    granule_paths,
    read_manifest_row,
)
from crec.enumerate import next_month  # noqa: E402
import datetime as dt  # noqa: E402
import zipfile  # noqa: E402

import pyarrow.parquet as pq  # noqa: E402

from analysis.ingest.govinfo_bulk import (  # noqa: E402
    _package_congress,
    _turn_fingerprint,
    run_bulk,
)
from analysis.ingest.govinfo import ingest_govinfo  # noqa: E402
from analysis.validate import (  # noqa: E402
    ANNOTATION_FIELDS,
    build_validation_sample,
    read_annotation_pass,
    validation_report,
)
from analysis.ingest.schema import ARROW_SCHEMA  # noqa: E402
import pyarrow as pa  # noqa: E402


def test_redact_masks_api_key() -> None:
    url = "https://api.govinfo.gov/packages/CREC-2024-01-09/granules/g/mods?api_key=SECRET123&x=1"
    red = _redact(url)
    assert "SECRET123" not in red
    assert "api_key=REDACTED" in red
    assert "x=1" in red


def test_redact_no_query_is_noop() -> None:
    url = "https://api.govinfo.gov/collections"
    assert _redact(url) == url


def test_path_traversal_ids_rejected() -> None:
    out = Path("/tmp/out")
    for bad in ("../evil", "a/b", "/etc/passwd", "foo/../bar"):
        try:
            granule_paths(out, "CREC-2024-01-09", bad)
        except GovInfoError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected rejection for granule id {bad!r}")
    # A well-formed id is accepted and stays under out_dir.
    p = granule_paths(out, "CREC-2024-01-09", "CREC-2024-01-09-pt1-PgH5")
    assert str(p["txt"]).startswith(str(out))


def test_backfill_row_has_full_schema(tmp_path: Path = None) -> None:
    out = Path(__file__).resolve().parent / "_tmp_bf"
    pkg = "CREC-2024-01-09"
    gid = "CREC-2024-01-09-pt1-PgH5"
    paths = granule_paths(out, pkg, gid)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    mods = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<mods xmlns="http://www.loc.gov/mods/v3"><extension>'
        "<congress>118</congress><session>2</session></extension>"
        '<relatedItem type="constituent"><extension>'
        "<granuleClass>HOUSE</granuleClass>"
        '<congMember bioGuideId="S000001" party="D" state="CA">'
        '<name type="authority-fnf">Jane Q. Smith</name></congMember>'
        "</extension></relatedItem></mods>"
    )
    paths["txt"].write_text("hello floor\n", encoding="utf-8")
    paths["mods"].write_bytes(mods.encode("utf-8"))
    try:
        row = read_manifest_row(pkg, {"granuleId": gid}, out)
        # Same schema as a fresh download + the backfilled flag.
        for key in (
            "granuleId", "packageId", "granuleClass", "title", "dateIssued",
            "congress", "session", "chamber", "citation", "member_names",
            "bioguide_ids", "char_count", "txt_path", "mods_path",
        ):
            assert key in row, f"missing {key}"
        assert row["backfilled"] is True
        assert row["congress"] == "118"
        assert row["granuleClass"] == "HOUSE"
        assert row["bioguide_ids"] == ["S000001"]
        assert row["char_count"] == len("hello floor\n")
    finally:
        # cleanup
        for f in paths["dir"].glob("*"):
            f.unlink()
        import shutil
        shutil.rmtree(out, ignore_errors=True)


def test_next_month_rolls_over_year() -> None:
    assert next_month(dt.date(2024, 1, 15)) == dt.date(2024, 2, 1)
    assert next_month(dt.date(2024, 12, 31)) == dt.date(2025, 1, 1)


def test_bulk_package_congress_uses_explicit_mods_metadata() -> None:
    assert _package_congress(b"<mods><extension><congress>118</congress></extension></mods>") == 118


def test_bulk_ingest_incremental_runs_are_additive_and_deduplicated() -> None:
    import shutil

    root = Path(__file__).resolve().parent / "_tmp_bulk_incremental"
    bulk = root / "bulk"
    out = root / "interim"
    bulk.mkdir(parents=True, exist_ok=True)

    def make_zip(pkg: str, gid: str, sentence: str) -> None:
        mods = f"""<mods xmlns="http://www.loc.gov/mods/v3">
        <extension><congress>118</congress></extension>
        <relatedItem type="constituent" ID="id-{gid}"><extension>
        <granuleClass>HOUSE</granuleClass>
        <congMember bioGuideId="S000001" party="D" state="CA">
        <name type="authority-fnf">Jane Smith</name></congMember>
        </extension></relatedItem></mods>"""
        html = f"<html><body><pre>Mr. SMITH of California. {sentence}</pre></body></html>"
        with zipfile.ZipFile(bulk / f"{pkg}.zip", "w") as z:
            z.writestr(f"{pkg}/mods.xml", mods)
            z.writestr(f"{pkg}/{gid}.htm", html)

    pkg1, gid1 = "CREC-2024-01-09", "CREC-2024-01-09-pt1-PgH1"
    pkg2, gid2 = "CREC-2024-01-10", "CREC-2024-01-10-pt1-PgH2"
    try:
        make_zip(pkg1, gid1, "First real-source fixture turn.")
        assert run_bulk([pkg1], bulk, out, workers=1) == 1
        make_zip(pkg2, gid2, "Second real-source fixture turn.")
        assert run_bulk([pkg2], bulk, out, workers=1) == 1
        parquet = out / "turns" / "govinfo_bulk_118.parquet"
        assert pq.ParquetFile(parquet).metadata.num_rows == 2
        assert (out / "coverage" / "govinfo_bulk_latest.json").exists()

        make_zip(pkg2, gid2, "Second real-source fixture turn.")
        assert run_bulk([pkg2], bulk, out, workers=1) == 0
        assert pq.ParquetFile(parquet).metadata.num_rows == 2
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_validation_sample_is_blinded_and_real_text_preserved() -> None:
    import shutil

    root = Path(__file__).resolve().parent / "_tmp_validation"
    turns = root / "turns"
    turns.mkdir(parents=True, exist_ok=True)
    passage = "Mr. Speaker, I thank my Republican colleague for working across the aisle."
    row = {
        "turn_id": "crec:fixture#0", "source": "govinfo", "date": "2024-01-09",
        "congress": 118, "chamber": "house", "speaker_name": "Ms. SMITH",
        "speaker_id": "", "bioguide": "S000001", "party": "D", "state": "CA",
        "word_count": len(passage.split()), "is_procedural": False, "text": passage,
    }
    long_text = "My Republican colleagues spoke first. " + ("neutral material " * 100) + "That liar."
    long_row = {
        **row,
        "turn_id": "crec:fixture#1",
        "text": long_text,
        "word_count": len(long_text.split()),
    }
    try:
        pq.write_table(
            pa.Table.from_pylist([row, long_row], schema=ARROW_SCHEMA),
            turns / "govinfo_bulk_118.parquet",
        )
        blinded, hidden = build_validation_sample(turns, root / "validation", 1, 1)
        assert not blinded.empty and not hidden.empty
        assert "sampling_stratum" not in blinded.columns
        assert blinded.iloc[0]["passage"] == passage
        assert hidden["sampling_stratum"].str.contains("cooperation|random").any()
        attack_ids = hidden[
            hidden["sampling_stratum"].str.endswith("personal_attack")
        ]["sample_id"]
        assert blinded[blinded["sample_id"].isin(attack_ids)]["passage"].str.contains("liar").all()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_long_duplicate_turn_fingerprint_ignores_page_markers() -> None:
    base = {
        "bioguide": "S000148",
        "speaker_name": "Mr. SCHUMER",
        "chamber": "senate",
        "is_procedural": False,
    }
    first = {
        **base,
        "text": "opening " + ("substantive remarks " * 100) + "[[Page S4372]] closing",
    }
    second = {
        **base,
        "text": "opening " + ("substantive remarks " * 100) + "closing",
    }
    assert _turn_fingerprint(first) == _turn_fingerprint(second)
    assert _turn_fingerprint({**base, "text": "I yield back."}) == ""


def test_manifest_ingest_rejects_malformed_rows() -> None:
    import shutil

    root = Path(__file__).resolve().parent / "_tmp_bad_manifest"
    root.mkdir(exist_ok=True)
    manifest = root / "manifest.jsonl"
    manifest.write_text("{not json}\\n", encoding="utf-8")
    try:
        try:
            ingest_govinfo(manifest, root, root / "out")
        except ValueError as exc:
            assert "rejected 1 rows" in str(exc)
        else:
            raise AssertionError("malformed manifest should fail strict ingest")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_validation_agreement_uses_comparable_rows_only() -> None:
    import pandas as pd

    a = pd.DataFrame({"sample_id": ["1", "2"], "profanity": ["yes", None]})
    b = pd.DataFrame({"sample_id": ["1", "2"], "profanity": ["yes", "no"]})
    report = validation_report(a, b)
    row = report[report.field.eq("profanity")].iloc[0]
    assert row["n"] == 1
    assert row["raw_agreement"] == 1.0


def test_validation_rejects_missing_sample_ids() -> None:
    import pandas as pd

    a = pd.DataFrame({"sample_id": ["1", "2"], "profanity": ["yes", "no"]})
    b = pd.DataFrame({"sample_id": ["1"], "profanity": ["yes"]})
    try:
        validation_report(a, b)
    except ValueError as exc:
        assert "sample_id mismatch" in str(exc)
    else:
        raise AssertionError("incomplete validation pass should fail")


def test_validation_rejects_reused_ids_for_different_passages() -> None:
    import pandas as pd

    common = {"sample_id": ["VAL-X"], "turn_id": ["turn-1"], "profanity": ["no"]}
    a = pd.DataFrame({**common, "passage_sha256": ["a" * 64]})
    b = pd.DataFrame({**common, "passage_sha256": ["b" * 64]})
    try:
        validation_report(a, b)
    except ValueError as exc:
        assert "validation identity mismatch" in str(exc)
    else:
        raise AssertionError("reused sample IDs must not join different passages")


def test_annotation_pass_rejects_invalid_values() -> None:
    import pandas as pd
    import shutil

    root = Path(__file__).resolve().parent / "_tmp_annotations"
    root.mkdir(exist_ok=True)
    row = {
        "sample_id": "VAL-0001",
        "turn_id": "crec:test#1",
        "passage_sha256": "0" * 64,
        **{field: "no" for field in ANNOTATION_FIELDS},
    }
    row["target_party"] = "none"
    row["confidence"] = "high"
    row["rationale"] = "No coded signal appears."
    row["profanity"] = "maybe"
    pd.DataFrame([row]).to_csv(root / "batch_001.csv", index=False)
    try:
        try:
            read_annotation_pass(root)
        except ValueError as exc:
            assert "invalid profanity values" in str(exc)
        else:
            raise AssertionError("invalid annotation vocabulary should fail")
    finally:
        shutil.rmtree(root, ignore_errors=True)


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
