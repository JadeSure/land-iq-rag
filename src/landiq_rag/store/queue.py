"""JobQueue factory: Postgres poll-loop locally, SQS delivery on AWS."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from ..config import Settings
from ..ports import JobQueue
from .tasks import PostgresJobQueue


def make_queue(settings: Settings, pool: AsyncConnectionPool) -> JobQueue:
    if settings.profile == "aws":
        if not settings.sqs_queue_url:
            raise ValueError("RAG_SQS_QUEUE_URL must be set when RAG_PROFILE=aws")
        from .queue_sqs import SqsJobQueue

        return SqsJobQueue(pool, settings.sqs_queue_url, lease_seconds=settings.lease_seconds)
    return PostgresJobQueue(pool, lease_seconds=settings.lease_seconds)
