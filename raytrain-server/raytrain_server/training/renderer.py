"""
RayJob renderer: turn a validated TrainingJob intent into a KubeRay
``ray.io/v1 RayJob`` dict, plus Kueue queue labels and platform-reserved
labels/annotations.

Pure function — no K8s calls. The output dict is what gets applied (or printed
for --dry-run). Every rule here is covered by tests/test_training_renderer.py.

Rules implemented (per spec "RayJob 渲染规则"):
  - CPU-only head (configurable cpu/mem)
  - workerGroupSpecs: replicas = nodes, each requests gpusPerNode
  - multi-node: add RDMA resource + NCCL env
  - gpuType node affinity
  - inject TRAIN_* / NCCL_* / RAY_object_spilling_config env
  - standard mounts: /data (ro), /checkpoints (rw), /scratch (emptyDir)
  - reserved labels/annotations (creator/project/quota/gpuType/queue/priority)
  - submissionMode K8sJobMode, shutdownAfterJobFinishes, ttl
"""
from __future__ import annotations

import json
from typing import Any

from . import labels as L
from .domain import CodeSourceKind, MountMode, TrainingJob

RAY_VERSION = "2.54.1"
RDMA_RESOURCE = "rdma/mlnx_shared"

# Standard in-container paths (spec storage convention)
DATA_PATH = "/data"
CHECKPOINT_PATH = "/checkpoints"
SCRATCH_PATH = "/scratch"


def _platform_labels(job: TrainingJob) -> dict[str, str]:
    return {
        L.LABEL_CREATOR: job.creator,
        L.LABEL_CREATOR_ID: job.creator_id,
        L.LABEL_PROJECT: job.project,
        L.LABEL_TENANT: job.tenant,
        L.LABEL_QUOTA_GROUP: job.quota_group,
        L.LABEL_GPU_TYPE: job.resources.gpu_type,
        L.LABEL_QUEUE: job.queue,
        L.LABEL_PRIORITY: str(job.priority),
        L.LABEL_JOB_ID: job.job_id,
        L.LABEL_RUN_ID: job.run_id,
        # Kueue admission
        L.KUEUE_QUEUE_NAME: job.queue,
    }


def _platform_annotations(job: TrainingJob) -> dict[str, str]:
    return {
        L.ANNO_NODES: str(job.resources.nodes),
        L.ANNO_GPUS_PER_NODE: str(job.resources.gpus_per_node),
        L.ANNO_NUM_WORKERS: str(job.resources.num_workers),
        L.ANNO_IMAGE: job.image,
        L.ANNO_DATA_PATH: DATA_PATH,
        L.ANNO_CHECKPOINT_PATH: CHECKPOINT_PATH,
    }


def _spilling_config() -> str:
    return json.dumps(
        {
            "type": "filesystem",
            "params": {"directory_path": SCRATCH_PATH},
        }
    )


def _train_env(job: TrainingJob) -> list[dict[str, str]]:
    r = job.resources
    env: dict[str, str] = {
        "TRAIN_NODES": str(r.nodes),
        "TRAIN_GPUS_PER_NODE": str(r.gpus_per_node),
        "TRAIN_NUM_WORKERS": str(r.num_workers),
        "TRAIN_DATA_PATH": DATA_PATH,
        "TRAIN_CHECKPOINT_PATH": CHECKPOINT_PATH,
        "NCCL_DEBUG": "WARN",
        "RAY_object_spilling_config": _spilling_config(),
    }
    if r.is_multi_node:
        # RDMA fabric available → keep IB enabled; single node → disable IB.
        env["NCCL_IB_DISABLE"] = "0"
        env["NCCL_SOCKET_IFNAME"] = "eth0"
    else:
        env["NCCL_IB_DISABLE"] = "1"
    # user env wins, but cannot clobber reserved keys (validated earlier)
    env.update(job.env)
    return [{"name": k, "value": str(v)} for k, v in env.items()]


def _gpu_affinity(job: TrainingJob) -> dict[str, Any]:
    return {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {
                        "matchExpressions": [
                            {
                                "key": L.NODE_GPU_LABEL,
                                "operator": "In",
                                "values": [job.resources.gpu_type],
                            }
                        ]
                    }
                ]
            }
        }
    }


