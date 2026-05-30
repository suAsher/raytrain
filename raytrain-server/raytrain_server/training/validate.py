"""
Pre-submit validation of a TrainingJob intent.

Pure functions: take a TrainingJob (+ optional quota view), raise PlatformError
with a stable code on the first hard failure, or return a list of warnings.

Covers the spec's hard rules:
    - reserved label/annotation collision
    - multi-node requires shared (RWX) checkpoint storage; RWO blocked
    - multi-node requires a checkpoint URI at all
    - basic resource sanity (gpus_per_node 0..8, nodes >= 1)
    - quota (when a quota view is supplied)
"""
from __future__ import annotations

from dataclasses import dataclass

from . import errors as E
from .domain import MountMode, TrainingJob
from .labels import reject_reserved


@dataclass
class QuotaView:
    """What the caller's quota group currently allows / has used."""

    gpu_limit: int
    gpu_used: int

    @property
    def gpu_available(self) -> int:
        return max(0, self.gpu_limit - self.gpu_used)


def validate_reserved_labels(job: TrainingJob) -> None:
    bad = reject_reserved(job.labels) + reject_reserved(job.annotations)
    if bad:
        raise E.PlatformError(
            code=E.ERR_RESERVED_LABEL,
            message=(
                "这些标签使用了平台保留前缀，不能自定义："
                + ", ".join(sorted(set(bad)))
            ),
            details={"keys": sorted(set(bad))},
        )


def validate_required_fields(job: TrainingJob) -> None:
    missing = []
    if not job.name:
        missing.append("name")
    if not job.image:
        missing.append("image")
    if not job.command:
        missing.append("command")
    if not job.queue:
        missing.append("queue")
    if missing:
        raise E.PlatformError(
            code=E.ERR_MISSING_FIELD,
            message="缺少必填字段：" + ", ".join(missing),
            details={"fields": missing},
        )


def validate_resources(job: TrainingJob) -> None:
    r = job.resources
    if r.nodes < 1:
        raise E.PlatformError(
            code=E.ERR_INVALID_RESOURCE,
            message="节点数必须 >= 1",
            details={"nodes": r.nodes},
        )
    if not (0 <= r.gpus_per_node <= 8):
        raise E.PlatformError(
            code=E.ERR_INVALID_RESOURCE,
            message="每节点 GPU 数必须在 0..8 之间",
            details={"gpus_per_node": r.gpus_per_node},
        )


def validate_multinode_checkpoint(job: TrainingJob) -> None:
    """Multi-node jobs must write checkpoints to shared (RWX) storage."""
    if not job.resources.is_multi_node:
        return
    ckpt = job.checkpoint
    if not ckpt.uri:
        raise E.PlatformError(
            code=E.ERR_CHECKPOINT_REQUIRED_MULTINODE,
            message="多节点训练必须配置共享 checkpoint 存储",
            details={"nodes": job.resources.nodes},
        )
    # If the checkpoint is backed by a PVC, it must be RWX.
    if ckpt.uri.startswith("pvc://") and ckpt.mode == MountMode.RWO:
        raise E.PlatformError(
            code=E.ERR_PVC_RWO_MULTINODE,
            message=(
                "多节点训练的 checkpoint PVC 是 ReadWriteOnce，无法被多个节点"
                "同时写入。请使用 ReadWriteMany PVC 或对象存储。"
            ),
            details={"uri": ckpt.uri, "mode": ckpt.mode.value},
        )


def validate_quota(job: TrainingJob, quota: QuotaView | None) -> None:
    if quota is None:
        return
    need = job.resources.total_gpus
    if need > quota.gpu_available:
        raise E.PlatformError(
            code=E.ERR_QUOTA_EXCEEDED,
            message=(
                f"GPU 配额不足：需要 {need}，可用 {quota.gpu_available} "
                f"(上限 {quota.gpu_limit}，已用 {quota.gpu_used})"
            ),
            details={
                "need": need,
                "available": quota.gpu_available,
                "limit": quota.gpu_limit,
                "used": quota.gpu_used,
            },
        )


def validate_image_allowed(job: TrainingJob, allowed_images: list[str] | None) -> None:
    """If an allowlist is configured, the image must match one of its prefixes."""
    if not allowed_images:
        return
    if not any(job.image.startswith(a) for a in allowed_images):
        raise E.PlatformError(
            code=E.ERR_IMAGE_NOT_ALLOWED,
            message=f"镜像 {job.image} 不在允许列表中",
            details={"image": job.image, "allowed": allowed_images},
        )


def validate_all(
    job: TrainingJob,
    quota: QuotaView | None = None,
    allowed_images: list[str] | None = None,
) -> None:
    """Run every hard check in order. Raises on the first failure."""
    validate_required_fields(job)
    validate_reserved_labels(job)
    validate_resources(job)
    validate_multinode_checkpoint(job)
    validate_image_allowed(job, allowed_images)
    validate_quota(job, quota)
