"""StorageBackend implementations.

LocalFsStorage is the local/demo backend. S3Storage is the AWS (Track B) backend
behind the identical port; only the implementation differs, no caller changes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

from ..config import Settings
from ..ports import StorageBackend


def _safe_segment(value: str) -> str:
    """Keep storage keys filesystem-safe without losing the (address, upload) identity."""
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in value)


class LocalFsStorage:
    """Writes under {root}/{address_id}/{upload_id}; returns file:// refs."""

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    async def put(
        self, *, address_id: str, upload_id: str, data: bytes, content_type: str
    ) -> str:
        dest = self.root / _safe_segment(address_id) / _safe_segment(upload_id)
        await asyncio.to_thread(self._write, dest, data)
        return dest.as_uri()

    @staticmethod
    def _write(dest: Path, data: bytes) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    async def get(self, ref: str) -> bytes:
        path = Path(urlparse(ref).path)
        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, ref: str) -> None:
        path = Path(urlparse(ref).path)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))


class S3Storage:
    """AWS backend (Track B). Keys: address=<id>/doc=<upload_id>/original/<name>.

    boto3 is synchronous; calls are offloaded to threads to keep the port async.

    ``endpoint_url`` points the client at an S3-compatible store (MinIO/LocalStack)
    for local dev; leave it ``None`` for real AWS so boto3 uses the default endpoint
    and the instance/role credential chain. A custom endpoint forces path-style
    addressing (``host/bucket/key``) since bucket-as-subdomain won't resolve locally.
    """

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        import boto3  # imported lazily so local dev needs no AWS deps at import time

        self.bucket = bucket
        client_kwargs: dict = {}
        if endpoint_url:
            from botocore.config import Config

            client_kwargs["endpoint_url"] = endpoint_url
            client_kwargs["config"] = Config(s3={"addressing_style": "path"})
        if region:
            client_kwargs["region_name"] = region
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key
        self._s3 = boto3.client("s3", **client_kwargs)

    def _key(self, address_id: str, upload_id: str) -> str:
        return f"address={_safe_segment(address_id)}/doc={_safe_segment(upload_id)}/original"

    async def put(
        self, *, address_id: str, upload_id: str, data: bytes, content_type: str
    ) -> str:
        key = self._key(address_id, upload_id)
        await asyncio.to_thread(
            self._s3.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
        )
        return f"s3://{self.bucket}/{key}"

    async def get(self, ref: str) -> bytes:
        parsed = urlparse(ref)
        bucket, key = parsed.netloc, parsed.path.lstrip("/")
        obj = await asyncio.to_thread(self._s3.get_object, Bucket=bucket, Key=key)
        return await asyncio.to_thread(obj["Body"].read)

    async def delete(self, ref: str) -> None:
        parsed = urlparse(ref)
        bucket, key = parsed.netloc, parsed.path.lstrip("/")
        await asyncio.to_thread(self._s3.delete_object, Bucket=bucket, Key=key)


def make_storage(settings: Settings) -> StorageBackend:
    if settings.storage_backend == "s3":
        if not settings.s3_bucket:
            raise ValueError("RAG_S3_BUCKET must be set when RAG_STORAGE_BACKEND=s3")
        return S3Storage(
            settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url or None,
            region=settings.aws_region or None,
            access_key=settings.aws_access_key_id or None,
            secret_key=settings.aws_secret_access_key or None,
        )
    return LocalFsStorage(settings.storage_local_root)
