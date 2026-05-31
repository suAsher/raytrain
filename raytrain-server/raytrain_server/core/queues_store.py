"""
Queue / resource-pool registry (Kueue-facing, but CRD-free for users).

The console's Queues page and Overview resource pools read from here. In a live
cluster a reconciler would refresh ``used`` / ``pending`` / ``admitted`` from
Kueue Workload + ClusterQueue status; for now these are platform records an
admin seeds (and the job store keeps ``used`` roughly in sync via
``recompute_from_jobs``).
"""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class QueueRecord:
    name: str
    cluster_queue: str
    gpu_type: str
    nominal: int          # nominal quota (GPUs)
    used: int = 0
    pending: int = 0
    admitted: int = 0
    avg_wait_min: int = 0
    health: str = "healthy"   # healthy | degraded | down

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PoolRecord:
    name: str             # H20 | A100 | CPU-only
    total_gpu: int
    used_gpu: int = 0
    nodes: int = 0
    health: str = "healthy"

    def to_dict(self) -> dict:
        return asdict(self)


# Default seed so a fresh platform shows a sensible Queues/Overview page.
_DEFAULT_QUEUES = [
    QueueRecord("h20-research", "cq-h20", "H20", 64, 0, 0, 0, 3, "healthy"),
    QueueRecord("h20-shared", "cq-h20", "H20", 64, 0, 0, 0, 6, "healthy"),
    QueueRecord("a100-research", "cq-a100", "A100", 32, 0, 0, 0, 12, "healthy"),
    QueueRecord("cpu-batch", "cq-cpu", "CPU-only", 512, 0, 0, 0, 0, "healthy"),
]

_DEFAULT_POOLS = [
    PoolRecord("H20", 128, 0, 16, "healthy"),
    PoolRecord("A100", 32, 0, 4, "healthy"),
    PoolRecord("CPU-only", 0, 0, 8, "healthy"),
]


class QueueStore:
    def __init__(self, seed: bool = True) -> None:
        self._lock = threading.Lock()
        self._queues: dict[str, QueueRecord] = {}
        self._pools: dict[str, PoolRecord] = {}
        if seed:
            for q in _DEFAULT_QUEUES:
                self._queues[q.name] = QueueRecord(**asdict(q))
            for p in _DEFAULT_POOLS:
                self._pools[p.name] = PoolRecord(**asdict(p))

    def list_queues(self) -> list[QueueRecord]:
        with self._lock:
            return list(self._queues.values())

    def get_queue(self, name: str) -> Optional[QueueRecord]:
        with self._lock:
            return self._queues.get(name)

    def add_queue(self, rec: QueueRecord) -> QueueRecord:
        with self._lock:
            if rec.name in self._queues:
                raise ValueError(f"queue {rec.name!r} already exists")
            self._queues[rec.name] = rec
            return rec

    def remove_queue(self, name: str) -> bool:
        with self._lock:
            return self._queues.pop(name, None) is not None

    def list_pools(self) -> list[PoolRecord]:
        with self._lock:
            return list(self._pools.values())

    def recompute_from_jobs(self, jobs) -> None:
        """Refresh used/pending/admitted from current job records."""
        with self._lock:
            for q in self._queues.values():
                q.used = 0
                q.pending = 0
                q.admitted = 0
            for p in self._pools.values():
                p.used_gpu = 0
            for j in jobs:
                q = self._queues.get(j.queue)
                gpus = j.resources.total_gpu
                if j.status in ("Running", "Starting"):
                    if q:
                        q.used += gpus
                        q.admitted += 1
                    pool = self._pools.get(j.resources.gpu_type)
                    if pool:
                        pool.used_gpu += gpus
                elif j.status == "Queued":
                    if q:
                        q.pending += 1
            # derive health from utilization
            for q in self._queues.values():
                ratio = (q.used / q.nominal) if q.nominal else 0
                q.health = "degraded" if (ratio >= 0.9 or q.pending > 10) else "healthy"
            for p in self._pools.values():
                ratio = (p.used_gpu / p.total_gpu) if p.total_gpu else 0
                p.health = "degraded" if ratio >= 0.9 else "healthy"


_queue_store: QueueStore | None = None


def get_queue_store() -> QueueStore:
    global _queue_store
    if _queue_store is None:
        _queue_store = QueueStore()
    return _queue_store


def set_queue_store(s: QueueStore) -> None:
    global _queue_store
    _queue_store = s
