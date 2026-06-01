"""Token-aware chunking that carries provenance through.

Deterministic under fixed (content, chunk_tokens, overlap): the same inputs yield
identical chunks, hence equivalent retrieval (PRD 8.4). Each chunk records the
page/paragraph it begins in and its char span for human verification (F7).
"""

from __future__ import annotations

import bisect
import functools

import tiktoken

from ..store.documents import ChunkRow
from .extract import Segment

_SEPARATOR = "\n\n"


@functools.lru_cache(maxsize=1)
def _encoding():
    # text-embedding-3-* use cl100k_base; adequate for token sizing of any provider.
    return tiktoken.get_encoding("cl100k_base")


def chunk_segments(segments: list[Segment], *, chunk_tokens: int, overlap: int) -> list[ChunkRow]:
    if not segments:
        return []
    if overlap >= chunk_tokens:
        raise ValueError("chunk_overlap must be smaller than chunk_tokens")

    # Reconstruct the document text and record where each segment starts, so a char
    # offset can be mapped back to (page, paragraph).
    pieces: list[str] = []
    starts: list[int] = []
    provenance: list[tuple[int | None, int]] = []
    cursor = 0
    for seg in segments:
        if pieces:
            cursor += len(_SEPARATOR)
        starts.append(cursor)
        provenance.append((seg.page_number, seg.paragraph_index))
        pieces.append(seg.text)
        cursor += len(seg.text)
    full_text = _SEPARATOR.join(pieces)

    enc = _encoding()
    tokens = enc.encode(full_text)

    def locate(char_pos: int) -> tuple[int | None, int]:
        i = bisect.bisect_right(starts, char_pos) - 1
        i = max(0, i)
        return provenance[i]

    chunks: list[ChunkRow] = []
    start = 0
    ordinal = 0
    prefix_len = 0  # len(decode(tokens[:start])), maintained incrementally
    while start < len(tokens):
        end = min(start + chunk_tokens, len(tokens))
        text = enc.decode(tokens[start:end])
        char_start = prefix_len
        char_end = char_start + len(text)
        page, para = locate(char_start)
        chunks.append(
            ChunkRow(
                text=text,
                ordinal=ordinal,
                page_number=page,
                paragraph_index=para,
                char_start=char_start,
                char_end=char_end,
                token_count=end - start,
            )
        )
        ordinal += 1
        if end == len(tokens):
            break
        step = end - overlap
        prefix_len += len(enc.decode(tokens[start:step]))
        start = step
    return chunks
