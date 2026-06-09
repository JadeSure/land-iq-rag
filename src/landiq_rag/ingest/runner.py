"""Transport-agnostic job execution.

`process_job` is the single body shared by the local poll-loop worker and the
AWS SQS Lambda. It dispatches ingest vs rebuild and contains all failure handling
(retry-vs-fail classification, status updates), so neither transport duplicates
that logic and the pipeline never knows which delivery path ran it.
"""

from __future__ import annotations

import asyncio

import structlog

from ..context import AppContext
from ..ports import ClaimedJob
from ..store import tasks
from .pipeline import PipelineError, run_ingest_job, run_rebuild_job

log = structlog.get_logger()


def is_retryable(exc: BaseException) -> bool:
    """Transient failures (rate limits, timeouts, connection) retry; others fail fast."""
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, TimeoutError)):
        return True
    name = type(exc).__name__
    return name in {
        # OpenAI / embedding-provider transients.
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
        "APIError",
        # botocore S3 transients — connection-level, always transient. These do
        # not subclass ConnectionError, so they must be matched by name. Note:
        # ClientError is deliberately excluded (it also covers permanent 404s).
        "EndpointConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ConnectionClosedError",
    }


async def process_job(ctx: AppContext, job: ClaimedJob) -> None:
    """Run one already-leased job to a terminal state. Never raises for job-level
    failures; it records them on the ingest_job row (no silent failures, 6.5)."""
    try:
        if job.job_type == "rebuild":
            await run_rebuild_job(ctx, job)
        else:
            await run_ingest_job(ctx, job)
        log.info("job.done", task_id=job.task_id, job_type=job.job_type, address=job.address_id)
    except Exception as exc:  # noqa: BLE001  (CancelledError is BaseException; let it propagate)
        step = exc.step if isinstance(exc, PipelineError) else job.state
        cause = exc.cause if isinstance(exc, PipelineError) else exc
        outcome = await tasks.mark_failed(
            ctx.pool,
            job,
            step=step,
            error_type=type(cause).__name__,
            message=str(cause),
            retryable=is_retryable(cause),
        )
        log.warning(
            "job.failed",
            task_id=job.task_id,
            step=step,
            error=type(cause).__name__,
            outcome=outcome,
            attempt=job.attempts,
        )
        # On AWS the SQS Lambda needs the exception to surface so the message is
        # retried/DLQ'd per maxReceiveCount; re-raise only when a retry is wanted.
        if outcome == "pending":
            raise
