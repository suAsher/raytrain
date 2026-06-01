"""
``/v1/workspaces`` — manage long-lived browser dev environments.

A Workspace is a CPU pod with Jupyter / VS Code / PyCharm / SSH baked in,
plus an RWX PVC so the user's code survives restarts. The user reaches it
entirely through the browser — they never install raytrain themselves.

Flow:
    POST   /v1/workspaces            create (PVC + Pod + Service)
    GET    /v1/workspaces            list mine (admin: all)
    GET    /v1/workspaces/{id}       detail incl. IDE URLs + live pod phase
    POST   /v1/workspaces/{id}/stop  delete pod, keep PVC (code preserved)
    POST   /v1/workspaces/{id}/start re-create pod against existing PVC
    DELETE /v1/workspaces/{id}       delete pod + service (+ PVC if requested)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..core.jwt_auth import Identity, require_user
from ..core.k8s_client import K8sClient
from ..core.settings import Settings, get_settings
from ..core.store import WorkspaceRecord, WorkspaceStore, get_workspace_store
from ..core import workspace as ws
from ..core.workspace_service import WorkspaceService, derive_state, validate_image

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/workspaces", tags=["workspaces"])

# Per-user cap (M4 will read this from the DB / quota engine).
DEFAULT_MAX_WORKSPACES = 3


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=40)
    image: str | None = None
    cpu: int = Field(default=0, ge=0, le=64)
    memory_gi: int = Field(default=0, ge=0, le=512)
    pvc_gi: int = Field(default=0, ge=0, le=2000)
    enabled_ides: list[str] = Field(
        default_factory=lambda: ["jupyter", "code", "ssh"]
    )


class WorkspaceResponse(BaseModel):
    id: str
    user: str
    tenant: str
    name: str
    image: str
    state: str
    pod_phase: str | None = None
    reason: str | None = None
    ide_urls: dict = {}
    cpu: int
    memory_gi: int
    pvc_gi: int

    @classmethod
    def from_record(
        cls,
        rec: WorkspaceRecord,
        pod_phase: str | None = None,
        reason: str | None = None,
        state: str | None = None,
        ide_urls: dict | None = None,
    ) -> "WorkspaceResponse":
        return cls(
            id=rec.id,
            user=rec.user,
            tenant=rec.tenant,
            name=rec.name,
            image=rec.image,
            state=state if state is not None else rec.state,
            pod_phase=pod_phase,
            reason=reason,
            ide_urls=ide_urls if ide_urls is not None else rec.ide_urls,
            cpu=rec.cpu,
            memory_gi=rec.memory_gi,
            pvc_gi=rec.pvc_gi,
        )


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


def _store() -> WorkspaceStore:
    return get_workspace_store()


def _k8s(settings: Settings = Depends(get_settings)) -> K8sClient:
    return K8sClient(in_cluster=settings.in_cluster)


def _spec_from(
    rec: WorkspaceRecord, settings: Settings, token: str
) -> ws.WorkspaceSpec:
    return ws.WorkspaceSpec(
        workspace_id=rec.id,
        user=rec.user,
        tenant=rec.tenant,
        name=rec.name,
        image=rec.image,
        cpu=rec.cpu,
        memory_gi=rec.memory_gi,
        pvc_gi=rec.pvc_gi,
        namespace=settings.workspace_namespace,
        storage_class=settings.workspace_storage_class,
        minio_endpoint=settings.minio_endpoint,
        minio_access_key=settings.minio_access_key,
        minio_secret_key=settings.minio_secret_key,
        submission_server="",  # in-pod CLI reaches server via in-cluster DNS
        raytrain_token=token,
        mlflow_uri=settings.mlflow_tracking_uri,
    )


def _ensure_owner(identity: Identity, rec: WorkspaceRecord) -> None:
    if identity.is_admin or rec.user == identity.user:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not owner")


def _audit(identity: Identity, action: str, resource: str, result: str) -> None:
    from ..core.audit import get_audit

    get_audit().record(user=identity.user, action=action, resource=resource, result=result)


def _build_response(
    rec: WorkspaceRecord, k8s: K8sClient, settings: Settings
) -> WorkspaceResponse:
    """Derive real state + (only when running) NodePort IDE URLs."""
    d = derive_state(rec, k8s, settings.workspace_namespace)
    ide_urls: dict = {}
    if d.state == "running" and rec.service_name:
        node_ports = k8s.service_node_ports(rec.service_name, settings.workspace_namespace)
        node_host = settings.workspace_node_host or (
            k8s.node_address(rec.pod_name, settings.workspace_namespace) or ""
        )
        ide_urls = ws.build_ide_urls_nodeport(node_host, node_ports)
    return WorkspaceResponse.from_record(
        rec, pod_phase=d.pod_phase, reason=d.reason, state=d.state, ide_urls=ide_urls
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.post("", response_model=WorkspaceResponse, status_code=201)
def create_workspace(
    body: CreateWorkspaceRequest,
    identity: Identity = Depends(require_user),
    store: WorkspaceStore = Depends(_store),
    k8s: K8sClient = Depends(_k8s),
    settings: Settings = Depends(get_settings),
) -> WorkspaceResponse:
    # quota check (M4: read from DB)
    if store.count_for_user(identity.user) >= DEFAULT_MAX_WORKSPACES:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"workspace limit reached ({DEFAULT_MAX_WORKSPACES})",
        )

    image = body.image or settings.workspace_image
    validate_image(image)  # Req 2.4 — reject malformed custom images

    rec = store.create(
        user=identity.user,
        tenant=identity.tenant,
        name=body.name,
        image=image,
        cpu=body.cpu or settings.workspace_default_cpu,
        memory_gi=body.memory_gi or settings.workspace_default_memory_gi,
        pvc_gi=body.pvc_gi or settings.workspace_default_pvc_gi,
    )

    spec = _spec_from(rec, settings, token="")
    spec.enabled_ides = body.enabled_ides
    store.update(
        rec.id,
        pod_name=spec.pod_name,
        pvc_name=spec.pvc_name,
        service_name=spec.service_name,
    )

    try:
        k8s.ensure_pvc(
            name=spec.pvc_name,
            namespace=spec.namespace,
            size_gi=spec.pvc_gi,
            storage_class=spec.storage_class,
            labels=ws.build_pvc_labels(spec),
        )
        k8s.create_pod(ws.build_pod_manifest(spec), spec.namespace)
        k8s.ensure_service(ws.build_service_manifest(spec), spec.namespace)
    except Exception as exc:  # noqa: BLE001
        store.update(rec.id, state="error")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"failed to create workspace: {exc!r}",
        ) from exc

    # Req 1.1 — do NOT fake "running"; mark creating and let derive_state map
    # the real pod phase on subsequent reads.
    rec = store.update(rec.id, state="creating")
    log.info("workspace.create id=%s user=%s", rec.id, identity.user)
    return _build_response(rec, k8s, settings)


@router.get("", response_model=list[WorkspaceResponse])
def list_workspaces(
    identity: Identity = Depends(require_user),
    store: WorkspaceStore = Depends(_store),
    k8s: K8sClient = Depends(_k8s),
    settings: Settings = Depends(get_settings),
) -> list[WorkspaceResponse]:
    recs = store.list_for_user(identity.user, is_admin=identity.is_admin)
    return [_build_response(r, k8s, settings) for r in recs]


@router.get("/{wid}", response_model=WorkspaceResponse)
def get_workspace(
    wid: str,
    identity: Identity = Depends(require_user),
    store: WorkspaceStore = Depends(_store),
    k8s: K8sClient = Depends(_k8s),
    settings: Settings = Depends(get_settings),
) -> WorkspaceResponse:
    rec = store.get(wid)
    if not rec:
        raise HTTPException(status_code=404, detail="workspace not found")
    _ensure_owner(identity, rec)
    return _build_response(rec, k8s, settings)


@router.post("/{wid}/stop", response_model=WorkspaceResponse)
def stop_workspace(
    wid: str,
    identity: Identity = Depends(require_user),
    store: WorkspaceStore = Depends(_store),
    k8s: K8sClient = Depends(_k8s),
    settings: Settings = Depends(get_settings),
) -> WorkspaceResponse:
    rec = store.get(wid)
    if not rec:
        raise HTTPException(status_code=404, detail="workspace not found")
    _ensure_owner(identity, rec)
    if rec.pod_name:
        k8s.delete_pod(rec.pod_name, settings.workspace_namespace)
    # Req 4.1/4.2 — mark stopping (pod may still be Terminating); derive_state
    # reports 'stopping' until the pod is fully gone, then 'stopped'.
    rec = store.update(wid, state="stopping")
    _audit(identity, "workspace.stop", wid, "ok")
    return _build_response(rec, k8s, settings)


@router.post("/{wid}/start", response_model=WorkspaceResponse)
def start_workspace(
    wid: str,
    identity: Identity = Depends(require_user),
    store: WorkspaceStore = Depends(_store),
    k8s: K8sClient = Depends(_k8s),
    settings: Settings = Depends(get_settings),
) -> WorkspaceResponse:
    rec = store.get(wid)
    if not rec:
        raise HTTPException(status_code=404, detail="workspace not found")
    _ensure_owner(identity, rec)
    spec = _spec_from(rec, settings, token="")
    svc = WorkspaceService(store, k8s, settings)
    # PVC persists across stop/start.
    try:
        k8s.ensure_pvc(
            name=spec.pvc_name,
            namespace=spec.namespace,
            size_gi=spec.pvc_gi,
            storage_class=spec.storage_class,
            labels=ws.build_pvc_labels(spec),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"failed to ensure PVC: {exc!r}") from exc
    # Req 4.3/4.4/4.5 — wait for any old (Terminating) pod, then create; 409→Friendly.
    svc.start_after_terminating(rec, spec)
    try:
        k8s.ensure_service(ws.build_service_manifest(spec), spec.namespace)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"failed to ensure service: {exc!r}") from exc
    # Req 4.6 — back to creating; derive_state maps to running once ready.
    rec = store.update(wid, state="creating")
    _audit(identity, "workspace.start", wid, "ok")
    return _build_response(rec, k8s, settings)


@router.delete("/{wid}", status_code=204)
def delete_workspace(
    wid: str,
    delete_pvc: bool = Query(default=False),
    identity: Identity = Depends(require_user),
    store: WorkspaceStore = Depends(_store),
    k8s: K8sClient = Depends(_k8s),
    settings: Settings = Depends(get_settings),
) -> None:
    rec = store.get(wid)
    if not rec:
        raise HTTPException(status_code=404, detail="workspace not found")
    _ensure_owner(identity, rec)
    ns = settings.workspace_namespace
    if rec.pod_name:
        k8s.delete_pod(rec.pod_name, ns)
    if rec.service_name:
        k8s.delete_service(rec.service_name, ns)
    if delete_pvc and rec.pvc_name:
        k8s.delete_pvc(rec.pvc_name, ns)
    store.delete(wid)
