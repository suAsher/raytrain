"""
Platform-side training-job records.

Why a separate store from Ray?
------------------------------
The thin ``/v1/jobs`` API forwards to a live RayCluster and returns only
``submission_id`` / ``status`` / ``metadata`` for one gpu_type at a time. The
web console needs a richer, cluster-independent view: a durable record of every
submission (name, project, queue, resources, mounts, creator, timestamps,
failure info, artifacts) that it can list / filter / detail without talking to
Ray directly.

This store is that record. It is updated when a job is submitted through the
console, and is the source of truth the console reads back. Live telemetry that
genuinely requires the cluster (real pod phase, GPU metrics) is *derived* from
the stored record (see ``console_views.py``) and clearly marked — everything
structural here is real platform state.

Same dual-backend pattern as the rest of core (in-memory now; a SQL-backed
variant can slot in behind the same interface later).
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

# canonical statuses the console understands
JOB_STATUSES = ("Queued", "Starting", "Running", "Succeeded", "Failed", "Cancelled")


@dataclass
class JobResources:
    gpu_type: str = "H20"
    nodes: int = 1
    gpus_per_node: int = 8
    cpu_per_gpu: int = 8
    mem_per_gpu_gi: int = 96
    head_cpu: int = 4
    head_mem_gi: int = 16
    rdma: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def total_gpu(self) -> int:
        if self.gpu_type.upper() == "CPU-ONLY":
            return 0
        return self.nodes * self.gpus_per_node


@dataclass
class JobMounts:
    dataset_uri: str = ""
    checkpoint_uri: str = ""
    checkpoint_shared: bool = True
    scratch_gi: int = 200

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class FailureInfo:
    category: str = ""
    summary: str = ""
    detail: str = ""
    container: str = ""
    log_anchor: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlatformJob:
    id: str
    name: str
    user: str
    tenant: str
    project: str
    queue: str
    quota_group: str = ""
    priority: str = "normal"
    status: str = "Queued"
    image: str = ""
    entrypoint: str = ""
    working_dir: str = ""
    git_ref: str = ""
    env: dict = field(default_factory=dict)
    submission_id: str = ""          # the Ray submission id, if forwarded
    code_uri: str = ""
    resources: JobResources = field(default_factory=JobResources)
    mounts: JobMounts = field(default_factory=JobMounts)
    failure: Optional[FailureInfo] = None
    description: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    experiment: str = ""             # experiment group name (for grouping)

    def to_dict(self) -> dict:
        d = asdict(self)
        # asdict already recurses into nested dataclasses
        if self.failure is None:
            d["failure"] = None
        return d

    @property
    def duration_sec(self) -> int:
        if not self.started_at:
            return 0
        end = self.finished_at or time.time()
        return int(end - self.started_at)


class JobStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, PlatformJob] = {}

    def create(self, rec: PlatformJob) -> PlatformJob:
        with self._lock:
            if not rec.id:
                rec.id = "job-" + uuid.uuid4().hex[:8]
            self._items[rec.id] = rec
            return rec

    def get(self, jid: str) -> Optional[PlatformJob]:
        with self._lock:
            return self._items.get(jid)

    def update(self, jid: str, **changes) -> Optional[PlatformJob]:
        with self._lock:
            rec = self._items.get(jid)
            if not rec:
                return None
            for k, v in changes.items():
                setattr(rec, k, v)
            return rec

    def delete(self, jid: str) -> bool:
        with self._lock:
            return self._items.pop(jid, None) is not None

    def list_visible(self, user: str, tenant: str, is_admin: bool) -> list[PlatformJob]:
        with self._lock:
            vals = list(self._items.values())
        vals.sort(key=lambda j: j.created_at, reverse=True)
        if is_admin:
            return vals
        # users see their own jobs + same-tenant jobs (read-only collaboration)
        return [j for j in vals if j.user == user or j.tenant == tenant]

    def count_running_gpus(self, user: str) -> int:
        with self._lock:
            return sum(
                j.resources.total_gpu
                for j in self._items.values()
                if j.user == user and j.status in ("Queued", "Starting", "Running")
            )


_job_store: JobStore | None = None


def get_job_store() -> JobStore:
    global _job_store
    if _job_store is None:
        _job_store = JobStore()
    return _job_store


def set_job_store(s: JobStore) -> None:
    global _job_store
    _job_store = s
