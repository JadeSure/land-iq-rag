"""End-to-end acceptance tests mapped to PRD-RAG section 11 + functional reqs.

Each test runs the full app (API + worker + pgvector) against a fresh schema.
"""

from __future__ import annotations

from conftest import upload, wait_terminal

NSW = (
    "Front setback requirement is 6 metres for this lot. "
    "The floor space ratio (FSR) limit is 0.5:1. "
    "Estimated development cost is 4.2 million dollars."
)
OTHER = (
    "Heritage conservation overlay applies to the northern boundary. "
    "Maximum building height is 8.5 metres under the LEP."
)


async def test_ingest_async_then_query_with_provenance(client):
    """F1,F2,F3,F4,F5,F6,F7,F12,F13 + acceptance: cited, ordered chunks."""
    r = await upload(client, "addr-nsw-1", text=NSW)
    assert r.status_code == 202
    handle = r.json()
    assert handle["state"] == "pending"  # returned before embedding completes (F13)

    final = await wait_terminal(client, handle["task_id"])
    assert final["state"] == "done"
    assert final["chunk_count"] >= 1
    assert final["embedding_count"] == final["chunk_count"]

    q = await client.post("/addresses/addr-nsw-1/query", json={"query": "front setback", "k": 5})
    assert q.status_code == 200
    body = q.json()
    assert body["chunks_returned"] >= 1
    top = body["results"][0]
    assert "setback" in top["text"].lower()
    cit = top["citation"]
    assert cit["page_number"] == 1 and cit["ordinal"] is not None
    assert cit["storage_ref"].startswith("file://")
    assert body["candidates_examined"] >= 1  # observability (8.3)


async def test_address_isolation_no_cross_leak(client):
    """Acceptance 11: a query for one address never returns another's chunks."""
    a = await upload(client, "addr-A", text=NSW)
    b = await upload(client, "addr-B", text=OTHER)
    await wait_terminal(client, a.json()["task_id"])
    await wait_terminal(client, b.json()["task_id"])

    qa = (await client.post("/addresses/addr-A/query", json={"query": "heritage height"})).json()
    qb = (await client.post("/addresses/addr-B/query", json={"query": "front setback"})).json()

    for item in qa["results"]:
        assert "heritage" not in item["text"].lower()  # B's content must not appear under A
    for item in qb["results"]:
        assert "setback" not in item["text"].lower()


async def test_idempotency_unchanged_is_skipped(client):
    """F10: re-ingesting unchanged content does no work and adds no duplicates."""
    first = await upload(client, "addr-idem", text=NSW, upload_id="u1")
    await wait_terminal(client, first.json()["task_id"])

    again = await upload(client, "addr-idem", text=NSW, upload_id="u1")
    assert again.json()["state"] == "skipped"

    status = (await client.get("/addresses/addr-idem/ingestion-status")).json()
    assert status["document_count"] == 1
    assert status["fully_indexed"] is True


async def test_idempotency_changed_replaces_no_orphans(client):
    """F10: changed content replaces the prior version; old chunks are gone."""
    first = await upload(client, "addr-chg", text=NSW, upload_id="u1")
    await wait_terminal(client, first.json()["task_id"])

    changed = await upload(
        client, "addr-chg", text="The bushfire attack level is BAL-29 for this parcel.",
        upload_id="u1",
    )
    final = await wait_terminal(client, changed.json()["task_id"])
    assert final["state"] == "done"
    assert final["doc_version"] == 2

    q = (await client.post("/addresses/addr-chg/query", json={"query": "bushfire"})).json()
    assert q["chunks_returned"] >= 1
    for item in q["results"]:
        assert "setback" not in item["text"].lower()  # old version must be gone

    status = (await client.get("/addresses/addr-chg/ingestion-status")).json()
    assert status["document_count"] == 1
    assert status["fully_indexed"] is True


async def test_removal_makes_chunks_unretrievable(client):
    """F11: after removal, no chunk from the document is returned."""
    up = await upload(client, "addr-del", text=NSW)
    await wait_terminal(client, up.json()["task_id"])
    document_id = up.json()["document_id"]

    d = await client.delete(f"/documents/{document_id}")
    assert d.status_code == 200 and d.json()["deleted"] is True

    q = (await client.post("/addresses/addr-del/query", json={"query": "front setback"})).json()
    assert q["chunks_returned"] == 0


async def test_removal_deletes_stored_bytes(client):
    """F11: removal must not leak the raw document in storage (orphaned object)."""
    from pathlib import Path
    from urllib.parse import urlparse

    up = await upload(client, "addr-del-bytes", text=NSW, upload_id="u1")
    final = await wait_terminal(client, up.json()["task_id"])
    assert final["state"] == "done"

    # Resolve the stored object from the citation's storage_ref (file:// locally).
    q = (await client.post("/addresses/addr-del-bytes/query", json={"query": "front setback"})).json()
    storage_ref = q["results"][0]["citation"]["storage_ref"]
    path = Path(urlparse(storage_ref).path)
    assert path.exists()  # bytes are present before removal

    d = await client.delete(f"/documents/{up.json()['document_id']}")
    assert d.status_code == 200 and d.json()["deleted"] is True
    assert not path.exists()  # bytes are gone after removal — no orphan


async def test_model_switch_and_rebuild(client):
    """F8,F9,F14,8.5: switch model -> rebuild -> active flips, index consistent."""
    up = await upload(client, "addr-model", text=NSW)
    await wait_terminal(client, up.json()["task_id"])

    assert (await client.get("/config/embedding-model")).json()["active_model"] == "hash:feature-256"

    # A different dimension forces a different per-model table (cannot mix; F14).
    put = await client.put("/config/embedding-model", json={"model_id": "hash:feature-128"})
    assert put.status_code == 200
    rebuild_task = put.json()["rebuild_task_id"]
    await wait_terminal(client, rebuild_task)

    assert (await client.get("/config/embedding-model")).json()["active_model"] == "hash:feature-128"

    q = await client.post("/addresses/addr-model/query", json={"query": "front setback"})
    assert q.status_code == 200
    body = q.json()
    assert body["model_id"] == "hash:feature-128"  # query uses the new model only
    assert body["chunks_returned"] >= 1


async def test_failure_isolation(client):
    """6.5: one document failing does not block siblings; failure is reportable."""
    good = await upload(client, "addr-fail", text=NSW, upload_id="good")
    bad = await upload(
        client, "addr-fail", data=b"this is not a real pdf", content_type="application/pdf",
        filename="broken.pdf", upload_id="bad",
    )
    good_final = await wait_terminal(client, good.json()["task_id"])
    bad_final = await wait_terminal(client, bad.json()["task_id"])

    assert good_final["state"] == "done"
    assert bad_final["state"] == "failed"
    assert bad_final["error_type"]  # no silent failure
    assert bad_final["failed_step"] == "extract"

    # The good document is still queryable despite the sibling failure.
    q = (await client.post("/addresses/addr-fail/query", json={"query": "front setback"})).json()
    assert q["chunks_returned"] >= 1


async def test_graceful_degradation_no_documents(client):
    """6.6: an address with no documents is not an error; query returns empty."""
    status = (await client.get("/addresses/addr-empty/ingestion-status")).json()
    assert status["has_documents"] is False

    q = await client.post("/addresses/addr-empty/query", json={"query": "anything"})
    assert q.status_code == 200
    assert q.json()["chunks_returned"] == 0
