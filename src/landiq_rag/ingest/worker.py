"""Local ingestion worker: claim jobs (SKIP LOCKED) and run the shared body.

On AWS there is no such loop; an SQS event source invokes a Lambda that runs the
same `process_job`. Here a process polls the Postgres job table. Retries happen by
re-claiming a job whose state was reset to 'pending'; process_job's retry re-raise
is swallowed so one failing job never stops the loop (PRD 6.5).
"""

from __future__ import annotations

import asyncio
import os

import structlog

from ..context import AppContext
from ..ports import ClaimedJob
from ..store.tasks import PostgresJobQueue
from .runner import process_job

log = structlog.get_logger()


async def _safe(ctx: AppContext, job: ClaimedJob) -> None:
    try:
        await process_job(ctx, job)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001  (already recorded on the row; retry via re-claim)
        pass


async def run_worker(ctx: AppContext) -> None:
    queue = PostgresJobQueue(ctx.pool, lease_seconds=ctx.settings.lease_seconds)
    worker_id = f"worker-{os.getpid()}"
    poll = ctx.settings.worker_poll_seconds
    while True:
        try:
            jobs = await queue.claim(worker_id=worker_id, max_jobs=ctx.settings.worker_batch)
            if not jobs:
                await asyncio.sleep(poll)
                continue
            await asyncio.gather(*(_safe(ctx, j) for j in jobs))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("worker.loop_error")
            await asyncio.sleep(poll)
