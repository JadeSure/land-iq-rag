"""SQS -> Lambda ingestion/rebuild worker.

Runs the same transport-agnostic `process_job` the local poll loop runs. Uses a
persistent event loop + connection pool across warm invocations (a pool is bound
to the loop that opened it, so a fresh asyncio.run per call would break reuse).
Returns partial-batch failures so only genuinely-retryable messages are redelivered
(enable ReportBatchItemFailures on the event source mapping).
"""

from __future__ import annotations

import asyncio
import json
import os

os.environ.setdefault("RAG_PROFILE", "aws")
os.environ.setdefault("RAG_WORKER_ENABLED", "false")
os.environ.setdefault("RAG_RUN_MIGRATIONS_ON_STARTUP", "false")

import structlog  # noqa: E402

from ..config import get_settings  # noqa: E402
from ..context import AppContext  # noqa: E402
from ..ingest.runner import process_job  # noqa: E402
from ..store.db import make_pool  # noqa: E402
from ..store.files import make_storage  # noqa: E402
from ..store.queue import make_queue  # noqa: E402
from ..store.tasks import claim_by_id  # noqa: E402
from .ssm_config import hydrate_settings_from_ssm  # noqa: E402

log = structlog.get_logger()

_loop: asyncio.AbstractEventLoop | None = None
_ctx: AppContext | None = None


async def _get_ctx() -> AppContext:
    global _ctx
    if _ctx is None:
        settings = hydrate_settings_from_ssm(get_settings())
        pool = await make_pool(settings.database_url, min_size=1, max_size=2)
        _ctx = AppContext(
            settings=settings,
            pool=pool,
            storage=make_storage(settings),
            queue=make_queue(settings, pool),
        )
    return _ctx


async def _process_record(ctx: AppContext, record: dict) -> None:
    body = json.loads(record["body"])
    task_id = body["task_id"]
    job = await claim_by_id(
        ctx.pool, task_id, worker_id="lambda", lease_seconds=ctx.settings.lease_seconds
    )
    if job is None:
        return  # already terminal (idempotent under SQS at-least-once)
    await process_job(ctx, job)  # raises only when a retry is wanted


def handler(event: dict, context) -> dict:
    global _loop
    if _loop is None:
        _loop = asyncio.new_event_loop()
    ctx = _loop.run_until_complete(_get_ctx())
    failures: list[dict] = []
    for record in event.get("Records", []):
        try:
            _loop.run_until_complete(_process_record(ctx, record))
        except Exception:  # noqa: BLE001
            log.exception("record.failed", message_id=record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
