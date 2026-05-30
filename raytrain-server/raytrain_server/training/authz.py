"""
Authorization for the training workbench.

Pure decision functions (per spec 权限和治理):
    - normal user: only own jobs
    - project admin: jobs in their project
    - quota-group admin: jobs in their quota group
    - platform admin: everything
    - before create: must be allowed to use project / queue / quota_group /
      dataset / image

These take a Principal (resolved from the JWT + role bindings) and return
bool / raise PlatformError. No DB, no FastAPI — easy to unit test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from . import errors as E
from .domain import TrainingJob


class Role(str, Enum):
    USER = "user"
    PROJECT_ADMIN = "project_admin"
    QUOTA_ADMIN = "quota_admin"
    PLATFORM_ADMIN = "platform_admin"


@dataclass
class Principal:
    """The authenticated caller + their grants."""

    user_id: str
    user: str
    role: Role = Role.USER
    # which projects / quota groups / queues / datasets / images this user may use
    projects: set[str] = field(default_factory=set)
    quota_groups: set[str] = field(default_factory=set)
    queues: set[str] = field(default_factory=set)
    datasets: set[str] = field(default_factory=set)
    image_prefixes: list[str] = field(default_factory=list)
    # for admins: which scopes they can observe
    admin_projects: set[str] = field(default_factory=set)
    admin_quota_groups: set[str] = field(default_factory=set)

    @property
    def is_platform_admin(self) -> bool:
        return self.role == Role.PLATFORM_ADMIN


# --------------------------------------------------------------------------- #
# visibility (list / get)
# --------------------------------------------------------------------------- #


def can_view_job(
    principal: Principal,
    *,
    job_creator_id: str,
    job_project: str,
    job_quota_group: str,
) -> bool:
    if principal.is_platform_admin:
        return True
    if job_creator_id == principal.user_id:
        return True
    if job_project in principal.admin_projects:
        return True
    if job_quota_group in principal.admin_quota_groups:
        return True
    return False


# --------------------------------------------------------------------------- #
# create-time authorization
# --------------------------------------------------------------------------- #


def authorize_create(principal: Principal, job: TrainingJob) -> None:
    """Raise PlatformError if the caller may not create this job."""
    if principal.is_platform_admin:
        return  # platform admin bypasses scope checks

    if job.project not in principal.projects:
        raise E.PlatformError(
            code=E.ERR_PROJECT_FORBIDDEN,
            message=f"你没有项目 {job.project} 的使用权限",
            details={"project": job.project},
        )
    if job.quota_group not in principal.quota_groups:
        raise E.PlatformError(
            code=E.ERR_QUOTA_EXCEEDED,
            message=f"你没有配额组 {job.quota_group} 的使用权限",
            details={"quota_group": job.quota_group},
        )
    if job.queue not in principal.queues:
        raise E.PlatformError(
            code=E.ERR_QUEUE_FORBIDDEN,
            message=f"你没有队列 {job.queue} 的使用权限",
            details={"queue": job.queue},
        )
    # dataset grants
    for ds in job.datasets:
        if principal.datasets and ds.name not in principal.datasets:
            raise E.PlatformError(
                code=E.ERR_DATASET_FORBIDDEN,
                message=f"你没有数据集 {ds.name} 的访问权限",
                details={"dataset": ds.name},
            )
    # image allowlist (if the principal has one)
    if principal.image_prefixes and not any(
        job.image.startswith(p) for p in principal.image_prefixes
    ):
        raise E.PlatformError(
            code=E.ERR_IMAGE_NOT_ALLOWED,
            message=f"你没有镜像 {job.image} 的使用权限",
            details={"image": job.image},
        )
