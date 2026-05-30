"""
``/v1/dev-sessions`` — short-lived GPU debug sessions bound to a Workspace.

Flow:
    POST   /v1/dev-sessions               request 1..8 GPUs on a Workspace
    GET    /v1/dev-sessions               list mine
    GET    /v1/dev-sessions/{id}          detail + live pod phase
    POST   /v1/dev-sessions/{id}/heartbeat keep-alive (resets idle timer)
    DELETE /v1/dev-sessions/{id}          terminate now

Auto-reclaim: a background sweep (api.reclaim) kills sessions whose idle /
lifetime budget is exhausted. The session shares its Workspace's PVC so the
code the user debugs is exactly what they'll submit.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..core import devsession as dv
from ..core.jwt_auth import Identity, require_user
from ..core.k8s_client import K8sClient
from ..core.settings import Settings, get_settings
from ..core.store import (
    DevSessionRecord,
    DevSessionStore,
    WorkspaceStore,
    get_devsession_store,
    get_workspace_store,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/dev-sessions", tags=["dev-sessions"])

# Per-user GPU cap for DevSessions (M4: from quota engine).
DEFAULT_MAX_GPU_PER_USER = 8


class CreateDevSessionRequest(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    gpu_type: str = Field(..., examples=["h20", "a100"])
    gpu_count: int = Field(default=1, ge=1, le=8)
    image: str | None = None
    enabled_ides: list[str] = Field(
        default_factory=lambda: ["jupyter", "code", "ssh"]
    )


class DevSessionResponse(BaseModel):
    id: str
    workspace_id: str
    user: str
    gpu_type: str
    gpu_count: int
    state: str
    pod_phase: str | None = None
    ide_urls: dict = {}

    @classmethod
    def from_record(cls, r: DevSessionRecord, pod_phase: str | None = None):
        return cls(
            id=r.id,
            workspace_id=r.workspace_id,
            user=r.user,
            gpu_type=r.gpu_type,
            gpu_count=r.gpu_count,
            state=r.state,
            pod_phase=pod_phase,
            ide_urls=r.ide_urls,
        )


def _store() -> DevSessionStore:
    return get_devsession_store()


def _ws_store() -> WorkspaceStore:
    return get_workspace_store()


def _k8s(settings: Settings = Depends(get_settings)) -> K8sClient:
    return K8sClient(in_cluster=settings.in_cluster)


def _ensure_owner(identity: Identity, rec: DevSessionRecord) -> None:
    if identity.is_admin or rec.user == identity.user:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not owner")


@router.post("", response_model=DevSessionResponse, status_code=201)
def create_devsession(
    body: CreateDevSessionRequest,
    identity: Identity = Depends(require_user),
    store: DevSessionStore = Depends(_store),
    ws_store: WorkspaceStore = Depends(_ws_store),
    k8s: K8sClient = Depends(_k8s),
    settings: Settings = Depends(get_settings),
) -> DevSessionResponse:
    # Parent workspace must exist & belong to caller
    ws_rec = ws_store.get(body.workspace_id)
    if not ws_rec:
        raise HTTPException(status_code=404, detail="parent workspace not found")
    if not identity.is_admin and ws_rec.user != identity.user:
        raise HTTPException(status_code=403, detail="not owner of workspace")

    # GPU quota check (M4: quota engine)
    in_use = store.gpu_in_use_by_user(identity.user)
    if in_use + body.gpu_count > DEFAULT_MAX_GPU_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"GPU quota exceeded: {in_use} in use + {body.gpu_count} "
                f"requested > {DEFAULT_MAX_GPU_PER_USER}"
            ),
        )

    rec = store.create(
        workspace_id=body.workspace_id,
        user=identity.user,
        tenant=identity.tenant,
        image=body.image or settings.devsession_image,
        gpu_type=body.gpu_type,
        gpu_count=body.gpu_count,
        pvc_name=ws_rec.pvc_name,
        idle_timeout_s=settings.devsession_idle_timeout_s,
        max_lifetime_s=settings.devsession_max_lifetime_s,
    )

    spec = dv.DevSessionSpec(
        session_id=rec.id,
        workspace_id=rec.workspace_id,
        user=rec.user,
        tenant=rec.tenant,
        image=rec.image,
        gpu_type=rec.gpu_type,
        gpu_count=rec.gpu_count,
        pvc_name=rec.pvc_name,
        namespace=settings.devsession_namespace,
        cpu=settings.devsession_default_cpu,
        memory_gi=settings.devsession_default_memory_gi,
        minio_endpoint=settings.minio_endpoint,
        minio_access_key=settings.minio_access_key,
        minio_secret_key=settings.minio_secret_key,
        mlflow_uri=settings.mlflow_tracking_uri,
        enabled_ides=body.enabled_ides,
    )
    store.update(
        rec.id,
        pod_name=spec.pod_name,
        service_name=spec.service_name,
        ide_urls=dv.build_ide_urls(spec, settings.workspace_base_domain),
    )

    try:
        k8s.create_pod(dv.build_pod_manifest(spec), spec.namespace)
        k8s.ensure_service(dv.build_service_manifest(spec), spec.namespace)
    except Exception as exc:  # noqa: BLE001
        store.update(rec.id, state="error")
        raise HTTPException(
            status_code=502, detail=f"failed to create dev session: {exc!r}"
        ) from exc

    rec = store.update(rec.id, state="running")
    log.info("devsession.create id=%s user=%s gpu=%dx%s",
             rec.id, identity.user, body.gpu_count, body.gpu_type)
    return DevSessionResponse.from_record(rec)


@router.get("", response_model=list[DevSessionResponse])
def list_devsessions(
    identity: Identity = Depends(require_user),
    store: DevSessionStore = Depends(_store),
) -> list[DevSessionResponse]:
    recs = store.list_for_user(identity.user, is_admin=identity.is_admin)
    return [DevSessionResponse.from_record(r) for r in recs]


@router.get("/{sid}", response_model=DevSessionResponse)
def get_devsession(
    sid: str,
    identity: Identity = Depends(require_user),
    store: DevSessionStore = Depends(_store),
    k8s: K8sClient = Depends(_k8s),
    settings: Settings = Depends(get_settings),
) -> DevSessionResponse:
    rec = store.get(sid)
    if not rec:
        raise HTTPException(status_code=404, detail="dev session not found")
    _ensure_owner(identity, rec)
    phase = None
    if rec.pod_name:
        try:
            phase = k8s.pod_phase(rec.pod_name, settings.devsession_namespace)
        except Exception:  # noqa: BLE001
            phase = "Unknown"
    return DevSessionResponse.from_record(rec, pod_phase=phase)


@router.post("/{sid}/heartbeat", response_model=DevSessionResponse)
def heartbeat(
    sid: str,
    identity: Identity = Depends(require_user),
    store: DevSessionStore = Depends(_store),
) -> DevSessionResponse:
    import time as _t
    rec = store.get(sid)
    if not rec:
        raise HTTPException(status_code=404, detail="dev session not found")
    _ensure_owner(identity, rec)
    rec = store.update(sid, last_seen_at=_t.time())
    return DevSessionResponse.from_record(rec)


@router.delete("/{sid}", status_code=204)
def delete_devsession(
    sid: str,
    identity: Identity = Depends(require_user),
    store: DevSessionStore = Depends(_store),
    k8s: K8sClient = Depends(_k8s),
    settings: Settings = Depends(get_settings),
) -> None:
    rec = store.get(sid)
    if not rec:
        raise HTTPException(status_code=404, detail="dev session not found")
    _ensure_owner(identity, rec)
    ns = settings.devsession_namespace
    if rec.pod_name:
        k8s.delete_pod(rec.pod_name, ns)
    if rec.service_name:
        k8s.delete_service(rec.service_name, ns)
    store.delete(sid)
