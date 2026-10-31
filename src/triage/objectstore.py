"""Reads log bundles from the S3-compatible object store."""

from __future__ import annotations

import asyncio
from typing import Protocol

from .bundle import BundleError
from .tracing import tracer


class ObjectStore(Protocol):
    async def get(self, key: str, max_bytes: int) -> bytes: ...


class S3Store:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str) -> None:
        import boto3
        from botocore.config import Config

        url = endpoint if "://" in endpoint else f"http://{endpoint}"
        self.bucket = bucket
        self.s3 = boto3.client(
            "s3",
            endpoint_url=url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
            config=Config(
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=30,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )

    def _get(self, key: str, max_bytes: int) -> bytes:
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        size = obj.get("ContentLength") or 0
        if size > max_bytes:
            obj["Body"].close()
            raise BundleError(f"{key} is {size} bytes, over the {max_bytes}-byte limit")
        return obj["Body"].read(max_bytes + 1)

    async def get(self, key: str, max_bytes: int) -> bytes:
        with tracer.start_as_current_span(
            "s3 GetObject", attributes={"aws.s3.bucket": self.bucket, "aws.s3.key": key}
        ):
            return await asyncio.to_thread(self._get, key, max_bytes)


class MemoryStore:
    """For tests and evals."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self.objects = dict(objects or {})

    async def get(self, key: str, max_bytes: int) -> bytes:
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]
