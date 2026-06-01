"""Determinism + provenance of chunking (no DB). PRD 8.4, F7."""

from __future__ import annotations

from landiq_rag.embedding.providers import FeatureHashEmbeddingProvider
from landiq_rag.ingest.chunk import chunk_segments
from landiq_rag.ingest.extract import Segment, extract_segments


def _segments():
    return [
        Segment(page_number=1, paragraph_index=0, text="The minimum front setback is 6 metres."),
        Segment(page_number=1, paragraph_index=1, text="The floor space ratio limit is 0.5:1."),
        Segment(page_number=2, paragraph_index=0, text="Development cost is estimated at 4.2M."),
    ]


def test_chunking_is_deterministic():
    a = chunk_segments(_segments(), chunk_tokens=20, overlap=5)
    b = chunk_segments(_segments(), chunk_tokens=20, overlap=5)
    assert [c.text for c in a] == [c.text for c in b]
    assert [(c.page_number, c.paragraph_index) for c in a] == [
        (c.page_number, c.paragraph_index) for c in b
    ]


def test_chunks_carry_provenance():
    chunks = chunk_segments(_segments(), chunk_tokens=10, overlap=2)
    assert chunks
    for c in chunks:
        assert c.page_number in (1, 2)
        assert c.paragraph_index is not None
        assert c.char_start is not None and c.char_end >= c.char_start


def test_overlap_must_be_smaller_than_chunk():
    try:
        chunk_segments(_segments(), chunk_tokens=5, overlap=5)
    except ValueError:
        return
    raise AssertionError("expected ValueError when overlap >= chunk_tokens")


async def test_feature_hash_is_deterministic_and_lexical():
    p = FeatureHashEmbeddingProvider(dim=64)
    v1 = await p.embed_query("front setback requirement")
    v2 = await p.embed_query("front setback requirement")
    assert v1.vectors[0] == v2.vectors[0]  # deterministic
    assert len(v1.vectors[0]) == 64


def test_plain_text_extraction():
    segs = extract_segments(b"para one here.\n\npara two here.", "text/plain")
    assert len(segs) == 2
    assert segs[0].page_number == 1
