"""
``/v1/admin/users`` — admin-only user lifecycle + grants/quota management.

This is how an admin "creates a user with permissions, and updates them later"
(per product decision). Quota is per single user; grants + quota are persisted
in the DB so changes take effect immediately (the JWT only carries identity).

Also exposes ``GET /v1/quota`` (any authenticated user) so a user can see their
own caps + current usage in the UI.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..core.jwt_auth import Identity, issue_token, require_admin, require_user
from ..core.quota import Usage
from ..core.settings import Settings, get_settings
from ..core.store import get_devsession_store
from ..core.users import UserQuota, UserRecord, get_user_store

log = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])


# ---------------------------------------------------------------------------- #
# Schemas
# ---------------------------------------------------------------------------- #


class QuotaModel(BaseModel):
    max_gpus: int = 0
    max_jobs: int = 0
    max_cpus: int = 0
    max_memory_gi: int = 0

    def to_quota(self) -> UserQuota:
        return UserQuota(
            max_gpus=self.max_gpus,
            max_jobs=self.max_jobs,
            max_cpus=self.max_cpus,
            max_memory_gi=self.max_memory_gi,
        )


class CreateUserRequest(BaseModel):
    user: str = Field(..., min_length=1, max_length=64)
    tenant: str = "default"
    role: str = Field("user", pattern="^(user|admin)$")
    quota: QuotaModel = Field(default_factory=QuotaModel)
    projects: list[str] = Field(default_factory=list)
    queues: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=list)
    image_prefixes: list[str] = Field(default_factory=list)
    # Optionally mint a token in the same call so the admin can hand it over.
    issue_token: bool = False
    token_days: Optional[int] = None


class UpdateUserRequest(BaseModel):
    """All fields optional — only provided ones are changed (PATCH semantics)."""

    tenant: Optional[str] = None
    role: Optional[str] = Field(None, pattern="^(user|admin)$")
    quota: Optional[QuotaModel] = None
    projects: Optional[list[str]] = None
    queues: Optional[list[str]] = None
    datasets: Optional[list[str]] = None
    image_prefixes: Optional[list[str]] = None
    enabled: Optional[bool] = None


class UserView(BaseModel):
    user: str
    tenant: str
    role: str
    quota: QuotaModel
    projects: list[str]
    queues: list[str]
    datasets: list[str]
    image_prefixes: list[str]
    enabled: bool
    created_at: float
    updated_at: float


class CreateUserResponse(BaseModel):
    user: UserView
    token: Optional[str] = None
    token_expires_at: Optional[int] = None


def _view(rec: UserRecord) -> UserView:
    return UserView(
        user=rec.user,
        tenant=rec.tenant,
        role=rec.role,
        quota=QuotaModel(**rec.quota.to_dict()),
        projects=list(rec.projects),
        queues=list(rec.queues),
        datasets=list(rec.datasets),
        image_prefixes=list(rec.image_prefixes),
        enabled=rec.enabled,
        created_at=rec.created_at,
        updated_at=rec.updated_at,
    )


# ---------------------------------------------------------------------------- #
# Admin endpoints
# ---------------------------------------------------------------------------- #


@router.post(
    "/v1/admin/users",
    response_model=CreateUserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user with grants + per-user quota (admin only)",
)
def create_user(
    body: CreateUserRequest,
    admin: Identity = Depends(require_admin),
    settings: Settings = Depends(get_settings),
) -> CreateUserResponse:
    store = get_user_store()
    rec = UserRecord(
        user=body.user,
        tenant=body.tenant,
        role=body.role,
        quota=body.quota.to_quota(),
        projects=body.projects,
        queues=body.queues,
        datasets=body.datasets,
        image_prefixes=body.image_prefixes,
        created_at=time.time(),
        updated_at=time.time(),
    )
    try:
        store.create(rec)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    token = None
    exp = None
    if body.issue_token:
        try:
            token, exp = issue_token(
                user=rec.user, tenant=rec.tenant, role=rec.role,
                ttl_days=body.token_days, settings=settings,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    log.info("admin.user.create user=%s by=%s", rec.user, admin.user)
    return CreateUserResponse(user=_view(rec), token=token, token_expires_at=exp)


@router.get(
    "/v1/admin/users",
    response_model=list[UserView],
    summary="List all users (admin only)",
)
def list_users(admin: Identity = Depends(require_admin)) -> list[UserView]:
    return [_view(r) for r in get_user_store().list_all()]


@router.get(
    "/v1/admin/users/{user}",
    response_model=UserView,
    summary="Get a user (admin only)",
)
def get_user(user: str, admin: Identity = Depends(require_admin)) -> UserView:
    rec = get_user_store().get(user)
    if not rec:
        raise HTTPException(status_code=404, detail=f"user {user!r} not found")
    return _view(rec)


@router.patch(
    "/v1/admin/users/{user}",
    response_model=UserView,
    summary="Update a user's grants / quota / role (admin only)",
)
def update_user(
    user: str,
    body: UpdateUserRequest,
    admin: Identity = Depends(require_admin),
) -> UserView:
    store = get_user_store()
    if store.get(user) is None:
        raise HTTPException(status_code=404, detail=f"user {user!r} not found")

    changes: dict = {}
    if body.tenant is not None:
        changes["tenant"] = body.tenant
    if body.role is not None:
        changes["role"] = body.role
    if body.quota is not None:
        changes["quota"] = body.quota.to_quota()
    if body.projects is not None:
        changes["projects"] = body.projects
    if body.queues is not None:
        changes["queues"] = body.queues
    if body.datasets is not None:
        changes["datasets"] = body.datasets
    if body.image_prefixes is not None:
        changes["image_prefixes"] = body.image_prefixes
    if body.enabled is not None:
        changes["enabled"] = body.enabled

    rec = store.update(user, **changes)
    log.info("admin.user.update user=%s by=%s fields=%s",
             user, admin.user, sorted(changes))
    return _view(rec)


@router.delete(
    "/v1/admin/users/{user}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a user (admin only)",
)
def delete_user(user: str, admin: Identity = Depends(require_admin)) -> None:
    if not get_user_store().delete(user):
        raise HTTPException(status_code=404, detail=f"user {user!r} not found")
    log.info("admin.user.delete user=%s by=%s", user, admin.user)


# ---------------------------------------------------------------------------- #
# Self-service: see my own quota + usage
# ---------------------------------------------------------------------------- #


class MyQuotaView(BaseModel):
    user: str
    quota: QuotaModel
    usage: dict
    # convenience: remaining per resource (None = unlimited)
    remaining: dict


@router.get(
    "/v1/quota",
    response_model=MyQuotaView,
    summary="Show the caller's own quota + current usage",
)
def my_quota(identity: Identity = Depends(require_user)) -> MyQuotaView:
    rec = get_user_store().get(identity.user)
    quota = rec.quota if rec else UserQuota()

    # current GPU usage from running/creating dev-sessions (jobs usage is added
    # once the jobs store lands; dev-sessions already track GPUs per user).
    dev = get_devsession_store()
    gpu_used = dev.gpu_in_use_by_user(identity.user)
    usage = Usage(gpus=gpu_used)

    def _rem(cap: int, used: int):
        if cap is None or cap <= 0:
            return None  # unlimited / unset
        return max(0, cap - used)

    return MyQuotaView(
        user=identity.user,
        quota=QuotaModel(**quota.to_dict()),
        usage={"gpus": usage.gpus, "cpus": usage.cpus,
               "memory_gi": usage.memory_gi, "jobs": usage.jobs},
        remaining={
            "gpus": _rem(quota.max_gpus, usage.gpus),
            "cpus": _rem(quota.max_cpus, usage.cpus),
            "memory_gi": _rem(quota.max_memory_gi, usage.memory_gi),
            "jobs": _rem(quota.max_jobs, usage.jobs),
        },
    )
