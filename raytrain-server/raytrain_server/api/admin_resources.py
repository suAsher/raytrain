"""
``/v1/admin/resources/{kind}`` — admin CRUD for catalog resources
(projects / quota_groups / runtime_images), plus ``/v1/admin/queues`` writes.

Read paths are open to any authenticated user (the Create-Job wizard needs the
project / image / quota-group lists); writes are admin-only.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..core.jwt_auth import Identity, require_admin, require_user
from ..core.queues_store import QueueRecord, get_queue_store
from ..core.resources_store import KINDS, get_resource_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"])


class ResourceBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    spec: dict = Field(default_factory=dict)
    enabled: bool = True


class ResourceView(BaseModel):
    id: str
    kind: str
    name: str
    spec: dict
    enabled: bool
    created_at: float
    updated_at: float


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise HTTPException(400, detail=f"unknown kind {kind!r}; valid: {list(KINDS)}")


@router.get("/resources/{kind}", response_model=list[ResourceView])
def list_resources(kind: str, identity: Identity = Depends(require_user)) -> list[ResourceView]:
    _check_kind(kind)
    return [ResourceView(**r.to_dict()) for r in get_resource_store().list(kind)]


@router.post("/resources/{kind}", response_model=ResourceView, status_code=201)
def create_resource(
    kind: str, body: ResourceBody, admin: Identity = Depends(require_admin)
) -> ResourceView:
    _check_kind(kind)
    rec = get_resource_store().create(kind, body.name, body.spec)
    log.info("admin.resource.create kind=%s name=%s by=%s", kind, body.name, admin.user)
    return ResourceView(**rec.to_dict())


@router.patch("/resources/{kind}/{rid}", response_model=ResourceView)
def update_resource(
    kind: str, rid: str, body: ResourceBody, admin: Identity = Depends(require_admin)
) -> ResourceView:
    _check_kind(kind)
    store = get_resource_store()
    if store.get(rid) is None:
        raise HTTPException(404, detail="resource not found")
    rec = store.update(rid, name=body.name, spec=body.spec, enabled=body.enabled)
    return ResourceView(**rec.to_dict())


@router.delete("/resources/{kind}/{rid}", status_code=204)
def delete_resource(
    kind: str, rid: str, admin: Identity = Depends(require_admin)
) -> None:
    _check_kind(kind)
    if not get_resource_store().delete(rid):
        raise HTTPException(404, detail="resource not found")


# --------------------------------------------------------------------------- #
# Queues (admin write; live read stays on /v1/console/queues)
# --------------------------------------------------------------------------- #


class QueueBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    cluster_queue: str = "cq-default"
    gpu_type: str = "H20"
    nominal: int = Field(0, ge=0)


class QueueView(BaseModel):
    name: str
    cluster_queue: str
    gpu_type: str
    nominal: int
    used: int
    pending: int
    admitted: int
    avg_wait_min: int
    health: str


@router.get("/queues", response_model=list[QueueView])
def list_queues(identity: Identity = Depends(require_user)) -> list[QueueView]:
    return [QueueView(**q.to_dict()) for q in get_queue_store().list_queues()]


@router.post("/queues", response_model=QueueView, status_code=201)
def create_queue(body: QueueBody, admin: Identity = Depends(require_admin)) -> QueueView:
    store = get_queue_store()
    rec = QueueRecord(
        name=body.name, cluster_queue=body.cluster_queue,
        gpu_type=body.gpu_type, nominal=body.nominal,
    )
    try:
        store.add_queue(rec)
    except ValueError as exc:
        raise HTTPException(409, detail=str(exc))
    log.info("admin.queue.create name=%s by=%s", body.name, admin.user)
    return QueueView(**rec.to_dict())


@router.delete("/queues/{name}", status_code=204)
def delete_queue(name: str, admin: Identity = Depends(require_admin)) -> None:
    if not get_queue_store().remove_queue(name):
        raise HTTPException(404, detail="queue not found")
