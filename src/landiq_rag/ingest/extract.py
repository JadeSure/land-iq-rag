"""Text extraction with page/paragraph provenance (F7).

Primary backend: pdfplumber (MIT, geometry-based, fast).
Fallback: Claude API PDF vision, triggered when pdfplumber quality is low
(image-heavy or complex layouts). Set RAG_ANTHROPIC_API_KEY to enable;
omitting it disables the fallback silently.

Quality is scored 0–1 from three proxies:
  chars/page   — very low ⟹ image-only PDF
  page_coverage — fraction of pages that yielded any text
  avg_word_len  — very short ⟹ garbled / encoding issues
Below RAG_PDF_FALLBACK_THRESHOLD (default 0.4), the Claude path is tried.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
from dataclasses import dataclass


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
    """Async extraction with Claude fallback when pdfplumber quality is low.

    Falls through to plain extract_segments when:
    - not a PDF, or
    - no anthropic_api_key configured, or
    - quality score ≥ threshold.
    """
    import structlog
    log = structlog.get_logger()

    segments = extract_segments(data, content_type)

    if not (content_type == "application/pdf" or _looks_like_pdf(data)):
        return segments
    if not anthropic_api_key:
        return segments

    page_count = _pdf_page_count(data)
    score = extraction_quality(segments, page_count)
    log.debug("pdf.quality", score=round(score, 3), pages=page_count, segments=len(segments))

    if score >= threshold:
        return segments

    log.info("pdf.claude_fallback", quality=round(score, 3), threshold=threshold)
    claude_segs = await _extract_with_claude(data, anthropic_api_key)
    if claude_segs:
        return claude_segs

    log.warning("pdf.claude_fallback_empty", quality=round(score, 3))
    return segments


# ── Quality scoring ───────────────────────────────────────────────────────────

def extraction_quality(segments: list[Segment], page_count: int) -> float:
    """Score pdfplumber output 0.0 (empty/garbled) → 1.0 (good).

    Three weighted proxies:
      50% page_coverage  — fraction of pages that produced ≥1 segment
      30% char_score     — avg chars/page normalised at 500
      20% word_score     — avg word length (≤2 = garbled, ≥5 = normal)
    """
    if page_count == 0:
        return 0.0
    if not segments:
        return 0.0

    covered = len({s.page_number for s in segments if s.page_number is not None})
    page_coverage = covered / page_count

    total_chars = sum(len(s.text.strip()) for s in segments)
    char_score = min(total_chars / (page_count * 500), 1.0)

    all_words = " ".join(s.text for s in segments).split()
    if all_words:
        avg_word = sum(len(w) for w in all_words) / len(all_words)
        word_score = min(max(avg_word - 2.0, 0.0) / 3.0, 1.0)
    else:
        word_score = 0.0

    return 0.5 * page_coverage + 0.3 * char_score + 0.2 * word_score


def _pdf_page_count(data: bytes) -> int:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return len(pdf.pages)


# ── Claude API fallback ───────────────────────────────────────────────────────

async def _extract_with_claude(data: bytes, api_key: str) -> list[Segment]:
    """Send PDF to Claude vision; parse page-by-page text response."""
    try:
        import anthropic
    except ImportError:
        return []

    client = anthropic.Anthropic(api_key=api_key)
    encoded = base64.standard_b64encode(data).decode("utf-8")

    def _call() -> str:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": encoded,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Extract all text from this PDF exactly as it appears. "
                            "Output each page using this format:\n"
                            "PAGE 1\n{full page text}\n\nPAGE 2\n{full page text}\n\n...\n"
                            "Render tables as markdown tables. "
                            "Do not summarise, interpret, or omit any content."
                        ),
                    },
                ],
            }],
        )
        return resp.content[0].text

    try:
        raw = await asyncio.to_thread(_call)
    except Exception:  # noqa: BLE001
        return []

    return _parse_claude_pages(raw)


def _parse_claude_pages(text: str) -> list[Segment]:
    """Parse 'PAGE N\\n{content}' blocks back into Segments."""
    segments: list[Segment] = []
    parts = re.split(r'(?:^|\n)PAGE\s+(\d+)[:\n]?', text.strip(), flags=re.IGNORECASE)
    # parts: ['preamble', '1', 'content1', '2', 'content2', ...]
    i = 1
    while i + 1 < len(parts):
        page_no = int(parts[i])
        content = parts[i + 1].strip()
        for para_idx, para in enumerate(_split_paragraphs(content)):
            segments.append(Segment(page_number=page_no, paragraph_index=para_idx, text=para))
        i += 2
    return segments


# ── pdfplumber helpers ────────────────────────────────────────────────────────

def _looks_like_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


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
    ocr_candidates: list[int] = []

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            para_idx = 0
            page_had_content = False

            for para in _split_paragraphs(_page_text_excluding_tables(page)):
                segments.append(Segment(page_number=page_no, paragraph_index=para_idx, text=para))
                para_idx += 1
                page_had_content = True

            for table in page.extract_tables():
                table_text = _table_to_text(table)
                if table_text:
                    segments.append(Segment(page_number=page_no, paragraph_index=para_idx, text=table_text))
                    para_idx += 1
                    page_had_content = True

            if not page_had_content:
                ocr_candidates.append(page_no)

    if ocr_candidates:
        segments.extend(_ocr_pages(data, ocr_candidates))

    return segments


# ── OCR fallback (image-only pages) ──────────────────────────────────────────

def _ocr_pages(data: bytes, page_numbers: list[int]) -> list[Segment]:
    """OCR image-only pages. Silently returns [] if pdf2image/pytesseract not installed."""
    if not page_numbers:
        return []
    try:
        from pdf2image import convert_from_bytes  # type: ignore[import-untyped]
        import pytesseract  # type: ignore[import-untyped]
    except ImportError:
        return []

    first, last = min(page_numbers), max(page_numbers)
    needed = set(page_numbers)
    try:
        images = convert_from_bytes(data, first_page=first, last_page=last)
    except Exception:  # noqa: BLE001
        return []

    segments: list[Segment] = []
    for img_idx, image in enumerate(images):
        page_no = first + img_idx
        if page_no not in needed:
            continue
        text = pytesseract.image_to_string(image)
        for para_idx, para in enumerate(_split_paragraphs(text)):
            segments.append(Segment(page_number=page_no, paragraph_index=para_idx, text=para))
    return segments
