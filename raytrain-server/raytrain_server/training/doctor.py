"""
Cluster readiness checks (POST /api/doctor).

Verifies the platform's runtime prerequisites: KubeRay CRD, Kueue CRD, GPU
nodes, RDMA capacity, a usable StorageClass, and the target namespace.

Each check returns a CheckResult with ok/skipped + a human message. The probe
functions take an injectable "kube" facade so tests run without a cluster.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str
    severity: str = "error"  # error | warn | info


@dataclass
class DoctorReport:
    results: list[CheckResult] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return all(r.ok or r.severity != "error" for r in self.results)

    def to_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "checks": [
                {"name": r.name, "ok": r.ok, "message": r.message, "severity": r.severity}
                for r in self.results
            ],
        }


class KubeProbe(Protocol):
    def crd_exists(self, name: str) -> bool: ...
    def count_gpu_nodes(self, gpu_type: str | None = None) -> int: ...
    def count_rdma_capacity(self) -> int: ...
    def storage_classes(self) -> list[str]: ...
    def namespace_exists(self, ns: str) -> bool: ...


def run_doctor(
    probe: KubeProbe,
    *,
    namespace: str,
    want_gpu_type: str | None = None,
    want_rwx: bool = True,
) -> DoctorReport:
    results: list[CheckResult] = []

    # KubeRay
    ok = probe.crd_exists("rayjobs.ray.io")
    results.append(CheckResult(
        "kuberay", ok,
        "KubeRay CRD (rayjobs.ray.io) 已安装" if ok else "缺少 KubeRay CRD，无法提交 RayJob",
    ))

    # Kueue
    ok = probe.crd_exists("workloads.kueue.x-k8s.io")
    results.append(CheckResult(
        "kueue", ok,
        "Kueue 已安装" if ok else "未检测到 Kueue，任务将不经队列直接调度",
        severity="warn",
    ))

    # GPU nodes
    n = probe.count_gpu_nodes(want_gpu_type)
    ok = n > 0
    label = want_gpu_type or "任意"
    results.append(CheckResult(
        "gpu_nodes", ok,
        f"检测到 {n} 个 {label} GPU 节点" if ok else f"未发现 {label} GPU 节点",
    ))

    # RDMA (warn-only; only needed for multi-node)
    rdma = probe.count_rdma_capacity()
    results.append(CheckResult(
        "rdma", rdma > 0,
        f"RDMA 可用容量 {rdma}" if rdma > 0 else "未检测到 RDMA，多节点训练可能退化为以太网",
        severity="warn",
    ))

    # StorageClass with RWX
    scs = probe.storage_classes()
    ok = len(scs) > 0
    results.append(CheckResult(
        "storage", ok,
        f"可用 StorageClass: {', '.join(scs)}" if ok else "没有可用 StorageClass",
    ))

    # namespace
    ok = probe.namespace_exists(namespace)
    results.append(CheckResult(
        "namespace", ok,
        f"命名空间 {namespace} 存在" if ok else f"命名空间 {namespace} 不存在",
    ))

    return DoctorReport(results=results)
