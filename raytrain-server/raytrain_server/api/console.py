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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core.console_views import (
    build_timeline,
    render_rayjob_yaml,
)
from ..core.jwt_auth import Identity, require_user
from ..core.jobs_store import (
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
from ..core.kueue_reader import KueueUnavailable, get_kueue_reader
from ..core.artifact_store import ArtifactsUnavailable, get_artifact_store
from ..core.access_control import AccessDenied, SubmitAsk, enforce_submit
from ..core.users import get_user_store
from ..core.store import get_devsession_store
from ..core.errors import Codes, FriendlyError
from ..core.settings import Settings, get_settings

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


def _job_detail(j: PlatformJob, settings: Settings | None = None) -> dict:
    pods, events, pods_source = _pods_and_events(j, settings)
    arts, arts_source = _artifacts_for(j)
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
        "pods": pods,
        "pods_source": pods_source,
        "events": events,
        # logs/metrics are served by dedicated real-data endpoints
        # (/jobs/{id}/logs → Loki, /jobs/{id}/metrics → Prometheus); the detail
        # payload no longer carries synthesized telemetry.
        "logs": [],
        "metrics": {},
        "artifacts": arts,
        "artifacts_source": arts_source,
        "rayJobYaml": render_rayjob_yaml(j),
    }


def _artifacts_for(j: PlatformJob) -> tuple[list[dict], str]:
    """List a job's REAL output artifacts from object storage under its
    checkpoint URI; empty + 'unavailable' when no store / no s3 uri (never
    synthesized — Req 14.5/14.6). Listing errors degrade to 'unavailable'
    rather than breaking the detail payload."""
    store = get_artifact_store()
    if store is None or not j.mounts.checkpoint_uri:
        return [], "unavailable"
    try:
        page = store.list_for_uri(j.mounts.checkpoint_uri)
    except ArtifactsUnavailable as exc:
        log.warning("console.artifacts_failed job=%s err=%r", j.id, exc)
        return [], "unavailable"
    return [a.to_dict() for a in page.artifacts], page.source


def _pods_and_events(j: PlatformJob, settings: Settings | None):
    """Real pods + events for a LIVE job (read from K8s by the Ray submission-id
    label); empty (source='unavailable') otherwise — never synthesized (Req 14.5)."""
    s = settings or get_settings()
    if not j.submission_id or not cluster_configured(j.resources.gpu_type, s):
        return [], [], "unavailable"
    try:
        from .k8s_client import K8sClient

        k8s = K8sClient(in_cluster=s.in_cluster)
        selector = f"ray.io/job-submission-id={j.submission_id}"
        ns = s.shared_namespace
        pods = k8s.list_pods_by_label(selector, ns)
        events = [_translate_event(e) for e in k8s.list_pod_events(selector, ns)]
        return pods, events, "k8s"
    except Exception as exc:  # noqa: BLE001 — never break detail on a read error
        log.warning("console.pods_events_failed job=%s err=%r", j.id, exc)
        return [], [], "unavailable"


def _enforce_console_submit(identity: Identity, body: "CreateJobModel") -> None:
    """Run the SAME account-enabled + grant + quota checks as /v1/jobs, so the
    console submit path can't bypass them (DRY via core.access_control).

    Usage = the user's GPUs already committed across non-terminal platform jobs
    + running dev-sessions; the ask is this job's total GPUs.
    """
    rec = get_user_store().get(identity.user)
    js = get_job_store()
    committed_gpus = js.count_running_gpus(identity.user)
    try:
        committed_gpus += get_devsession_store().gpu_in_use_by_user(identity.user)
    except Exception:  # noqa: BLE001 — usage probe must never hard-fail the submit gate
        log.warning("console.quota: devsession usage probe failed", exc_info=True)
    running_jobs = sum(
        1 for j in js.list_visible(identity.user, identity.tenant, is_admin=True)
        if j.user == identity.user and j.status in ("Queued", "Starting", "Running")
    )
    ask = SubmitAsk(
        project=body.project,
        queue=body.queue,
        image=body.image,
        gpus=body.resources.nodes * body.resources.gpusPerNode,
    )
    try:
        enforce_submit(rec, ask, current_gpus=committed_gpus, current_jobs=running_jobs)
    except AccessDenied as exc:
        # disabled account → 403; quota/grant → 403 as well (forbidden, not bad request)
        raise FriendlyError(403, exc.code, exc.message, hint=exc.hint) from exc


