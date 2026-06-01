"""EventBridge -> Lambda reconciler (production hardening, not a demo blocker).

Re-enqueues jobs stuck in a non-terminal state past a threshold or inserted but
never delivered (the upload-Lambda partial-failure window: row written, SQS send
failed). SQS visibility timeout + the DLQ already cover most failure paths; this
catches the residue. There is no scheduled re-embedding (determinism, 8.4).
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
from ..store.db import make_pool  # noqa: E402
from .ssm_config import hydrate_settings_from_ssm  # noqa: E402

log = structlog.get_logger()

_STUCK_AFTER = "10 minutes"


async def _reconcile() -> int:
    import boto3

    settings = hydrate_settings_from_ssm(get_settings())
    sqs = boto3.client("sqs")
    pool = await make_pool(settings.database_url, min_size=1, max_size=2)
    try:
        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT task_id, job_type FROM ingest_job
                     WHERE state IN ('pending','extracting','chunking','embedding')
                       AND (lease_until IS NULL OR lease_until < now())
                       AND created_at < now() - %s::interval
                    """,
                    (_STUCK_AFTER,),
                )
            ).fetchall()
        for task_id, job_type in rows:
            sqs.send_message(
                QueueUrl=settings.sqs_queue_url,
                MessageBody=json.dumps({"task_id": str(task_id), "job_type": job_type}),
            )
        return len(rows)
    finally:
        await pool.close()


def handler(event: dict, context) -> dict:
    requeued = asyncio.run(_reconcile())
    log.info("reconciler.done", requeued=requeued)
    return {"requeued": requeued}
