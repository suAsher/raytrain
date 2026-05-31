"""
Bridge between the web console's platform job records and the real Ray
submission path.

Responsibilities
----------------
1. **Real submit**: when the job's gpu_type maps to a configured shared
   RayCluster (``settings.shared_clusters``), actually submit a RayJob via
   :class:`RayClusterClient` — code-as-submission (``working_dir`` = the code
   zip in MinIO) + Ray Data/Lance env injection. Store the real
   ``submission_id`` + cluster address on the platform record.
2. **Graceful degrade**: if no cluster is configured for that gpu_type (e.g.
   local dev), keep the platform record as a queued demo job — nothing else
   changes, the UI still works.
3. **Status reconcile**: map Ray job status → console status, so the console's
   list/detail reflect live cluster state.
4. **Logs**: tail real Ray logs when the job is live.

Lance / Ray Data
----------------
A selected dataset's URI is injected as ``RAYTRAIN_DATA_SOURCE_URI`` (+ filter/
version/columns). Training code calls ``raytrain.data.auto_dataset()`` which
reads it via ``ray.data.read_lance()`` from MinIO — real Ray Data + Lance.
"""
from __future__ import annotations

import logging
from typing import Optional

from .jobs_store import PlatformJob, get_job_store
from .ray_client import JobSubmissionSpec, RayClusterClient, make_submission_id
from .settings import Settings, get_settings

log = logging.getLogger(__name__)


# Ray JobStatus (string) → console status.
_RAY_TO_CONSOLE = {
    "PENDING": "Queued",
    "RUNNING": "Running",
    "SUCCEEDED": "Succeeded",
    "FAILED": "Failed",
    "STOPPED": "Cancelled",
}


def ray_status_to_console(ray_status: str) -> str:
    return _RAY_TO_CONSOLE.get(str(ray_status).upper(), "Queued")


def cluster_configured(gpu_type: str, settings: Settings) -> bool:
    return gpu_type.lower() in settings.shared_clusters


def _data_source_env(job: PlatformJob, dataset_uri: str) -> dict[str, str]:
    """Env vars that make Ray Data/Lance work for the training subprocess."""
    if not dataset_uri:
        return {}
    return {"RAYTRAIN_DATA_SOURCE_URI": dataset_uri}


class SubmissionService:
    """Owns the console↔Ray bridge. Inject a RayClusterClient for tests."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        ray: Optional[RayClusterClient] = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._ray = ray or RayClusterClient(settings=self._settings)

    # -- submit ---------------------------------------------------------------

    def submit(self, job: PlatformJob, *, dataset_uri: str = "", code_uri: str = "") -> PlatformJob:
        """Submit ``job`` to the real cluster if possible; update its record.

        Returns the (possibly updated) job. Never raises for a missing cluster
        — that path just leaves the job as a queued platform record.
        """
        gpu_type = job.resources.gpu_type
        store = get_job_store()

        if not cluster_configured(gpu_type, self._settings):
            log.info(
                "submission.record_only job=%s gpu_type=%s (no shared cluster configured)",
                job.id, gpu_type,
            )
            return job

        gt = gpu_type.lower()
        spec = JobSubmissionSpec(
            user=job.user,
            tenant=job.tenant,
            gpu_type=gt,
            num_nodes=job.resources.nodes,
            gpus_per_node=job.resources.gpus_per_node,
            entrypoint=job.entrypoint,
            code_uri=code_uri or None,
            extra_env={
                **{k: str(v) for k, v in job.env.items()},
                **_data_source_env(job, dataset_uri),
            },
            metadata={
                "raytrain.user": job.user,
                "raytrain.project": job.project,
                "raytrain.console_job_id": job.id,
                "raytrain.queue": job.queue,
                "raytrain.gpus_total": str(job.resources.total_gpu),
            },
        )
        submission_id = make_submission_id(job.user, job.project, job.name)
        try:
            sid = self._ray.submit_job(spec, submission_id=submission_id, repo=job.project)
            address = self._ray.address_for(gt)
        except Exception as exc:  # noqa: BLE001 — surface but don't crash submit
            log.warning("submission.ray_failed job=%s err=%r", job.id, exc)
            store.update(job.id, status="Failed")
            from .jobs_store import FailureInfo

            store.update(
                job.id,
                failure=FailureInfo(
                    category="SubmitError",
                    summary="提交到 Ray 集群失败",
                    detail=f"{exc!r}",
                    container="submitter",
                ),
            )
            return store.get(job.id)

        store.update(job.id, submission_id=sid, code_uri=code_uri, status="Starting")
        log.info("submission.submitted job=%s submission_id=%s cluster=%s", job.id, sid, address)
        return store.get(job.id)

    # -- reconcile ------------------------------------------------------------

    def reconcile(self, job: PlatformJob) -> PlatformJob:
        """Refresh a live job's status from Ray (best-effort, no-op otherwise)."""
        if not job.submission_id:
            return job
        gpu_type = job.resources.gpu_type
        if not cluster_configured(gpu_type, self._settings):
            return job
        if job.status in ("Succeeded", "Failed", "Cancelled"):
            return job  # terminal; don't re-poll
        try:
            ray_status = self._ray.get_status(gpu_type.lower(), job.submission_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("reconcile.skip job=%s err=%r", job.id, exc)
            return job
        new_status = ray_status_to_console(ray_status)
        if new_status != job.status:
            get_job_store().update(job.id, status=new_status)
            return get_job_store().get(job.id)
        return job

    # -- logs -----------------------------------------------------------------

    def can_stream_logs(self, job: PlatformJob) -> bool:
        return bool(job.submission_id) and cluster_configured(
            job.resources.gpu_type, self._settings
        )

    def tail_logs(self, job: PlatformJob):
        return self._ray.tail_logs(job.resources.gpu_type.lower(), job.submission_id)

    def stop(self, job: PlatformJob) -> None:
        if self.can_stream_logs(job):
            try:
                self._ray.stop(job.resources.gpu_type.lower(), job.submission_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("submission.stop_failed job=%s err=%r", job.id, exc)


# module-level singleton + injection seam (mirrors the rest of core)
_service: Optional[SubmissionService] = None


def get_submission_service() -> SubmissionService:
    global _service
    if _service is None:
        _service = SubmissionService()
    return _service


def set_submission_service(s: SubmissionService) -> None:
    global _service
    _service = s
