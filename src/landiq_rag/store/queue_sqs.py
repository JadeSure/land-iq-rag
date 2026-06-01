"""SqsJobQueue: the AWS delivery transport.

enqueue still writes the durable ingest_job row (the F12 status/retry/resume
record) and additionally sends an SQS message carrying the task_id. Delivery to
the worker is the SQS event source -> Lambda, so claim() is never called on AWS
(the Lambda leases the specific job via tasks.claim_by_id). heartbeat is a no-op:
SQS visibility timeout plays that role.
"""

from __future__ import annotations

import asyncio
import json

from psycopg_pool import AsyncConnectionPool

from ..ports import ClaimedJob, IngestJobSpec
from .tasks import PostgresJobQueue


class SqsJobQueue:
    def __init__(self, pool: AsyncConnectionPool, queue_url: str, *, lease_seconds: int = 300) -> None:
        import boto3  # lazy: local dev needs no AWS deps

        self._rows = PostgresJobQueue(pool, lease_seconds=lease_seconds)
        self._queue_url = queue_url
        self._sqs = boto3.client("sqs")

    async def enqueue(self, spec: IngestJobSpec) -> str:
        task_id = await self._rows.enqueue(spec)
        body = json.dumps({"task_id": task_id, "job_type": spec.job_type})
        await asyncio.to_thread(
            self._sqs.send_message, QueueUrl=self._queue_url, MessageBody=body
        )
        return task_id

    async def claim(self, *, worker_id: str, max_jobs: int) -> list[ClaimedJob]:
        raise NotImplementedError("On AWS the SQS event source delivers; use claim_by_id")

    async def heartbeat(self, task_id: str) -> None:
        return None
