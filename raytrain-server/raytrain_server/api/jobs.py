"""
``/v1/jobs`` — submit, list, query, stop, tail-logs.

This is the user-facing replacement for ``raytrain submit``: instead of the
CLI talking K8s directly, it POSTs here, the server validates the request,
authorises it (token + tenant), and forwards to the right long-lived
RayCluster via ``RayClusterClient``.

Concurrency note:
    All endpoints run inside FastAPI's threadpool because the underlying
    ray.job_submission.JobSubmissionClient is sync. Don't add ``async def``
    bodies here unless you also vet the Ray call paths.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from ..core.jwt_auth import Identity, require_user
from ..core.quota import ResourceAsk, Usage, check_quota
from ..core.ray_client import (
    JobSubmissionSpec,
    RayClusterClient,
    make_submission_id,
)
from ..core.settings import Settings, get_settings
from ..core.store import get_devsession_store
from ..core.users import UserQuota, get_user_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])


# ---------------------------------------------------------------------------- #
# Schemas
# ---------------------------------------------------------------------------- #


class SubmitJobRequest(BaseModel):
    """Body of ``POST /v1/jobs``. Mirrors the CLI's submit flags."""

    repo: str = Field(..., min_length=1, max_length=64)
    exp_name: str = Field(..., min_length=1, max_length=64)
    gpu_type: str = Field(..., examples=["h20", "a100"])
    num_nodes: int = Field(..., ge=1, le=64)
    gpus_per_node: int = Field(..., ge=0, le=8)
    entrypoint: str = Field(
        ...,
        description=(
            "The shell command Ray will run, e.g. "
            "'python tools/train.py --config configs/x.py'"
        ),
    )
    code_uri: str | None = Field(
        default=None,
        description="s3:// URI returned by /v1/code",
    )
    code_hash: str | None = None
    extra_env: dict[str, str] = Field(default_factory=dict)
    extra_pip: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("entrypoint")
    @classmethod
    def _no_shell_metachars(cls, v: str) -> str:
        # Crude but effective; Ray will run this with shell=True on the
        # head pod so we should at least block the obvious nasties.
        forbidden = ("`", "$(", ";", "&&", "||", "\n", "\r")
        for f in forbidden:
            if f in v:
                raise ValueError(f"entrypoint contains forbidden token {f!r}")
        return v


class SubmitJobResponse(BaseModel):
    submission_id: str
    code_uri: str | None
    cluster_address: str
    runtime_env: dict[str, Any]


class JobInfo(BaseModel):
    submission_id: str
    status: str
    metadata: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------------- #


def _ray_client(settings: Settings = Depends(get_settings)) -> RayClusterClient:
    return RayClusterClient(settings=settings)


def _validate_gpu_type(settings: Settings, gpu_type: str) -> None:
    if gpu_type not in settings.shared_clusters:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"unsupported gpu_type {gpu_type!r}; available: "
                f"{sorted(settings.shared_clusters)}"
            ),
        )


def _ensure_owner_or_admin(identity: Identity, owner_user: str) -> None:
    if identity.is_admin:
        return
    if identity.user != owner_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not the owner of this submission",
        )


def _read_metadata_owner(metadata: dict[str, str]) -> str:
    return metadata.get("raytrain.user", "")


def _enforce_quota(identity: Identity, body: "SubmitJobRequest", ray: "RayClusterClient") -> None:
    """Per-user quota gate. Admins bypass. Unknown users fall back to unlimited
    (so a freshly-bootstrapped platform without a user table still works); set
    caps by creating the user via /v1/admin/users.

    Usage = GPUs this user already has committed across (a) running shared
    RayJobs on the requested cluster and (b) running dev-sessions. The ask is
    num_nodes * gpus_per_node for this submission.
    """
    if identity.is_admin:
        return
    rec = get_user_store().get(identity.user)
    if rec is None:
        return  # no record yet -> no caps enforced
    if not rec.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用，请联系管理员",
        )
    quota: UserQuota = rec.quota

    ask = ResourceAsk(gpus=body.num_nodes * body.gpus_per_node, jobs=1)

    # current committed GPU usage: dev-sessions + this user's running jobs.
    gpu_used = get_devsession_store().gpu_in_use_by_user(identity.user)
    jobs_used = 0
    try:
        for j in ray.list_jobs(body.gpu_type):
            meta = dict(getattr(j, "metadata", {}) or {})
            if meta.get("raytrain.user", "") != identity.user:
                continue
            st = str(getattr(j, "status", "")).upper()
            if st in ("PENDING", "RUNNING"):
                jobs_used += 1
                try:
                    gpu_used += int(meta.get("raytrain.gpus_total", 0) or 0)
                except (TypeError, ValueError):
                    pass
    except Exception:  # never let a usage probe failure block on its own
        log.warning("quota: could not list jobs for usage probe", exc_info=True)

    usage = Usage(gpus=gpu_used, jobs=jobs_used)
    violation = check_quota(quota, usage, ask)
    if violation is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=violation.message,
        )