def _volumes_and_mounts(job: TrainingJob) -> tuple[list[dict], list[dict]]:
    """Build (volumes, volumeMounts) for the worker containers.

    /scratch is always an emptyDir for Ray spilling. /data and /checkpoints
    come from datasets / checkpoint config (PVC or objectstore-backed)."""
    volumes: list[dict] = [
        {"name": "scratch", "emptyDir": {}},
        {"name": "dshm", "emptyDir": {"medium": "Memory"}},
    ]
    mounts: list[dict] = [
        {"name": "scratch", "mountPath": SCRATCH_PATH},
        {"name": "dshm", "mountPath": "/dev/shm"},
    ]

    # checkpoint PVC (only when pvc://). objectstore checkpoints need no mount.
    ck = job.checkpoint
    if ck.uri.startswith("pvc://"):
        claim = ck.uri[len("pvc://"):]
        volumes.append(
            {"name": "checkpoints", "persistentVolumeClaim": {"claimName": claim}}
        )
        mounts.append({"name": "checkpoints", "mountPath": ck.mount_path})

    # dataset PVCs
    for i, ds in enumerate(job.datasets):
        if ds.uri.startswith("pvc://"):
            claim = ds.uri[len("pvc://"):]
            vname = f"data-{i}"
            volumes.append(
                {"name": vname, "persistentVolumeClaim": {
                    "claimName": claim, "readOnly": ds.mode == MountMode.RO}}
            )
            mounts.append(
                {"name": vname, "mountPath": ds.mount_path,
                 "readOnly": ds.mode == MountMode.RO}
            )
    return volumes, mounts


def _worker_container(job: TrainingJob, volume_mounts: list[dict]) -> dict[str, Any]:
    r = job.resources
    requests = {
        "cpu": str(r.cpus_per_node),
        "memory": f"{r.memory_per_node_gi}Gi",
    }
    limits = dict(requests)
    if r.gpus_per_node > 0:
        requests["nvidia.com/gpu"] = str(r.gpus_per_node)
        limits["nvidia.com/gpu"] = str(r.gpus_per_node)
    if r.is_multi_node:
        requests[RDMA_RESOURCE] = "1"
        limits[RDMA_RESOURCE] = "1"
    return {
        "name": "ray-worker",
        "image": job.image,
        "imagePullPolicy": "IfNotPresent",
        "env": _train_env(job),
        "resources": {"requests": requests, "limits": limits},
        "volumeMounts": volume_mounts,
    }


def _head_container(job: TrainingJob) -> dict[str, Any]:
    r = job.resources
    return {
        "name": "ray-head",
        "image": job.image,
        "imagePullPolicy": "IfNotPresent",
        "env": _train_env(job),
        "resources": {
            "requests": {"cpu": str(r.head_cpu), "memory": f"{r.head_memory_gi}Gi"},
            "limits": {"cpu": str(r.head_cpu * 2), "memory": f"{r.head_memory_gi * 2}Gi"},
        },
        "volumeMounts": [{"name": "dshm", "mountPath": "/dev/shm"}],
    }


def render_rayjob(job: TrainingJob) -> dict[str, Any]:
    """Render the full RayJob dict. Caller is responsible for having validated."""
    volumes, mounts = _volumes_and_mounts(job)

    # Merge platform + user labels; platform wins (validation already rejected
    # user attempts to set reserved keys, but we enforce precedence anyway).
    merged_labels = {**job.labels, **_platform_labels(job)}
    merged_annos = {**job.annotations, **_platform_annotations(job)}

    head_group = {
        "rayStartParams": {"dashboard-host": "0.0.0.0", "num-gpus": "0"},
        "template": {
            "metadata": {"labels": {**merged_labels, "raytrain.io/role": "head"}},
            "spec": {
                "restartPolicy": "Never",
                "containers": [_head_container(job)],
                "volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}],
            },
        },
    }

    worker_spec: dict[str, Any] = {
        "spec": {
            "restartPolicy": "Never",
            "containers": [_worker_container(job, mounts)],
            "volumes": volumes,
        }
    }
    if job.resources.gpus_per_node > 0:
        worker_spec["spec"]["affinity"] = _gpu_affinity(job)
    worker_group = {
        "groupName": "gpu-workers",
        "replicas": job.resources.nodes,
        "minReplicas": job.resources.nodes,
        "maxReplicas": job.resources.nodes,
        "rayStartParams": {"num-gpus": str(job.resources.gpus_per_node)},
        "template": {
            "metadata": {"labels": {**merged_labels, "raytrain.io/role": "worker"}},
            **worker_spec,
        },
    }

    return {
        "apiVersion": "ray.io/v1",
        "kind": "RayJob",
        "metadata": {
            "name": job.job_id or job.name,
            "namespace": job.namespace,
            "labels": merged_labels,
            "annotations": merged_annos,
        },
        "spec": {
            "submissionMode": "K8sJobMode",
            "shutdownAfterJobFinishes": True,
            "ttlSecondsAfterFinished": job.ttl_seconds_after_finished,
            "entrypoint": job.command,
            "rayClusterSpec": {
                "rayVersion": RAY_VERSION,
                "headGroupSpec": head_group,
                "workerGroupSpecs": [worker_group],
            },
        },
    }
