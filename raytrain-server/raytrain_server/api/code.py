"""
``PUT /v1/code`` — receive a code zip from the CLI, store it in MinIO, return
the resulting ``s3://`` URI plus a SHA256 fingerprint.

Body is the raw zip (Content-Type: application/zip). The CLI must send:
    - X-Code-Sha256: <hex>     (sender's own hash; we re-verify)
    - X-Job-Name:    <name>    (used to key the object: <user>/<job>.zip)

Auth: bearer token; user identity becomes the bucket prefix.

Notes:
    - We re-hash the body server-side and reject mismatches → defends against
      truncated uploads even when the client computes its own SHA256.
    - We cap incoming zips at ``settings.code_max_size_mib * 1MiB`` (default
      200) — bigger and we 413 before storing anything.
    - Bucket lifecycle (7d expiration) is enforced at MinIO via
      ``deploy/setup-code-bucket.sh``; the server does NOT manage lifecycle.
"""
from __future__ import annotations

import hashlib
import io
import re

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    status,
)
from minio.error import S3Error
from pydantic import BaseModel

from ..core.jwt_auth import Identity, require_user
from ..core.minio_client import ensure_bucket, make_minio_client
from ..core.settings import Settings, get_settings

router = APIRouter(prefix="/v1/code", tags=["code"])

# 200 MiB matches the CLI default; centrally enforced here so a hostile or
# misconfigured CLI can't DoS the server.
DEFAULT_MAX_SIZE_BYTES = 200 * 1024 * 1024

# Job name validation: DNS-1123-ish. Tightened to also forbid '..' so we
# can't be tricked into writing outside a user's prefix.
_JOB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_USER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


class UploadResult(BaseModel):
    code_uri: str
    sha256: str
    size_bytes: int
    bucket: str
    object_key: str


def _validate_job_name(job_name: str) -> str:
    job = job_name.strip().lower()
    if not _JOB_NAME_RE.match(job):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid X-Job-Name (must match ^[a-z0-9][a-z0-9-]{0,80}$)",
        )
    return job


def _validate_user_for_key(user: str) -> str:
    u = user.strip().lower()
    if not _USER_NAME_RE.match(u):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="token sub is not a valid object-key prefix",
        )
    return u


@router.put(
    "",
    response_model=UploadResult,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a code zip and get its s3:// URI",
)
async def upload_code(
    request: Request,
    x_code_sha256: str = Header(..., alias="X-Code-Sha256"),
    x_job_name: str = Header(..., alias="X-Job-Name"),
    identity: Identity = Depends(require_user),
    settings: Settings = Depends(get_settings),
) -> UploadResult:
    job_name = _validate_job_name(x_job_name)
    user = _validate_user_for_key(identity.user)
    bucket = settings.code_bucket
    object_key = f"{user}/{job_name}.zip"

    # Read & hash in chunks; cap total size.
    hasher = hashlib.sha256()
    buf = io.BytesIO()
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > DEFAULT_MAX_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"code zip exceeds {DEFAULT_MAX_SIZE_BYTES // (1024*1024)} "
                    "MiB limit"
                ),
            )
        hasher.update(chunk)
        buf.write(chunk)

    if total == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="empty body; nothing to upload",
        )

    server_sha = hasher.hexdigest()
    if x_code_sha256.lower() != server_sha:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "X-Code-Sha256 mismatch — body may be truncated or "
                "corrupted in transit"
            ),
        )

    # Push to MinIO. Wrap in a clear error frame so callers can distinguish
    # auth failures (401 from upstream) from network blips.
    client = make_minio_client(settings)
    try:
        ensure_bucket(client, bucket)
        buf.seek(0)
        client.put_object(
            bucket_name=bucket,
            object_name=object_key,
            data=buf,
            length=total,
            content_type="application/zip",
            metadata={
                "x-amz-meta-raytrain-sha256": server_sha,
                "x-amz-meta-raytrain-user": user,
                "x-amz-meta-raytrain-job": job_name,
            },
        )
    except S3Error as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"MinIO upload failed: {exc.code}: {exc.message}",
        ) from exc

    return UploadResult(
        code_uri=f"s3://{bucket}/{object_key}",
        sha256=server_sha,
        size_bytes=total,
        bucket=bucket,
        object_key=object_key,
    )
