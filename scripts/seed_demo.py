"""Seed demo addresses by uploading the files under data/demo/<address>/ to a
running RAG server, then run a sample query so the demo flow is reproducible.

Usage:
    uv run uvicorn landiq_rag.main:app --port 8099      # in one shell
    RAG_BASE_URL=http://localhost:8099 uv run python scripts/seed_demo.py
"""

from __future__ import annotations

import mimetypes
import os
import pathlib
import sys
import time

import httpx

BASE = os.environ.get("RAG_BASE_URL", "http://localhost:8099")
DEMO_DIR = pathlib.Path(__file__).resolve().parents[1] / "data" / "demo"


def wait_terminal(client: httpx.Client, task_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"{BASE}/ingestion/{task_id}").json()
        if body["state"] in ("done", "skipped", "failed"):
            return body
        time.sleep(0.5)
    raise SystemExit(f"task {task_id} did not finish in {timeout}s")


def main() -> int:
    if not DEMO_DIR.exists():
        print(f"no demo data at {DEMO_DIR}")
        return 1
    with httpx.Client(timeout=60.0) as client:
        for addr_dir in sorted(p for p in DEMO_DIR.iterdir() if p.is_dir()):
            address_id = addr_dir.name
            print(f"\n== {address_id} ==")
            for f in sorted(addr_dir.iterdir()):
                if not f.is_file():
                    continue
                ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                resp = client.post(
                    f"{BASE}/addresses/{address_id}/documents",
                    files={"file": (f.name, f.read_bytes(), ctype)},
                    data={"upload_id": f.name},
                )
                resp.raise_for_status()
                handle = resp.json()
                final = wait_terminal(client, handle["task_id"])
                print(f"  {f.name}: {final['state']} "
                      f"chunks={final['chunk_count']} embeddings={final['embedding_count']}")

            status = client.get(f"{BASE}/addresses/{address_id}/ingestion-status").json()
            print(f"  fully_indexed={status['fully_indexed']} "
                  f"documents={status['document_count']} model={status['active_model']}")

            q = client.post(
                f"{BASE}/addresses/{address_id}/query",
                json={"query": "what is the floor space ratio and height limit?", "k": 3},
            ).json()
            print(f"  sample query -> {q['chunks_returned']} chunks "
                  f"(examined {q['candidates_examined']}, {q['latency_ms']}ms)")
            if q["results"]:
                top = q["results"][0]
                cit = top["citation"]
                print(f"    top: p{cit['page_number']} #{cit['ordinal']} "
                      f"{top['text'][:80]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
