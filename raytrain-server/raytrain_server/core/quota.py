"""
Per-user quota enforcement (pure decision functions).

These are deliberately free of FastAPI / DB / Ray imports so they unit-test
trivially. The API layer gathers the inputs (the user's caps + their current
usage + the resources this request asks for) and calls :func:`check_quota`
before forwarding a submission to Ray.

Quota is per single user (per the product decision). ``cap < 0`` means
*unlimited*; ``cap == 0`` means *unset* and is treated as unlimited too (admins
opt in by setting a positive cap). Any positive cap is a hard ceiling:

    current_usage + requested  <=  cap
"""
from __future__ import annotations

from dataclasses import dataclass

from .users import UserQuota


@dataclass
class ResourceAsk:
    """Resources a single submission will consume while running."""

    gpus: int = 0
    cpus: int = 0
    memory_gi: int = 0
    jobs: int = 1  # this submission counts as 1 concurrent job


@dataclass
class Usage:
    """The user's *current* committed usage (running + pending)."""

    gpus: int = 0
    cpus: int = 0
    memory_gi: int = 0
    jobs: int = 0


@dataclass
class QuotaViolation:
    resource: str   # "gpus" | "cpus" | "memory_gi" | "jobs"
    requested: int
    used: int
    cap: int

    @property
    def message(self) -> str:
        names = {
            "gpus": "GPU",
            "cpus": "CPU",
            "memory_gi": "内存(GiB)",
            "jobs": "并发任务数",
        }
        n = names.get(self.resource, self.resource)
        return (
            f"超出{n}配额：已用 {self.used} + 本次 {self.requested} "
            f"> 上限 {self.cap}"
        )


def _cap_unlimited(cap: int) -> bool:
    # <0 explicit unlimited; 0 unset -> treated as unlimited.
    return cap is None or cap <= 0


def check_quota(quota: UserQuota, usage: Usage, ask: ResourceAsk) -> QuotaViolation | None:
    """Return the FIRST violated resource, or None if the ask fits.

    Checked in a stable order so error messages are deterministic.
    """
    checks = [
        ("gpus", quota.max_gpus, usage.gpus, ask.gpus),
        ("jobs", quota.max_jobs, usage.jobs, ask.jobs),
        ("cpus", quota.max_cpus, usage.cpus, ask.cpus),
        ("memory_gi", quota.max_memory_gi, usage.memory_gi, ask.memory_gi),
    ]
    for resource, cap, used, requested in checks:
        if _cap_unlimited(cap):
            continue
        if used + requested > cap:
            return QuotaViolation(
                resource=resource, requested=requested, used=used, cap=cap
            )
    return None
