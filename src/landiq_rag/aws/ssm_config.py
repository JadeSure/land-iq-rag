"""Hydrate Settings from SSM Parameter Store on AWS (config in one place, F8).

Parameters live under a prefix (e.g. /landiq-rag/). The last path segment maps to
a Settings field; SecureString params (the OpenAI key) are decrypted. Nothing is
read from SSM under PROFILE=local.
"""

from __future__ import annotations

from ..config import Settings

# SSM param leaf -> (Settings attribute, caster)
_FIELDS: dict[str, tuple[str, type]] = {
    "active_embedding_model": ("active_embedding_model", str),
    "openai_api_key": ("openai_api_key", str),
    "database_url": ("database_url", str),
    "sqs_queue_url": ("sqs_queue_url", str),
    "s3_bucket": ("s3_bucket", str),
    "storage_backend": ("storage_backend", str),
    "selfhosted_url": ("selfhosted_url", str),
    "selfhosted_dim": ("selfhosted_dim", int),
    "chunk_tokens": ("chunk_tokens", int),
    "chunk_overlap": ("chunk_overlap", int),
}


def hydrate_settings_from_ssm(settings: Settings) -> Settings:
    if not settings.ssm_prefix:
        return settings
    import boto3

    ssm = boto3.client("ssm")
    prefix = settings.ssm_prefix.rstrip("/") + "/"
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=prefix, Recursive=True, WithDecryption=True):
        for param in page["Parameters"]:
            leaf = param["Name"].rsplit("/", 1)[-1]
            mapping = _FIELDS.get(leaf)
            if mapping is None:
                continue
            attr, caster = mapping
            setattr(settings, attr, caster(param["Value"]))
    return settings
