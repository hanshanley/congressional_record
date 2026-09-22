"""Column-aware fallback for text-based GovInfo daily Record section PDFs."""

from __future__ import annotations

from dataclasses import dataclass
import io
import logging
import re
import unicodedata
import zipfile
from typing import Iterator

import pdfplumber

from analysis.ingest.govinfo import (
    _SPEAKER_RE,
    _split_inserted_material,
    build_turns,
)
from analysis.ingest.legislators import members_on
from analysis.ingest.schema import normalize_chamber


LOG = logging.getLogger("analysis.ingest.govinfo_pdf")
_COLUMNS = ((40, 219), (219, 396), (396, 572))
_GUTTERS = ((218, 221), (395, 398))
_TIME_MARKER = re.compile(r"^(?:b\s+\d{4}|\u2211)$")
_HEADING = re.compile(r"^[A-Z0-9][A-Z0-9 .,'\u2019\u2014\u2013()/-]{5,}$")
_ROLLCALL = re.compile(r"^\[(?:Roll No\.|Rollcall Vote No\.)\s*\d+", re.I)
_HYPHENATED_WORD = re.compile(r"\b[A-Za-z]+(?:-[A-Za-z]+)+\b")


@dataclass
class _Paragraph:
    text: str
    direct: bool = False
    printed: bool = False
    kind: str = "text"


def _plain(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(char)
    )


def _line_style(line: dict, left: float) -> tuple[bool, bool]:
    sizes = [char["size"] for char in line["chars"] if char["text"].strip()]
    size = max(sizes, default=0)
    # Floor text is 8 pt; smallcaps can be smaller within the same line.
    floor = 7.8 <= size <= 8.2
    direct = floor and 9 <= line["x0"] - left <= 15
    return direct, size < 7.8


def _paragraphs(data: bytes, chamber: str, label: str) -> list[_Paragraph]:
    paragraphs: list[_Paragraph] = []
    current: list[str] = []
    direct = printed = False

    def flush() -> None:
        if current:
            paragraphs.append(_Paragraph("\n".join(current), direct, printed))
            current.clear()

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        if not pdf.pages:
            raise ValueError(f"{label}: empty PDF")
        for page_number, page in enumerate(pdf.pages, 1):
            try:
                if (round(page.width), round(page.height)) != (612, 792):
                    raise ValueError(f"{label}: unsupported PDF page size on page {page_number}")
                if not page.chars:
                    raise ValueError(
                        f"{label}: page {page_number} has no extractable text; OCR is required"
                    )
                if page_number == 1:
                    top, bottom = (70, 710) if chamber == "extensions" else (200, 670)
                else:
                    top, bottom = 34, 750
                crossing = any(
                    sum(
                        bool(char["text"].strip())
                        and top <= char["top"] and char["bottom"] <= bottom
                        and char["x0"] < right and char["x1"] > left
                        for char in page.chars
                    ) >= 3
                    for left, right in _GUTTERS
                )
                lines_by_column = [
                    page.within_bbox((left, top, right, bottom)).extract_text_lines(
                        x_tolerance=1, y_tolerance=3, return_chars=True, strip=True,
                    )
                    for left, right in _COLUMNS
                ]
                if crossing:
                    flush()
                    # A printed table may span the gutters. Never discard a page
                    # that could also contain a direct floor speaker.
                    for (left, _), lines in zip(_COLUMNS, lines_by_column):
                        for line in lines:
                            is_direct, _ = _line_style(line, left)
                            if chamber in {"house", "senate"} and is_direct and re.match(
                                r"^(?:(?:Mr|Mrs|Ms|Miss)\.|The (?:SPEAKER|PRESIDING))\s",
                                line["text"],
                            ):
                                raise ValueError(
                                    f"{label}: mixed floor/table layout on page {page_number}"
                                )
                    paragraphs.append(_Paragraph(
                        f"{label}: non-columnar layout on page {page_number}", kind="layout",
                    ))
                    continue
                for (left, _), lines in zip(_COLUMNS, lines_by_column):
                    previous_top = None
                    for line in lines:
                        text = _plain(re.sub(r"\s+", " ", line["text"]).strip())
                        if not text or _TIME_MARKER.fullmatch(text):
                            continue
                        if text == "f":
                            flush()
                            paragraphs.append(_Paragraph("", kind="boundary"))
                            continue
                        is_direct, is_printed = _line_style(line, left)
                        heading = bool(_HEADING.fullmatch(text))
                        if heading:
                            flush()
                            paragraphs.append(_Paragraph(text, kind="boundary"))
                            previous_top = line["top"]
                            continue
                        if (
                            line["x0"] - left >= 7
                            or (previous_top is not None and line["top"] - previous_top > 12)
                            or is_printed != printed
                        ):
                            flush()
                        if not current:
                            direct, printed = is_direct, is_printed
                        current.append(text)
                        previous_top = line["top"]
            finally:
                page.close()
    flush()
    if not any(p.text for p in paragraphs if p.kind == "text"):
        raise ValueError(f"{label}: PDF contains no extractable body text")

    compounds = {
        match.group().lower()
        for paragraph in paragraphs
        for match in _HYPHENATED_WORD.finditer(paragraph.text)
    }
    for paragraph in paragraphs:
        paragraph.text = re.sub(
            r"([A-Za-z]+)-\n([A-Za-z]+)",
            lambda match: (
                match[1] + "-" + match[2]
                if (match[1] + "-" + match[2]).lower() in compounds
                else match[1] + match[2]
            ),
            paragraph.text,
        ).replace("\n", " ")
    return paragraphs