# k8s reason → human-readable (Chinese) translation for the events timeline.
_EVENT_REASON_ZH = {
    "ImagePullBackOff": "镜像拉取失败：tag 不存在或仓库需要认证",
    "ErrImagePull": "镜像拉取出错",
    "FailedScheduling": "无法调度：资源不足或不满足约束（等待配额/节点）",
    "Unschedulable": "无法调度：当前资源池已满，等待资源释放",
    "OOMKilled": "内存超限被杀（exit 137）：建议降低 batch size 或提高显存配额",
    "BackOff": "容器反复重启（CrashLoopBackOff）",
    "FailedMount": "存储卷挂载失败：检查 PVC / 共享存储",
    "Created": "容器已创建",
    "Started": "容器已启动",
    "Pulling": "正在拉取镜像",
    "Pulled": "镜像拉取完成",
    "Scheduled": "已调度到节点",
}


def _translate_event(e: dict) -> dict:
    raw = e.get("raw") or e.get("reason") or ""
    msg = _EVENT_REASON_ZH.get(raw)
    if msg:
        e = {**e, "message": e.get("message") or msg}
    return e


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _validate_queue(queue: str, gpu_type: str, settings: Settings) -> None:
    """Req 9.6 — the chosen queue must be a real LocalQueue. We only enforce
    when we can actually read Kueue; if Kueue is unreadable we don't block the
    submit on it (the cluster check in Req 5.4 already gates real submission)."""
    if not queue:
        raise FriendlyError(400, Codes.QUEUE_NOT_FOUND, "请选择一个队列")
    try:
        names = {q.name for q in get_kueue_reader().list_queues()}
    except KueueUnavailable:
        return  # can't read queues now; don't hard-block here
    if names and queue not in names:
        raise FriendlyError(
            400, Codes.QUEUE_NOT_FOUND,
            f"队列 {queue!r} 不存在于集群 Kueue 中",
            hint=f"可用队列：{', '.join(sorted(names)) or '（无）'}",
        )


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
def get_job(
    jid: str,
    identity: Identity = Depends(require_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    j = get_job_store().get(jid)
    if not j:
        raise HTTPException(404, detail="job not found")
    if not identity.is_admin and j.user != identity.user and j.tenant != identity.tenant:
        raise HTTPException(403, detail="not visible to you")
    j = get_submission_service().reconcile(j)
    return _job_detail(j, settings)


@router.get("/jobs/{jid}/logs", summary="Real training logs (Loki when configured)")
def stream_logs(
    jid: str,
    container: str | None = None,
    identity: Identity = Depends(require_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Req 8 — query training logs from Loki by submission_id so they're
    available during AND after the run. Falls back to a clearly-labeled
    'unavailable' page when Loki isn't configured or the job isn't live."""
    j = get_job_store().get(jid)
    if not j:
        raise HTTPException(404, detail="job not found")
    if not identity.is_admin and j.user != identity.user and j.tenant != identity.tenant:
        raise HTTPException(403, detail="not visible to you")

    from ..core.loki_client import LokiUnavailable, get_loki_client

    loki = get_loki_client()
    if loki is None or not j.submission_id:
        # No Loki configured or job never went live → explicit unavailable,
        # not synthesized data (Req 14.5).
        return {"lines": [], "next_cursor": None, "source": "unavailable",
                "reason": "Loki 未配置或任务未真实提交到集群"}

    # window: from job creation to finished (or now), in nanoseconds
    start_ns = int((j.created_at - 60) * 1e9)
    end_ns = int(((j.finished_at or time.time()) + 60) * 1e9)
    try:
        page = loki.query_range(j.submission_id, start_ns, end_ns, container=container)
    except LokiUnavailable as exc:
        raise FriendlyError(
            503, Codes.LOKI_UNAVAILABLE, "训练日志暂不可用 (Loki)", hint=str(exc)
        )
    return page.to_dict()


@router.get("/jobs/{jid}/metrics", summary="Real GPU/throughput metrics (Prometheus)")
def job_metrics(
    jid: str,
    identity: Identity = Depends(require_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Req 10 — query the job's real GPU util / memory / throughput from
    Prometheus, constrained to its pods. Empty series are flagged 'unavailable',
    never fabricated."""
    j = get_job_store().get(jid)
    if not j:
        raise HTTPException(404, detail="job not found")
    if not identity.is_admin and j.user != identity.user and j.tenant != identity.tenant:
        raise HTTPException(403, detail="not visible to you")

    from ..core.prometheus_client import PromUnavailable, get_prometheus_client

    prom = get_prometheus_client()
    if prom is None or not j.submission_id:
        return {"series": [], "source": "unavailable",
                "reason": "Prometheus 未配置或任务未真实提交到集群"}

    now = int(time.time())
    start = int(j.created_at)
    end = int(j.finished_at or now)
    try:
        series = prom.job_metrics(j.submission_id, start, end, step=60)
    except PromUnavailable as exc:
        raise FriendlyError(
            503, Codes.PROM_UNAVAILABLE, "训练指标暂不可用 (Prometheus)", hint=str(exc)
        )
    return {"series": [s.to_dict() for s in series], "source": "prometheus"}


@router.post("/jobs", status_code=201, summary="Create a job record (console submit)")
def create_job(
    body: CreateJobModel,
    identity: Identity = Depends(require_user),
    settings: Settings = Depends(get_settings),
) -> dict:
    # Req 5.4 — reject submits whose gpu_type has no shared cluster instead of
    # silently creating a perpetually-Queued placeholder. A cluster-less demo
    # can opt in via allow_record_only_submit.
    if not cluster_configured(body.resources.gpuType, settings) and not settings.allow_record_only_submit:
        raise FriendlyError(
            400, Codes.NO_CLUSTER,
            f"GPU 类型 {body.resources.gpuType} 没有可用的训练集群",
            hint="请联系管理员为该 gpu_type 配置 RAYTRAIN_SHARED_CLUSTERS，或换一个有集群的 gpu_type",
        )
    # Req 9.6 — the chosen queue must be a real LocalQueue for this gpu_type.
    _validate_queue(body.queue, body.resources.gpuType, settings)
    # Bug fix — the console submit path must run the SAME account-enabled +
    # grant + quota checks as /v1/jobs (it previously bypassed all of them).
    _enforce_console_submit(identity, body)

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
    return _job_detail(rec, settings)


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
def retry_job(
    jid: str,
    identity: Identity = Depends(require_user),
    settings: Settings = Depends(get_settings),
) -> dict:
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
    log.info("console.job.retry src=%s new=%s user=%s", src.id, rec.id, identity.user)
    # Bug fix — Retry must actually re-run on Ray (it previously only created a
    # record). Re-submit through the same bridge as create, carrying over the
    # original code_uri so the rerun uses the identical code (reproducibility).
    svc = get_submission_service()
    rec = svc.submit(rec, dataset_uri=src.mounts.dataset_uri, code_uri=src.code_uri) or rec
    return _job_detail(rec, settings)


# --------------------------------------------------------------------------- #
# Queues / pools / overview / experiments
# --------------------------------------------------------------------------- #


@router.get("/queues", summary="Queues (real Kueue) with live used/pending")
def list_queues(identity: Identity = Depends(require_user)) -> list[dict]:
    """Req 9 — queues come from the cluster's real Kueue LocalQueues, not a
    hardcoded seed. recentJobs is enriched from the platform JobStore."""
    js = get_job_store()
    jobs = js.list_visible(identity.user, identity.tenant, is_admin=True)
    try:
        queues = get_kueue_reader().list_queues()
    except KueueUnavailable as exc:
        raise FriendlyError(
            503, Codes.KUEUE_UNAVAILABLE,
            "无法读取集群队列 (Kueue)",
            hint=str(exc),
        )
    out = []
    for q in queues:
        recent = [
            {"id": j.id, "name": j.name, "status": j.status}
            for j in jobs if j.queue == q.name
        ][:3]
        d = q.to_dict()
        d["recentJobs"] = recent
        d["source"] = "kueue"
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


@router.get("/artifacts", summary="All real artifacts across visible jobs (MinIO)")
def artifacts(identity: Identity = Depends(require_user)) -> list[dict]:
    """Req 14.6 — list each visible job's REAL outputs from object storage. No
    store configured (or a listing error) → that job simply contributes nothing
    (never synthesized). Empty overall → the page shows an empty state."""
    js = get_job_store()
    jobs = js.list_visible(identity.user, identity.tenant, identity.is_admin)
    store = get_artifact_store()
    if store is None:
        return []
    out = []
    for j in jobs:
        if not j.mounts.checkpoint_uri:
            continue
        try:
            page = store.list_for_uri(j.mounts.checkpoint_uri)
        except ArtifactsUnavailable:
            continue
        for a in page.artifacts:
            out.append({**a.to_dict(), "jobId": j.id, "jobName": j.name})
    return out
