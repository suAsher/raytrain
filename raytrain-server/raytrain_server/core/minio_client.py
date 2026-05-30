"""
MinIO client factory used by /v1/code (PUT object) and downstream lookups.

Centralized so we only have one place that knows how to construct a Minio
instance and one place that knows the default bucket name.
"""
from __future__ import annotations

from urllib.parse import urlparse

from minio import Minio

from .settings import Settings, get_settings


def make_minio_client(settings: Settings | None = None) -> Minio:
    s = settings or get_settings()
    u = urlparse(s.minio_endpoint)
    host = u.netloc or u.path
    secure = s.minio_secure or u.scheme == "https"
    return Minio(
        host,
        access_key=s.minio_access_key,
        secret_key=s.minio_secret_key,
        secure=secure,
        region=s.minio_region,
    )


def ensure_bucket(client: Minio, bucket: str) -> None:
    """Create ``bucket`` if it doesn't exist; idempotent."""
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
