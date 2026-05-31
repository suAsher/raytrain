"""
Seed a fresh platform with demonstrative job records so the console isn't empty
on first boot. Idempotent and opt-out via RAYTRAIN_SEED_DEMO=false.

These are real platform JobStore records (not browser mocks) — they list,
filter, open in detail, cancel, retry, and roll up into queues/overview just
like a user-submitted job. Real user submissions append alongside them.
"""
from __future__ import annotations

import logging
import time

from .jobs_store import FailureInfo, JobMounts, JobResources, PlatformJob, get_job_store

log = logging.getLogger(__name__)


def _mk(idx, name, status, project, queue, gpu_type, nodes, gpn, user, mins_ago,
        image, failure=None, experiment=""):
    now = time.time()
    created = now - mins_ago * 60
    started = 0.0 if status == "Queued" else created + 60
    finished = 0.0
    if status in ("Succeeded", "Failed", "Cancelled"):
        finished = created + mins_ago * 0.7 * 60
    return PlatformJob(
        id=f"job-{idx:04d}",
        name=name,
        user=user,
        tenant="research",
        project=project,
        queue=queue,
        quota_group=project + "-qg",
        priority="high" if idx % 5 == 0 else "normal",
        status=status,
        image=image,
        entrypoint=f"python tools/train.py --config configs/{project}/default.py",
        working_dir=f"/workspace/{project}",
        git_ref="main@a1f3",
        env={"OMP_NUM_THREADS": "8", "NCCL_IB_DISABLE": "0" if nodes > 1 else "1"},
        resources=JobResources(
            gpu_type=gpu_type, nodes=nodes, gpus_per_node=gpn,
            mem_per_gpu_gi=64 if (failure and failure.category == "OOMKilled") else 96,
            rdma=nodes > 1,
        ),
        mounts=JobMounts(
            dataset_uri=f"minio://datasets/{project}",
            checkpoint_uri=f"minio://checkpoints/{name}",
            checkpoint_shared=True,
        ),
        failure=failure,
        description=f"Training run for {project}.",
        experiment=experiment or project,
        created_at=created,
        started_at=started,
        finished_at=finished,
    )


_OOM = FailureInfo(
    category="OOMKilled",
    summary="worker-0 内存超限被杀 (exit 137)",
    detail="GPU 显存达到 78.2/80.0 GiB 后 CUDA OOM，进程被 OOMKilled。建议降低 batch size，或把 memory-per-gpu 调高后 Retry。",
    container="worker-0",
    log_anchor=9,
)
_IMG = FailureInfo(
    category="ImagePullBackOff",
    summary="镜像拉取失败：tag 不存在或需要认证",
    detail="ray-head 无法拉取镜像；该 tag 在仓库中不存在。请在创建向导 Step 2 选择有效镜像后重试。",
    container="ray-head",
    log_anchor=3,
)


def seed_demo_jobs() -> int:
    store = get_job_store()
    if store.list_visible("", "", is_admin=True):
        return 0  # already has data
    seeds = [
        _mk(1, "sslod26-pretrain-base", "Running", "sslod26", "h20-research", "H20", 2, 8, "asher", 32, "raytrain/sslod26:cu124-v3"),
        _mk(2, "pointcept-scannet-semseg", "Running", "pointcept", "h20-shared", "H20", 1, 8, "zhangsan", 65, "raytrain/pointcept:cu124-v3"),
        _mk(3, "occ-world-bevformer-large", "Queued", "occ-world", "a100-research", "A100", 4, 8, "lisi", 12, "raytrain/occworld:cu121-v2"),
        _mk(4, "nuscenes-det-centerpoint", "Failed", "nuscenes-det", "h20-shared", "H20", 2, 8, "wangwu", 48, "raytrain/nusc:cu124-v1", _OOM),
        _mk(5, "pointcept-s3dis-ablation-2", "Failed", "pointcept", "h20-shared", "H20", 1, 4, "intern-01", 90, "raytrain/pointcept:bad-tag", _IMG),
        _mk(6, "sslod26-finetune-nuscenes", "Succeeded", "sslod26", "h20-research", "H20", 2, 8, "asher", 240, "raytrain/sslod26:cu124-v3"),
        _mk(7, "occ-world-baseline", "Succeeded", "occ-world", "a100-research", "A100", 1, 8, "lisi", 320, "raytrain/occworld:cu121-v2"),
        _mk(8, "pointcept-scannet-sweep-lr", "Cancelled", "pointcept", "h20-shared", "H20", 1, 8, "zhangsan", 150, "raytrain/pointcept:cu124-v3"),
        _mk(9, "nuscenes-det-pillarnext", "Queued", "nuscenes-det", "h20-shared", "H20", 2, 8, "wangwu", 7, "raytrain/nusc:cu124-v1"),
        _mk(10, "sslod26-pretrain-large", "Running", "sslod26", "h20-research", "H20", 4, 8, "asher", 180, "raytrain/sslod26:cu124-v3"),
        _mk(11, "cpu-preprocess-lance", "Running", "pointcept", "cpu-batch", "CPU-only", 1, 0, "intern-01", 22, "raytrain/preprocess:v1"),
        _mk(12, "occ-world-debug-smoke", "Succeeded", "occ-world", "a100-research", "A100", 1, 1, "lisi", 410, "raytrain/occworld:cu121-v2"),
    ]
    for s in seeds:
        store.create(s)
    log.info("seeded %d demo jobs", len(seeds))
    return len(seeds)
