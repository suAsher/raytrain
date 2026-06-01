"""
Shared platform access control: account-enabled check + grant-based authorization
+ per-user quota enforcement.

Why this module exists
----------------------
Two entry points create training workload (`/v1/jobs` CLI proxy and
`/v1/console/jobs` web console). Quota/authz logic must be IDENTICAL on both, so
it lives here once (DRY) and both call it. Pure-ish: takes the user record +
current usage + the ask, returns a typed decision; the API layer maps the
decision to a FriendlyError.

What it enforces (in order):
  1. account enabled        — a disabled user cannot submit (even with a valid JWT)
  2. grants                 — project / queue / image-prefix must be in the user's grants
  3. quota                  — gpus / jobs / cpus / memory within the user's caps
Admins bypass grants + quota (but a disabled admin is still blocked).
"""
from __future__ import annotations

from dataclasses import dataclass

from .quota import ResourceAsk, Usage, check_quota
from .users import UserRecord


class AccessDenied(Exception):
    """Raised when a submission is not permitted. ``code`` maps to a Friendly code."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


@dataclass
class SubmitAsk:
    """What a single submission wants, for authz + quota checks."""

    project: str = ""
    queue: str = ""
    image: str = ""
    gpus: int = 0
    cpus: int = 0
    memory_gi: int = 0


def _granted(value: str, grants: list[str]) -> bool:
    """A user may use ``value`` if they have no restriction (empty grant list)
    or the value is explicitly listed. Empty grants = unrestricted (default for
    a freshly-created user; admins tighten by setting grants)."""
    if not grants:
        return True
    return value in grants


def _image_allowed(image: str, prefixes: list[str]) -> bool:
    if not prefixes:
        return True
    return any(image.startswith(p) for p in prefixes)


def enforce_submit(
    rec: UserRecord | None,
    ask: SubmitAsk,
    *,
    current_gpus: int,
    current_jobs: int,
    current_cpus: int = 0,
    current_memory_gi: int = 0,
) -> None:
    """Raise :class:`AccessDenied` if the submission isn't permitted.

    ``rec is None`` means the platform has no user table yet (fresh bootstrap):
    we don't block — caps are applied once an admin creates the user. This keeps
    a zero-config dev platform usable while staying safe in production where the
    bootstrap admin creates real users.
    """
    if rec is None:
        return

    # 1. account enabled — applies to everyone, including admins
    if not rec.enabled:
        raise AccessDenied(
            "ACCOUNT_DISABLED", "账号已被禁用，请联系管理员",
        )

    # admins bypass grants + quota
    if rec.is_admin:
        return

    # 2. grants
    if ask.project and not _granted(ask.project, rec.projects):
        raise AccessDenied(
            "PROJECT_FORBIDDEN", f"无权使用项目 {ask.project!r}",
            hint=f"已授权项目：{', '.join(rec.projects) or '（无）'}",
        )
    if ask.queue and not _granted(ask.queue, rec.queues):
        raise AccessDenied(
            "QUEUE_FORBIDDEN", f"无权使用队列 {ask.queue!r}",
            hint=f"已授权队列：{', '.join(rec.queues) or '（无）'}",
        )
    if ask.image and not _image_allowed(ask.image, rec.image_prefixes):
        raise AccessDenied(
            "IMAGE_FORBIDDEN", f"镜像 {ask.image!r} 不在允许的前缀内",
            hint=f"允许的镜像前缀：{', '.join(rec.image_prefixes) or '（无）'}",
        )

    # 3. quota
    violation = check_quota(
        rec.quota,
        Usage(gpus=current_gpus, jobs=current_jobs,
              cpus=current_cpus, memory_gi=current_memory_gi),
        ResourceAsk(gpus=ask.gpus, cpus=ask.cpus,
                    memory_gi=ask.memory_gi, jobs=1),
    )
    if violation is not None:
        raise AccessDenied("QUOTA_EXCEEDED", violation.message)
