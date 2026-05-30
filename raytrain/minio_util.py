"""
MinIO helpers wrapping the `minio` python SDK. Used by:
  - CLI `raytrain data {push,pull,ls}`
  - driver for dataset sync and artifact upload
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from minio import Minio
from minio.error import S3Error


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """`s3://bucket/prefix` -> (bucket, prefix). Prefix may be empty."""
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3 uri: {uri}")
    path = uri[5:]
    if "/" in path:
        bucket, prefix = path.split("/", 1)
    else:
        bucket, prefix = path, ""
    return bucket, prefix


def make_client(endpoint: str, access_key: str, secret_key: str,
                secure: bool = False, region: str = "us-east-1") -> Minio:
    u = urlparse(endpoint)
    host = u.netloc or u.path  # accept both `host:port` and `http://host:port`
    secure_flag = secure or (u.scheme == "https")
    return Minio(host, access_key=access_key, secret_key=secret_key,
                 secure=secure_flag, region=region)


def ensure_bucket(cli: Minio, bucket: str) -> None:
    try:
        if not cli.bucket_exists(bucket):
            cli.make_bucket(bucket)
    except S3Error as e:
        # some policies disallow create; warn and continue (read-only is fine)
        if e.code not in ("BucketAlreadyOwnedByYou", "AccessDenied"):
            raise


def upload_dir(cli: Minio, local_dir: str | Path, bucket: str, prefix: str,
               progress_cb=None) -> int:
    """Recursively upload a directory. Returns number of files uploaded."""
    root = Path(local_dir).resolve()
    n = 0
    for p in root.rglob("*"):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            key = f"{prefix.rstrip('/')}/{rel}" if prefix else rel
            cli.fput_object(bucket, key, str(p))
            if progress_cb:
                progress_cb(str(p), key)
            n += 1
    return n


def download_prefix(cli: Minio, bucket: str, prefix: str, local_dir: str | Path,
                    progress_cb=None, skip_existing: bool = True) -> int:
    """Mirror a prefix down to local_dir. Returns number of files downloaded."""
    root = Path(local_dir)
    root.mkdir(parents=True, exist_ok=True)
    n = 0
    for obj in cli.list_objects(bucket, prefix=prefix.rstrip("/") + "/", recursive=True):
        key = obj.object_name
        rel = key[len(prefix.rstrip("/")) + 1:] if prefix else key
        dst = root / rel
        if skip_existing and dst.exists() and dst.stat().st_size == obj.size:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        cli.fget_object(bucket, key, str(dst))
        if progress_cb:
            progress_cb(key, str(dst))
        n += 1
    return n


def list_prefix(cli: Minio, bucket: str, prefix: str) -> Iterator[str]:
    for obj in cli.list_objects(bucket, prefix=prefix, recursive=True):
        yield obj.object_name
