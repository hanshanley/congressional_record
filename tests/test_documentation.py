"""Rendering and link checks for repository Markdown."""

from __future__ import annotations

import json
from pathlib import Path
import re
import runpy
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
import pytest


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "data/validation/adjudication/ADJUDICATION_GUIDE.md"
DOCUMENTS = sorted([
    ROOT / "README.md",
    *ROOT.glob("docs/*.md"),
    GUIDE,
])
MARKDOWN = MarkdownIt("commonmark", {"html": True}).enable("table")


def _render(path: Path):
    return BeautifulSoup(MARKDOWN.render(path.read_text()), "html.parser")


def _anchors(document):
    anchors = {node["id"] for node in document.select("[id]")}
    for heading in document.find_all(re.compile(r"^h[1-6]$")):
        slug = re.sub(r"[^\w -]", "", heading.get_text().lower()).replace(" ", "-")
        anchor = slug
        suffix = 0
        while anchor in anchors:
            suffix += 1
            anchor = f"{slug}-{suffix}"
        anchors.add(anchor)
    return anchors


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_markdown_renders_with_valid_structure_and_local_links(path):
    document = _render(path)
    assert len(document.find_all("h1")) == 1
    assert "=" * 10 not in document.h1.get_text()
    for table in document.find_all("table"):
        columns = len(table.select("thead th"))
        assert columns > 0
        assert all(len(row.find_all("td", recursive=False)) == columns
                   for row in table.select("tbody tr"))
    lines = path.read_text().splitlines()
    for token in MARKDOWN.parse(path.read_text()):
        if token.type == "fence":
            closing = lines[token.map[1] - 1]
            assert re.fullmatch(r"\s*" + re.escape(token.markup[0])
                               + "{" + str(len(token.markup)) + r",}\s*", closing), (
                f"{path.relative_to(ROOT)}: unclosed code fence at line {token.map[0] + 1}"
            )
    for node in document.find_all(["a", "img"]):
        url = node.get("href", node.get("src", ""))
        parts = urlsplit(url)
        if not url or parts.scheme or parts.netloc:
            continue
        target = (path.parent / unquote(parts.path)).resolve() if parts.path else path
        assert target.exists(), f"{path.relative_to(ROOT)}: missing target {url}"
        if parts.fragment and target.suffix == ".md":
            assert unquote(parts.fragment) in _anchors(_render(target)), (
                f"{path.relative_to(ROOT)}: missing anchor {url}"
            )


def test_adjudication_guide_renders_original_fields_and_allowed_values():
    document = _render(GUIDE)
    validator = runpy.run_path(str(GUIDE.with_name("validate_result.py")))
    allowed = validator["ALLOWED"]
    rows = {
        row.find_all("td")[0].get_text(): row.find_all("td")
        for row in document.select("table tbody tr")
    }
    assert set(rows) == {*allowed, "rationale"}
    for name, values in allowed.items():
        assert {code.get_text() for code in rows[name][1].find_all("code")} == values
    fields = json.loads(document.select_one("pre code.language-json").get_text())
    assert fields == ["sample_id", *allowed, "rationale"]
    assert len(document.find_all("h2")) == 5


def test_readme_has_no_uncensored_profanity_or_slurs():
    from analysis.score.scorers import Scorers

    document = _render(ROOT / "README.md")
    visible = document.get_text(" ", strip=True)
    visible += " " + " ".join(image.get("alt", "") for image in document.find_all("img"))
    counts = Scorers().score_turn(visible)
    assert counts["profanity_hits"] == 0
    assert counts["slur_hits"] == 0