# ---------------------------------------------------------------------------- #
# Endpoints
# ---------------------------------------------------------------------------- #


@router.post(
    "",
    response_model=SubmitJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a training job to the shared RayCluster",
)
def submit_job(
    body: SubmitJobRequest,
    identity: Identity = Depends(require_user),
    ray: RayClusterClient = Depends(_ray_client),
    settings: Settings = Depends(get_settings),
) -> SubmitJobResponse:
    _validate_gpu_type(settings, body.gpu_type)
    _enforce_quota(identity, body, ray)

    # stamp gpus_total into metadata so future quota probes can sum it.
    md = dict(body.metadata)
    md.setdefault("raytrain.gpus_total", str(body.num_nodes * body.gpus_per_node))

    spec = JobSubmissionSpec(
        user=identity.user,
        tenant=identity.tenant,
        gpu_type=body.gpu_type,
        num_nodes=body.num_nodes,
        gpus_per_node=body.gpus_per_node,
        entrypoint=body.entrypoint,
        code_uri=body.code_uri,
        code_hash=body.code_hash,
        extra_env=body.extra_env,
        extra_pip=body.extra_pip,
        metadata=md,
    )
    submission_id = make_submission_id(identity.user, body.repo, body.exp_name)
    runtime_env = ray.build_runtime_env(spec)

    log.info(
        "job.submit",
        extra={
            "user": identity.user,
            "tenant": identity.tenant,
            "submission_id": submission_id,
            "gpu_type": body.gpu_type,
            "code_uri": body.code_uri or "",
        },
    )
    try:
        sid = ray.submit_job(spec, submission_id=submission_id, repo=body.repo)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except Exception as exc:  # network / ray API failure
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Ray submission failed: {exc!r}",
        ) from exc

    return SubmitJobResponse(
        submission_id=sid,
        code_uri=body.code_uri,
        cluster_address=ray.address_for(body.gpu_type),
        runtime_env=runtime_env,
    )


@router.get("/{submission_id}", response_model=JobInfo)
def get_job(
    submission_id: str,
    gpu_type: str,
    identity: Identity = Depends(require_user),
    ray: RayClusterClient = Depends(_ray_client),
    settings: Settings = Depends(get_settings),
) -> JobInfo:
    _validate_gpu_type(settings, gpu_type)
    try:
        info = ray.get_info(gpu_type, submission_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ray error: {exc!r}") from exc

    meta = dict(getattr(info, "metadata", {}) or {})
    _ensure_owner_or_admin(identity, _read_metadata_owner(meta))
    return JobInfo(
        submission_id=submission_id,
        status=str(getattr(info, "status", "UNKNOWN")),
        metadata=meta,
    )


@router.delete("/{submission_id}", status_code=status.HTTP_204_NO_CONTENT)
def stop_job(
    submission_id: str,
    gpu_type: str,
    identity: Identity = Depends(require_user),
    ray: RayClusterClient = Depends(_ray_client),
    settings: Settings = Depends(get_settings),
) -> None:
    _validate_gpu_type(settings, gpu_type)
    try:
        info = ray.get_info(gpu_type, submission_id)
        meta = dict(getattr(info, "metadata", {}) or {})
        _ensure_owner_or_admin(identity, _read_metadata_owner(meta))
        ray.stop(gpu_type, submission_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ray error: {exc!r}") from exc


@router.get(
    "/{submission_id}/logs",
    summary="Stream stdout/stderr of a submission",
)
def tail_logs(
    submission_id: str,
    gpu_type: str,
    identity: Identity = Depends(require_user),
    ray: RayClusterClient = Depends(_ray_client),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    _validate_gpu_type(settings, gpu_type)
    try:
        info = ray.get_info(gpu_type, submission_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ray error: {exc!r}") from exc

    meta = dict(getattr(info, "metadata", {}) or {})
    _ensure_owner_or_admin(identity, _read_metadata_owner(meta))

    def generator():
        try:
            for line in ray.tail_logs(gpu_type, submission_id):
                yield line
        except Exception as exc:  # noqa: BLE001
            yield f"\n[server] log stream error: {exc!r}\n"

    return StreamingResponse(generator(), media_type="text/plain")


@router.get(
    "",
    summary="List submissions visible to the caller",
)
def list_jobs(
    gpu_type: str,
    identity: Identity = Depends(require_user),
    ray: RayClusterClient = Depends(_ray_client),
    settings: Settings = Depends(get_settings),
) -> list[JobInfo]:
    _validate_gpu_type(settings, gpu_type)
    try:
        all_jobs = ray.list_jobs(gpu_type)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"ray error: {exc!r}") from exc

    out: list[JobInfo] = []
    for j in all_jobs:
        meta = dict(getattr(j, "metadata", {}) or {})
        owner = _read_metadata_owner(meta)
        if not identity.is_admin and owner != identity.user:
            continue
        out.append(
            JobInfo(
                submission_id=str(getattr(j, "submission_id", "")),
                status=str(getattr(j, "status", "UNKNOWN")),
                metadata=meta,
            )
        )
    return out
