"""
State aggregation.

The user-facing JobState is NOT a single field — it's derived from multiple
Kubernetes signals (per spec: "状态必须从 RayJob status、Kueue workload、Pod
phase、Events 聚合得出"). This module is a pure function so it's fully testable
without a cluster.

Priority of signals (highest wins):
    1. explicit cancel intent          -> Cancelling / Cancelled
    2. RayJob terminal status          -> Succeeded / Failed
    3. Kueue workload not admitted     -> Queued
    4. Kueue admitted but no pods yet  -> Admitted
    5. pods present but not all Running-> Starting
    6. all expected workers Running    -> Running
    7. nothing matched                 -> Unknown
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .domain import JobState


@dataclass
class ClusterSignals:
    """Raw signals gathered from the cluster for one run."""

    # RayJob .status.jobStatus / jobDeploymentStatus
    rayjob_status: str = ""           # PENDING|RUNNING|SUCCEEDED|FAILED|STOPPED|""
    rayjob_deployment_status: str = ""  # Initializing|Running|Complete|Failed|""
    # Kueue workload admission
    kueue_admitted: bool | None = None  # None = unknown / no kueue
    # Pod phases for the run's pods
    pod_phases: list[str] = field(default_factory=list)  # Pending/Running/...
    expected_workers: int = 1
    # platform intent
    cancel_requested: bool = False
    deleting: bool = False


def aggregate_state(sig: ClusterSignals) -> JobState:
    # 1. cancellation intent
    if sig.cancel_requested:
        # if pods are gone, it's fully cancelled
        running = [p for p in sig.pod_phases if p == "Running"]
        if not sig.pod_phases or not running:
            return JobState.CANCELLED
        return JobState.CANCELLING

    if sig.deleting:
        return JobState.CLEANING

    # 2. RayJob terminal
    rj = (sig.rayjob_status or "").upper()
    if rj == "SUCCEEDED":
        return JobState.SUCCEEDED
    if rj in ("FAILED", "STOPPED"):
        return JobState.FAILED

    dep = (sig.rayjob_deployment_status or "").lower()
    if dep == "complete":
        return JobState.SUCCEEDED
    if dep == "failed":
        return JobState.FAILED

    # 3. Kueue gating
    if sig.kueue_admitted is False:
        return JobState.QUEUED
    if sig.kueue_admitted is True and not sig.pod_phases:
        return JobState.ADMITTED

    # 4. pod-phase based
    if sig.pod_phases:
        running = sum(1 for p in sig.pod_phases if p == "Running")
        # consider succeeded pods as still "running" for aggregation simplicity
        if running >= sig.expected_workers and rj == "RUNNING":
            return JobState.RUNNING
        if running >= sig.expected_workers:
            return JobState.RUNNING
        # some pods exist but not all Running
        return JobState.STARTING

    # 5. RayJob says running but we have no pod info yet
    if rj == "RUNNING" or dep == "running":
        return JobState.RUNNING
    if rj == "PENDING" or dep == "initializing":
        return JobState.STARTING

    return JobState.UNKNOWN


# --------------------------------------------------------------------------- #
# Failure attribution (spec: 失败原因归因)
# --------------------------------------------------------------------------- #


@dataclass
class FailureReason:
    code: str
    message: str


def attribute_failure(sig: ClusterSignals, events: list[str]) -> FailureReason | None:
    """Best-effort human-readable failure attribution from events.

    Only meaningful when the aggregated state is FAILED. Returns None if we
    can't attribute a cause.
    """
    text = " ".join(events).lower()
    if "imagepullbackoff" in text or "errimagepull" in text:
        return FailureReason("IMAGE_PULL_FAILED", "镜像拉取失败，请检查镜像名/凭据")
    if "insufficient" in text and "nvidia.com/gpu" in text:
        return FailureReason("INSUFFICIENT_GPU", "GPU 资源不足，无法调度")
    if "oomkilled" in text or "out of memory" in text:
        return FailureReason("OOM", "内存不足 (OOMKilled)，请提高内存或减小 batch")
    if "unschedulable" in text or "failedscheduling" in text:
        return FailureReason("UNSCHEDULABLE", "节点资源不足，Pod 无法调度")
    if "quota" in text:
        return FailureReason("QUOTA", "配额不足，任务被拒绝")
    return None
