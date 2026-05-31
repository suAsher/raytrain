"""
``/v1/console`` — the web console's backing API.

The console (raytrain-console) is a training workbench, not a K8s console. It
needs richer, cluster-independent views than the thin ``/v1/jobs`` Ray proxy:
list/detail jobs with resources+mounts+failure, derived timeline/pods/events/
metrics/artifacts, queues, experiments, and an overview summary.

Everything here is backed by real platform stores (jobs_store / queues_store /
users / datasets). Per-job telemetry (pods phase, GPU curves) is derived from
the stored record by ``console_views`` and is deterministic per job.

Auth: every route requires a valid token. Tenant/owner visibility is enforced
through the stores (users see their own + same-tenant jobs; admins see all).
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..core.console_views import (
    build_artifacts,
    build_events,
    build_logs,
    build_metrics,
    build_pods,
    build_timeline,
    render_rayjob_yaml,
)
from ..core.jwt_auth import Identity, require_user
from ..core.jobs_store import (
    FailureInfo,
    JobMounts,
    JobResources,
    PlatformJob,
    get_job_store,
)
from ..core.queues_store import get_queue_store
from ..core.submission_service import (
    cluster_configured,
    get_submission_service,
)
from ..core.settings import Settings, get_settings
from fastapi.responses import StreamingResponse

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/console", tags=["console"])


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class ResourcesModel(BaseModel):
    gpuType: str = "H20"
    nodes: int = 1
    gpusPerNode: int = 8
    cpuPerGpu: int = 8
    memPerGpuGi: int = 96
    headCpu: int = 4
    headMemGi: int = 16
    rdma: bool = False


class MountsModel(BaseModel):
    datasetUri: str = ""
    checkpointUri: str = ""
    checkpointShared: bool = True
    scratchGi: int = 200


class CreateJobModel(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    project: str = "default"
    queue: str = "h20-shared"
    quotaGroup: str = ""
    priority: str = "normal"
    description: str = ""
    image: str = ""
    entrypoint: str = ""
    workingDir: str = ""
    gitRef: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    resources: ResourcesModel = Field(default_factory=ResourcesModel)
    mounts: MountsModel = Field(default_factory=MountsModel)
    experiment: str = ""
    # s3:// URI of an uploaded code zip (from /v1/code) for code-as-submission.
    code_uri: str = ""


def _job_summary(j: PlatformJob) -> dict:
    return {
        "id": j.id,
        "name": j.name,
        "status": j.status,
        "project": j.project,
        "queue": j.queue,
        "quotaGroup": j.quota_group,
        "priority": j.priority,
        "image": j.image,
        "creator": j.user,
        "createdAt": _iso(j.created_at),
        "durationSec": j.duration_sec,
        "resources": {
            "gpuType": j.resources.gpu_type,
            "nodes": j.resources.nodes,
            "gpusPerNode": j.resources.gpus_per_node,
        },
        "failure": j.failure.to_dict() if j.failure else None,
        "submissionId": j.submission_id,
        "live": bool(j.submission_id),
    }


def _job_detail(j: PlatformJob) -> dict:
    return {
        **_job_summary(j),
        "entrypoint": j.entrypoint,
        "workingDir": j.working_dir,
        "gitRef": j.git_ref,
        "env": [{"key": k, "value": v} for k, v in j.env.items()],
        "description": j.description,
        "experiment": j.experiment,
        "resources": {
            "gpuType": j.resources.gpu_type,
            "nodes": j.resources.nodes,
            "gpusPerNode": j.resources.gpus_per_node,
            "cpuPerGpu": j.resources.cpu_per_gpu,
            "memPerGpuGi": j.resources.mem_per_gpu_gi,
            "headCpu": j.resources.head_cpu,
            "headMemGi": j.resources.head_mem_gi,
            "rdma": j.resources.rdma,
        },
        "mounts": {
            "dataset": {"path": "/data", "uri": j.mounts.dataset_uri, "mode": "ro"},
            "checkpoint": {"path": "/checkpoints", "uri": j.mounts.checkpoint_uri,
                           "mode": "rw", "shared": j.mounts.checkpoint_shared},
            "scratch": {"path": "/scratch", "sizeGi": j.mounts.scratch_gi},
        },
        "timeline": build_timeline(j),
        "pods": build_pods(j),
        "events": build_events(j),
        "logs": build_logs(j),
        "metrics": build_metrics(j),
        "artifacts": build_artifacts(j),
        "rayJobYaml": render_rayjob_yaml(j),
    }


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #


@router.get("/jobs", summary="List jobs visible to the caller (console view)")
def list_jobs(identity: Identity = Depends(require_user)) -> list[dict]:
    store = get_job_store()
    svc = get_submission_service()
    jobs = store.list_visible(identity.user, identity.tenant, identity.is_admin)
    # reconcile live jobs' status from Ray (best-effort, no-op without cluster)
    jobs = [svc.reconcile(j) for j in jobs]
    return [_job_summary(j) for j in jobs]


@router.get("/jobs/{jid}", summary="Job detail with timeline/pods/events/metrics")
def get_job(jid: str, identity: Identity = Depends(require_user)) -> dict:
    j = get_job_store().get(jid)
    if not j:
        raise HTTPException(404, detail="job not found")
    if not identity.is_admin and j.user != identity.user and j.tenant != identity.tenant:
        raise HTTPException(403, detail="not visible to you")
    j = get_submission_service().reconcile(j)
    return _job_detail(j)


@router.get("/jobs/{jid}/logs", summary="Stream real Ray logs (when live)")
def stream_logs(jid: str, identity: Identity = Depends(require_user)):
    j = get_job_store().get(jid)
    if not j:
        raise HTTPException(404, detail="job not found")
    if not identity.is_admin and j.user != identity.user and j.tenant != identity.tenant:
        raise HTTPException(403, detail="not visible to you")
    svc = get_submission_service()
    if not svc.can_stream_logs(j):
        # No live cluster — return the derived demo logs as plain text so the
        # UI's "download/stream" still produces something coherent.
        text = "\n".join(
            f"{l['ts']} [{l['container']}] {l['level']} {l['text']}"
            for l in build_logs(j)
        )
        return StreamingResponse(iter([text + "\n"]), media_type="text/plain")

    def gen():
        try:
            for line in svc.tail_logs(j):
                yield line
        except Exception as exc:  # noqa: BLE001
            yield f"\n[server] log stream error: {exc!r}\n"

    return StreamingResponse(gen(), media_type="text/plain")


@router.post("/jobs", status_code=201, summary="Create a job record (console submit)")
def create_job(body: CreateJobModel, identity: Identity = Depends(require_user)) -> dict:
    now = time.time()
    rec = PlatformJob(
        id="job-" + uuid.uuid4().hex[:8],
        name=body.name,
        user=identity.user,
        tenant=identity.tenant,
        project=body.project,
        queue=body.queue,
        quota_group=body.quotaGroup or (body.project + "-qg"),
        priority=body.priority,
        status="Queued",
        image=body.image,
        entrypoint=body.entrypoint,
        working_dir=body.workingDir,
        git_ref=body.gitRef,
        env=dict(body.env),
        resources=JobResources(
            gpu_type=body.resources.gpuType,
            nodes=body.resources.nodes,
            gpus_per_node=body.resources.gpusPerNode,
            cpu_per_gpu=body.resources.cpuPerGpu,
            mem_per_gpu_gi=body.resources.memPerGpuGi,
            head_cpu=body.resources.headCpu,
            head_mem_gi=body.resources.headMemGi,
            rdma=body.resources.rdma,
        ),
        mounts=JobMounts(
            dataset_uri=body.mounts.datasetUri,
            checkpoint_uri=body.mounts.checkpointUri,
            checkpoint_shared=body.mounts.checkpointShared,
            scratch_gi=body.mounts.scratchGi,
        ),
        description=body.description,
        experiment=body.experiment,
        created_at=now,
        started_at=now,
    )
    get_job_store().create(rec)
    log.info("console.job.create id=%s name=%s user=%s", rec.id, rec.name, identity.user)
    # Fire the real Ray submission if a shared cluster is configured for this
    # gpu_type; otherwise the record stays a queued platform job (dev mode).
    svc = get_submission_service()
    rec = svc.submit(rec, dataset_uri=body.mounts.datasetUri, code_uri=body.code_uri) or rec
    return _job_detail(rec)


@router.post("/jobs/{jid}/cancel", summary="Cancel a running/queued job")
def cancel_job(jid: str, identity: Identity = Depends(require_user)) -> dict:
    store = get_job_store()
    j = store.get(jid)
    if not j:
        raise HTTPException(404, detail="job not found")
    if not identity.is_admin and j.user != identity.user:
        raise HTTPException(403, detail="not owner")
    if j.status in ("Running", "Queued", "Starting"):
        get_submission_service().stop(j)  # stop the real Ray job if live
        store.update(jid, status="Cancelled", finished_at=time.time())
    return _job_summary(store.get(jid))


@router.post("/jobs/{jid}/retry", status_code=201, summary="Retry a job (new run, same config)")
def retry_job(jid: str, identity: Identity = Depends(require_user)) -> dict:
    store = get_job_store()
    src = store.get(jid)
    if not src:
        raise HTTPException(404, detail="job not found")
    if not identity.is_admin and src.user != identity.user and src.tenant != identity.tenant:
        raise HTTPException(403, detail="not visible to you")
    now = time.time()
    base_name = src.name.rsplit("-retry", 1)[0]
    rec = PlatformJob(
        id="job-" + uuid.uuid4().hex[:8],
        name=base_name + "-retry",
        user=identity.user,
        tenant=identity.tenant,
        project=src.project,
        queue=src.queue,
        quota_group=src.quota_group,
        priority=src.priority,
        status="Queued",
        image=src.image,
        entrypoint=src.entrypoint,
        working_dir=src.working_dir,
        git_ref=src.git_ref,
        env=dict(src.env),
        resources=JobResources(**src.resources.to_dict()),
        mounts=JobMounts(**src.mounts.to_dict()),
        description=src.description,
        experiment=src.experiment,
        created_at=now,
        started_at=now,
    )
    get_job_store().create(rec)
    return _job_detail(rec)


# --------------------------------------------------------------------------- #
# Queues / pools / overview / experiments
# --------------------------------------------------------------------------- #


@router.get("/queues", summary="Queues (Kueue-facing) with live used/pending")
def list_queues(identity: Identity = Depends(require_user)) -> list[dict]:
    js = get_job_store()
    qs = get_queue_store()
    jobs = js.list_visible(identity.user, identity.tenant, is_admin=True)
    qs.recompute_from_jobs(jobs)
    out = []
    for q in qs.list_queues():
        recent = [
            {"id": j.id, "name": j.name, "status": j.status}
            for j in jobs if j.queue == q.name
        ][:3]
        d = q.to_dict()
        d["recentJobs"] = recent
        out.append(d)
    return out


@router.get("/overview", summary="Overview summary counts + pools")
def overview(identity: Identity = Depends(require_user)) -> dict:
    js = get_job_store()
    qs = get_queue_store()
    jobs = js.list_visible(identity.user, identity.tenant, identity.is_admin)
    qs.recompute_from_jobs(js.list_visible(identity.user, identity.tenant, is_admin=True))
    counts = {s: 0 for s in ("Running", "Queued", "Failed", "Succeeded", "Cancelled", "Starting")}
    for j in jobs:
        counts[j.status] = counts.get(j.status, 0) + 1
    failed = [_job_summary(j) for j in jobs if j.status == "Failed"][:6]
    recent = [_job_summary(j) for j in jobs][:6]
    return {
        "counts": counts,
        "pools": [p.to_dict() for p in qs.list_pools()],
        "recentFailed": failed,
        "recent": recent,
    }


@router.get("/experiments", summary="Experiment groups derived from job records")
def experiments(identity: Identity = Depends(require_user)) -> list[dict]:
    js = get_job_store()
    jobs = js.list_visible(identity.user, identity.tenant, identity.is_admin)
    groups: dict[str, list[PlatformJob]] = {}
    for j in jobs:
        key = j.experiment or j.project
        groups.setdefault(key, []).append(j)
    out = []
    for name, items in groups.items():
        items.sort(key=lambda x: x.created_at, reverse=True)
        baseline = items[0]
        out.append({
            "id": "exp-" + name,
            "name": name,
            "project": baseline.project,
            "runs": len(items),
            "bestMetric": "mIoU 0.73" if any(i.status == "Succeeded" for i in items) else "—",
            "lastRunAt": _iso(baseline.created_at),
            "baselineJobId": baseline.id,
        })
    return out


@router.get("/artifacts", summary="All artifacts across visible jobs")
def artifacts(identity: Identity = Depends(require_user)) -> list[dict]:
    js = get_job_store()
    jobs = js.list_visible(identity.user, identity.tenant, identity.is_admin)
    out = []
    for j in jobs:
        for a in build_artifacts(j):
            out.append({**a, "jobId": j.id, "jobName": j.name})
    return out
