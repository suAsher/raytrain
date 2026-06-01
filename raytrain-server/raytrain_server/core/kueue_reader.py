"""
KueueReader — read REAL queues from the cluster's Kueue CRDs (Req 9).

Replaces the hardcoded `_DEFAULT_QUEUES` seed. The console's Queues page and the
Create-Job wizard's queue choices come from here, so what a user sees equals
what the cluster actually has.

Model
-----
- LocalQueue (namespaced)   → user-facing queue; `.spec.clusterQueue` links it
  to a ClusterQueue.
- ClusterQueue (cluster)    → quota source: `.spec.resourceGroups[].flavors[]
  .resources[]` give nominalQuota; `.status.flavorsReservation`/`flavorsUsage`
  give used; `.status.pendingWorkloads`/`admittedWorkloads` give pending/admitted.

We expose a small Protocol + an HTTP/K8s implementation + a Fake for tests
(same injectable pattern as RayClusterClient / K8sClient).

Failure to read (CRD absent / RBAC denied) raises KueueUnavailable, which the
API turns into a FriendlyError — we never fall back to misleading hardcoded
queues (Req 9.4).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Protocol

log = logging.getLogger(__name__)

KUEUE_GROUP = "kueue.x-k8s.io"
KUEUE_VERSION = "v1beta1"


class KueueUnavailable(Exception):
    """Raised when Kueue CRDs cannot be read (absent / RBAC / network)."""


@dataclass
class QueueInfo:
    name: str                 # LocalQueue name (user-facing)
    namespace: str
    cluster_queue: str        # the ClusterQueue it points at
    gpu_type: str             # derived from flavor / resource name
    nominal: int              # nominal GPU quota
    used: int                 # reserved/used GPUs
    admitted: int             # admitted workloads
    pending: int              # pending workloads

    def to_dict(self) -> dict:
        return asdict(self)


def _gpu_type_from_flavor(flavor_name: str) -> str:
    """Heuristic: a ResourceFlavor named like 'h20-flavor' / 'a100' → gpu_type.
    Falls back to the flavor name as-is."""
    fl = (flavor_name or "").lower()
    for known in ("h20", "a100", "h100", "h800", "a800", "v100"):
        if known in fl:
            return known.upper()
    if "cpu" in fl:
        return "CPU-only"
    return flavor_name or "unknown"


def _sum_gpu_nominal(cluster_queue: dict) -> tuple[int, str]:
    """Sum nominalQuota for nvidia.com/gpu across resourceGroups/flavors.
    Returns (nominal_gpus, gpu_type)."""
    spec = cluster_queue.get("spec", {}) or {}
    nominal = 0
    gpu_type = "unknown"
    for rg in spec.get("resourceGroups", []) or []:
        for fl in rg.get("flavors", []) or []:
            fname = fl.get("name", "")
            for res in fl.get("resources", []) or []:
                if res.get("name") in ("nvidia.com/gpu", "gpu"):
                    try:
                        nominal += int(float(str(res.get("nominalQuota", 0))))
                    except (TypeError, ValueError):
                        pass
                    gt = _gpu_type_from_flavor(fname)
                    if gt != "unknown":
                        gpu_type = gt
    return nominal, gpu_type


def _sum_gpu_used(cluster_queue: dict) -> int:
    """Sum reserved/used nvidia.com/gpu from ClusterQueue.status."""
    status = cluster_queue.get("status", {}) or {}
    used = 0
    # status.flavorsReservation[].resources[] {name, total}
    for fr in status.get("flavorsReservation", []) or status.get("flavorsUsage", []) or []:
        for res in fr.get("resources", []) or []:
            if res.get("name") in ("nvidia.com/gpu", "gpu"):
                try:
                    used += int(float(str(res.get("total", 0))))
                except (TypeError, ValueError):
                    pass
    return used


class KueueReader(Protocol):
    def list_queues(self) -> list[QueueInfo]: ...
    def get_queue(self, name: str, namespace: str = "") -> QueueInfo | None: ...


class K8sKueueReader:
    """Reads ClusterQueue/LocalQueue via the K8s CustomObjects API."""

    def __init__(self, in_cluster: bool | None = None) -> None:
        self._in_cluster = in_cluster
        self._api = None

    def _ensure(self):
        if self._api is not None:
            return
        from kubernetes import client, config

        if self._in_cluster is True:
            config.load_incluster_config()
        elif self._in_cluster is False:
            config.load_kube_config()
        else:
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
        self._api = client.CustomObjectsApi()

    def _list_cr(self, plural: str, namespace: str | None = None) -> list[dict]:
        from kubernetes.client.rest import ApiException

        try:
            self._ensure()
            if namespace is None:
                resp = self._api.list_cluster_custom_object(
                    KUEUE_GROUP, KUEUE_VERSION, plural
                )
            else:
                resp = self._api.list_namespaced_custom_object(
                    KUEUE_GROUP, KUEUE_VERSION, namespace, plural
                )
        except ApiException as e:
            raise KueueUnavailable(
                f"failed to read {plural} (status={e.status}): {e.reason}"
            ) from e
        except Exception as e:  # noqa: BLE001 — network/config
            raise KueueUnavailable(f"failed to read {plural}: {e!r}") from e
        return resp.get("items", []) or []

    def list_queues(self) -> list[QueueInfo]:
        cqs = {
            (cq.get("metadata", {}) or {}).get("name"): cq
            for cq in self._list_cr("clusterqueues")
        }
        out: list[QueueInfo] = []
        for lq in self._list_cr("localqueues"):
            meta = lq.get("metadata", {}) or {}
            spec = lq.get("spec", {}) or {}
            lq_status = lq.get("status", {}) or {}
            cq_name = spec.get("clusterQueue", "")
            cq = cqs.get(cq_name, {})
            nominal, gpu_type = _sum_gpu_nominal(cq)
            out.append(
                QueueInfo(
                    name=meta.get("name", ""),
                    namespace=meta.get("namespace", ""),
                    cluster_queue=cq_name,
                    gpu_type=gpu_type,
                    nominal=nominal,
                    used=_sum_gpu_used(cq),
                    admitted=int(lq_status.get("admittedWorkloads", 0) or 0),
                    pending=int(lq_status.get("pendingWorkloads", 0) or 0),
                )
            )
        return out

    def get_queue(self, name: str, namespace: str = "") -> QueueInfo | None:
        for q in self.list_queues():
            if q.name == name and (not namespace or q.namespace == namespace):
                return q
        return None


class FakeKueueReader:
    """Test double: serves a preset list of QueueInfo."""

    def __init__(self, queues: list[QueueInfo] | None = None, fail: bool = False):
        self._queues = queues or []
        self._fail = fail

    def list_queues(self) -> list[QueueInfo]:
        if self._fail:
            raise KueueUnavailable("fake failure")
        return list(self._queues)

    def get_queue(self, name: str, namespace: str = "") -> QueueInfo | None:
        if self._fail:
            raise KueueUnavailable("fake failure")
        for q in self._queues:
            if q.name == name and (not namespace or q.namespace == namespace):
                return q
        return None


_kueue_reader: KueueReader | None = None


def get_kueue_reader() -> KueueReader:
    global _kueue_reader
    if _kueue_reader is None:
        from .settings import get_settings

        _kueue_reader = K8sKueueReader(in_cluster=get_settings().in_cluster)
    return _kueue_reader


def set_kueue_reader(r: KueueReader) -> None:
    global _kueue_reader
    _kueue_reader = r