def _section_turns(
    data: bytes, gid: str, date: str, congress: int, chamber: str,
) -> Iterator[dict]:
    members = members_on(date, chamber) if chamber in {"house", "senate"} else []
    paragraphs = _paragraphs(data, chamber, gid)
    current: list[str] = []
    attributed = False
    printing = False
    block = 0
    floor_markers = 0

    def flush() -> Iterator[dict]:
        nonlocal block
        if not current:
            return
        for turn in build_turns(
            " ".join(current), members if attributed else [],
            gid, date, congress, chamber,
        ):
            suffix = turn["turn_id"].split("#", 1)[1]
            turn["turn_id"] = f"crec:{gid}#pdf-{block}-{suffix}"
            if not attributed:
                turn["is_procedural"] = True
            yield turn
        block += 1
        current.clear()

    for paragraph in paragraphs:
        if paragraph.kind == "layout":
            yield from flush()
            if not printing and chamber in {"house", "senate"}:
                raise ValueError(paragraph.text)
            LOG.info("skipping non-speech table: %s", paragraph.text)
            continue
        if paragraph.kind == "boundary":
            yield from flush()
            attributed = False
            if paragraph.text:
                current.append(paragraph.text)
            continue
        marker = _SPEAKER_RE.match(paragraph.text + " ")
        if paragraph.direct and marker and chamber in {"house", "senate"}:
            yield from flush()
            attributed = True
            printing = False
            floor_markers += 1
        elif paragraph.printed or marker or _ROLLCALL.match(paragraph.text):
            yield from flush()
            attributed = False
            printing = True
        spoken, inserted = _split_inserted_material(paragraph.text)
        if inserted:
            if spoken:
                current.append(spoken)
            yield from flush()
            attributed = False
            printing = True
            current.append(inserted)
        else:
            current.append(paragraph.text)
    yield from flush()
    if chamber in {"house", "senate"} and not floor_markers:
        raise ValueError(f"{gid}: no recognizable floor speaker markers in PDF")


def turns_from_pdfs(
    archive: zipfile.ZipFile, granules: dict, pkg: str, date: str, congress: int,
) -> Iterator[dict]:
    """Read section PDFs once, excluding the duplicate whole-issue PDF and digest."""
    pdfs = {
        name.rsplit("/", 1)[-1][:-4]: name
        for name in archive.namelist() if name.endswith(".pdf")
    }
    sections = 0
    for gid, info in granules.items():
        chamber = normalize_chamber(info.get("granuleClass") or info.get("chamber"))
        if chamber not in {"house", "senate", "extensions"}:
            continue
        if gid != f"{pkg}-{chamber}" or gid not in pdfs:
            raise ValueError(f"{pkg}: missing or unsupported {chamber} section PDF: {gid}")
        sections += 1
        LOG.info("extracting %s PDF", gid)
        yield from _section_turns(archive.read(pdfs[gid]), gid, date, congress, chamber)
    if not sections:
        raise ValueError(f"{pkg}: archive has neither HTML nor supported section PDFs")
