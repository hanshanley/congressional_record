"""Offline unit tests for CREC parsing (no network / API key required).

Run directly:  python tests/test_parsing.py
Or with pytest: pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crec.download import html_to_text  # noqa: E402
from crec.metadata import parse_mods  # noqa: E402

SAMPLE_HTML = """<html><head><title>Congressional Record</title></head><body><pre>
[Congressional Record Volume 170, Number 4 (Tuesday, January 9, 2024)]
[House]
[Page H5]
From the Congressional Record Online through the GPO [www.gpo.gov]


                              PRAYER

  The Chaplain offered the following prayer:



  Let us pray.
</pre></body></html>"""

SAMPLE_MODS = """<?xml version="1.0" encoding="UTF-8"?>
<mods xmlns="http://www.loc.gov/mods/v3">
  <extension>
    <congress>118</congress>
    <session>2</session>
    <volume>170</volume>
    <issue>4</issue>
  </extension>
  <relatedItem type="constituent">
    <titleInfo><title>PRAYER</title></titleInfo>
    <identifier type="congressional record citation">170 Cong. Rec. H5</identifier>
    <extension>
      <granuleClass>HOUSE</granuleClass>
      <subGranuleClass>PRAYER</subGranuleClass>
      <pagePrefix>H</pagePrefix>
      <congMember bioGuideId="S000001" chamber="H" congress="118" party="D" role="SPEAKING" state="CA">
        <name type="parsed">Smith</name>
        <name type="authority-fnf">Jane Q. Smith</name>
        <name type="authority-lnf">Smith, Jane Q.</name>
      </congMember>
    </extension>
  </relatedItem>
</mods>"""


def test_html_to_text() -> None:
    out = html_to_text(SAMPLE_HTML)
    assert "PRAYER" in out
    assert "Let us pray." in out
    assert "\n\n\n" not in out  # blank lines collapsed
    assert out.endswith("\n")


def test_parse_mods_fields() -> None:
    meta = parse_mods(SAMPLE_MODS.encode("utf-8"))
    assert meta["granuleClass"] == "HOUSE"
    assert meta["congress"] == "118"
    assert meta["session"] == "2"
    assert meta["citation"] == "170 Cong. Rec. H5"
    assert meta["member_names"] == ["Jane Q. Smith"]
    assert meta["bioguide_ids"] == ["S000001"]
    assert meta["members"][0]["party"] == "D"
    assert meta["members"][0]["state"] == "CA"


def test_parse_mods_bad_xml() -> None:
    meta = parse_mods(b"<not valid xml")
    assert "mods_parse_error" in meta


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
