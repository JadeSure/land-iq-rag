"""Text extraction with page/paragraph provenance (F7).

PDF via pdfplumber (page-level text, split into paragraphs). Plain text is also
supported so the pipeline and tests can run without crafting PDFs. Image-only /
scanned PDFs are a non-goal (PRD 2.3): pages yielding no text are skipped.
"""

from __future__ import annotations

import io
from dataclasses import dataclass


@dataclass
class Segment:
    page_number: int | None
    paragraph_index: int
    text: str


def _split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n")]
    return [p for p in parts if p]


def extract_segments(data: bytes, content_type: str) -> list[Segment]:
    if content_type == "application/pdf" or _looks_like_pdf(data):
        return _extract_pdf(data)
    return _extract_text(data)


def _looks_like_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _extract_text(data: bytes) -> list[Segment]:
    text = data.decode("utf-8", errors="replace")
    return [
        Segment(page_number=1, paragraph_index=i, text=p)
        for i, p in enumerate(_split_paragraphs(text))
    ]


def _extract_pdf(data: bytes) -> list[Segment]:
    import pdfplumber

    segments: list[Segment] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            for para_idx, para in enumerate(_split_paragraphs(text)):
                segments.append(Segment(page_number=page_no, paragraph_index=para_idx, text=para))
    return segments
