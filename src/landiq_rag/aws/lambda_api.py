"""API Gateway -> Lambda entrypoint (FastAPI via Mangum).

Same FastAPI app, routes and DTOs as local. The poll-worker is disabled (ingestion
runs in the SQS worker Lambda) and migrations are not run on cold start (the EC2
bootstrap owns schema). Config is hydrated from SSM at import.
"""

from __future__ import annotations

import os

os.environ.setdefault("RAG_PROFILE", "aws")
os.environ.setdefault("RAG_WORKER_ENABLED", "false")
os.environ.setdefault("RAG_RUN_MIGRATIONS_ON_STARTUP", "false")

from mangum import Mangum  # noqa: E402

from ..config import get_settings  # noqa: E402
from .ssm_config import hydrate_settings_from_ssm  # noqa: E402

hydrate_settings_from_ssm(get_settings())

from ..main import create_app  # noqa: E402

app = create_app()
handler = Mangum(app, lifespan="auto")
