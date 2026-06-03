"""Text extraction with page/paragraph provenance (F7).

Extraction cascade for PDF:
  1. pdfplumber (MIT, geometry-based) — fast, handles digital PDFs and tables
  2. OCR via pytesseract + pdf2image — fallback for scanned / image-only pages
  3. Anthropic Claude API — last-resort fallback when OCR is unavailable or sparse

Non-PDF content is decoded as plain text (UTF-8 with replacement).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# chars/page below which pdfplumber output is considered "scanned/image PDF"
# threshold=0.4 (default) → cutoff = 0.4 * 250 = 100 chars/page
_CHARS_PER_PAGE_SCALE = 250


@dataclass
class Segment:
    page_number: int | None
    paragraph_index: int
    text: str


def _split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n")]
    return [p for p in parts if p]


# ── Public entry points ───────────────────────────────────────────────────────

def extract_segments(data: bytes, content_type: str) -> list[Segment]:
    """Synchronous extraction (pdfplumber only). Used by tests and simple callers."""
    if content_type == "application/pdf" or _looks_like_pdf(data):
        return _extract_pdf(data)
    return _extract_text(data)


async def extract_segments_with_fallback(
    data: bytes,
    content_type: str,
    *,
    anthropic_api_key: str = "",
    threshold: float = 0.4,
) -> list[Segment]:
    """Async extraction with progressive fallback (I2).

    Cascade (PDF only):
      1. pdfplumber via asyncio.to_thread
      2. OCR (pytesseract + pdf2image) if pdfplumber yields sparse text
      3. Anthropic Claude API if OCR unavailable or still sparse
    """
    segments = await asyncio.to_thread(extract_segments, data, content_type)

    if content_type != "application/pdf" and not _looks_like_pdf(data):
        return segments

    if not _is_sparse(segments, data, threshold):
        return segments

    log.info("pdfplumber sparse — trying OCR")
    ocr_segments = await asyncio.to_thread(_ocr_pages, data)
    if ocr_segments and not _is_sparse(ocr_segments, data, threshold * 0.5):
        return ocr_segments

    if anthropic_api_key:
        log.info("OCR insufficient — falling back to Anthropic Claude")
        claude_segments = await _extract_with_claude(data, anthropic_api_key)
        if claude_segments:
            return claude_segments

    return ocr_segments or segments


# ── Coverage helpers ──────────────────────────────────────────────────────────

def _looks_like_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _is_sparse(segments: list[Segment], data: bytes, threshold: float) -> bool:
    """True if extracted text is suspiciously short (likely a scanned/image PDF)."""
    if not segments:
        return True
    total_chars = sum(len(s.text) for s in segments)
    page_count = _pdf_page_count(data)
    return (total_chars / page_count) < (threshold * _CHARS_PER_PAGE_SCALE)


def _pdf_page_count(data: bytes) -> int:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return max(len(pdf.pages), 1)
    except Exception:
        return 1


# ── OCR path ─────────────────────────────────────────────────────────────────

def _ocr_pages(data: bytes) -> list[Segment]:
    """Extract text via tesseract OCR. Returns [] if system deps are missing."""
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
    except ImportError:
        log.warning("pdf2image or pytesseract not installed; OCR skipped")
        return []

    try:
        images = convert_from_bytes(data)
    except Exception as exc:
        log.warning("pdf2image conversion failed: %s", exc)
        return []

    segments: list[Segment] = []
    for page_no, img in enumerate(images, start=1):
        try:
            text = pytesseract.image_to_string(img)
        except Exception as exc:
            log.warning("pytesseract failed on page %d: %s", page_no, exc)
            continue
        for i, para in enumerate(_split_paragraphs(text)):
            segments.append(Segment(page_number=page_no, paragraph_index=i, text=para))
    return segments


# ── Anthropic API path ────────────────────────────────────────────────────────

async def _extract_with_claude(data: bytes, api_key: str) -> list[Segment]:
    """Send PDF to Claude claude-haiku-4-5 for text extraction. Returns [] on any failure."""
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed; Claude fallback skipped")
        return []

    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": base64.standard_b64encode(data).decode(),
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "Extract all text from this document. "
                                "Separate pages with the marker '---PAGE N---' where N is the page number. "
                                "Separate paragraphs with blank lines. "
                                "Return only extracted text — no commentary."
                            ),
                        },
                    ],
                }
            ],
        )
        raw = response.content[0].text
        return _parse_claude_pages(raw)
    except Exception as exc:
        log.warning("Anthropic extraction failed: %s", exc)
        return []


def _parse_claude_pages(text: str) -> list[Segment]:
    """Parse Claude's '---PAGE N---' format into Segment list."""
    segments: list[Segment] = []
    current_page = 1
    current_lines: list[str] = []

    for line in text.splitlines():
        m = re.match(r"^---PAGE\s+(\d+)---\s*$", line.strip())
        if m:
            _flush_block(current_lines, current_page, segments)
            current_lines = []
            current_page = int(m.group(1))
        else:
            current_lines.append(line)

    _flush_block(current_lines, current_page, segments)
    return segments


