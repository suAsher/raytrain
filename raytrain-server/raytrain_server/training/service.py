"""
Training service: orchestrates authz -> validate -> render -> (dry-run | submit).

Pure-ish: takes injectable collaborators (kube applier, audit) so it can be
unit-tested. The API layer (api/training.py) is a thin shell over this.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import errors as E
from .authz import Principal, authorize_create
from .domain import JobState, TrainingJob
from .renderer import render_rayjob
from .validate import QuotaView, validate_all


@dataclass
class SubmitResult:
    job_id: str
    run_id: str
    state: str
    rayjob: dict[str, Any]
    dry_run: bool


# applier: takes (rayjob_dict, namespace) -> None ; returns nothing or raises
KubeApplier = Callable[[dict, str], None]
# audit: (user, action, resource, result, detail) -> None
AuditFn = Callable[..., None]


class TrainingService:
    def __init__(
        self,
        applier: Optional[KubeApplier] = None,
        audit: Optional[AuditFn] = None,
        allowed_images: Optional[list[str]] = None,
    ) -> None:
        self._apply = applier
        self._audit = audit or (lambda **kw: None)
        self._allowed_images = allowed_images

    def _new_ids(self, job: TrainingJob) -> None:
        if not job.job_id:
            job.job_id = f"{job.creator}-{job.name}-{uuid.uuid4().hex[:8]}".lower()
        if not job.run_id:
            job.run_id = f"{job.job_id}-r0"

    def create(
        self,
        principal: Principal,
        job: TrainingJob,
        quota: QuotaView | None = None,
        dry_run: bool = False,
    ) -> SubmitResult:
        # 1. authz (scope grants)
        authorize_create(principal, job)
        # 2. assign ids
        self._new_ids(job)
        # stamp creator from the principal (never trust client)
        job.creator = principal.user
        job.creator_id = principal.user_id
        # 3. validate (reserved labels, multi-node ckpt, quota, image)
        validate_all(job, quota=quota, allowed_images=self._allowed_images)
        # 4. render
        rayjob = render_rayjob(job)

        if dry_run:
            self._audit(
                user=principal.user, action="dry_run_job",
                resource=job.job_id, result="ok",
                detail=f"{job.resources.total_gpus} gpu",
            )
            return SubmitResult(
                job_id=job.job_id, run_id=job.run_id,
                state=JobState.DRAFT.value, rayjob=rayjob, dry_run=True,
            )

        # 5. submit
        if self._apply is None:
            raise E.PlatformError(
                code="NO_APPLIER",
                message="服务端未配置 Kubernetes applier，无法真正提交",
            )
        try:
            self._apply(rayjob, job.namespace)
        except Exception as exc:  # noqa: BLE001
            self._audit(
                user=principal.user, action="submit_job",
                resource=job.job_id, result="error", detail=repr(exc),
            )
            raise E.PlatformError(
                code="SUBMIT_FAILED",
                message=f"提交 RayJob 失败: {exc!r}",
            ) from exc

        self._audit(
            user=principal.user, action="submit_job",
            resource=job.job_id, result="ok",
            detail=f"{job.resources.total_gpus} gpu on {job.resources.gpu_type}",
        )
        return SubmitResult(
            job_id=job.job_id, run_id=job.run_id,
            state=JobState.SUBMITTED.value, rayjob=rayjob, dry_run=False,
        )
