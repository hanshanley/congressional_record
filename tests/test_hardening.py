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