def _flush_block(lines: list[str], page_no: int, out: list[Segment]) -> None:
    for i, para in enumerate(_split_paragraphs("\n".join(lines))):
        out.append(Segment(page_number=page_no, paragraph_index=i, text=para))


# ── pdfplumber helpers ────────────────────────────────────────────────────────

def _extract_text(data: bytes) -> list[Segment]:
    text = data.decode("utf-8", errors="replace")
    return [
        Segment(page_number=1, paragraph_index=i, text=p)
        for i, p in enumerate(_split_paragraphs(text))
    ]


def _real_table_bboxes(page) -> list:
    """Return bounding boxes only for real data tables (≥2 rows × ≥2 columns).
    Flowchart boxes and decorative frames are excluded so their text still
    surfaces in the regular text pass."""
    bboxes = []
    for t in page.find_tables():
        cells = t.extract()
        non_empty = [r for r in cells if any(str(c or "").strip() for c in r)]
        if len(non_empty) >= 2 and max((len(r) for r in non_empty), default=0) >= 2:
            bboxes.append(t.bbox)
    return bboxes


def _page_text_excluding_tables(page) -> str:
    """Extract page text with real table areas filtered out to avoid duplication."""
    table_bboxes = _real_table_bboxes(page)
    if not table_bboxes:
        return page.extract_text() or ""

    def _not_in_table(obj) -> bool:
        mid_x = (obj.get("x0", 0) + obj.get("x1", 0)) / 2
        mid_y = (obj.get("top", 0) + obj.get("bottom", 0)) / 2
        return not any(
            bbox[0] <= mid_x <= bbox[2] and bbox[1] <= mid_y <= bbox[3]
            for bbox in table_bboxes
        )

    return page.filter(_not_in_table).extract_text() or ""


def _table_to_text(table: list[list]) -> str:
    """Convert a pdfplumber table to pipe-separated text.

    Returns empty string for degenerate 'tables' (flowchart boxes, decorative
    frames). Heuristic: real tables have ≥2 rows and ≥2 columns.
    """
    if not table:
        return ""
    non_empty = [r for r in table if any(str(c or "").strip() for c in r)]
    if len(non_empty) < 2 or max((len(r) for r in non_empty), default=0) < 2:
        return ""
    rows = []
    for row in non_empty:
        cells = [" ".join(str(cell or "").split()) for cell in row]
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def _extract_pdf(data: bytes) -> list[Segment]:
    import pdfplumber

    segments: list[Segment] = []

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            para_idx = 0

            for para in _split_paragraphs(_page_text_excluding_tables(page)):
                segments.append(Segment(page_number=page_no, paragraph_index=para_idx, text=para))
                para_idx += 1

            for table in page.extract_tables():
                table_text = _table_to_text(table)
                if table_text:
                    segments.append(Segment(page_number=page_no, paragraph_index=para_idx, text=table_text))
                    para_idx += 1

    return segments
