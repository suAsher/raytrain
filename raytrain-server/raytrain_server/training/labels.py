"""
Centralized definition of every platform-reserved label / annotation key.

Per spec: "所有平台保留 label/annotation 必须集中定义" and "用户自定义
labels/annotations 不能覆盖平台保留前缀". Nothing else in the codebase should
hardcode these strings — import from here.
"""
from __future__ import annotations

# Single reserved prefix. Anything starting with this is platform-owned and a
# user submission may NOT set it.
RESERVED_PREFIX = "raytrain.io/"

# --- identity / ownership ---
LABEL_CREATOR = "raytrain.io/creator"
LABEL_CREATOR_ID = "raytrain.io/creator-id"
LABEL_PROJECT = "raytrain.io/project"
LABEL_TENANT = "raytrain.io/tenant"
LABEL_QUOTA_GROUP = "raytrain.io/quota-group"

# --- scheduling / resources ---
LABEL_GPU_TYPE = "raytrain.io/gpu-type"
LABEL_QUEUE = "raytrain.io/queue"
LABEL_PRIORITY = "raytrain.io/priority"
LABEL_JOB_ID = "raytrain.io/job-id"
LABEL_RUN_ID = "raytrain.io/run-id"

# --- annotations (free-form values, not for selecting) ---
ANNO_NODES = "raytrain.io/nodes"
ANNO_GPUS_PER_NODE = "raytrain.io/gpus-per-node"
ANNO_NUM_WORKERS = "raytrain.io/num-workers"
ANNO_IMAGE = "raytrain.io/image"
ANNO_DATA_PATH = "raytrain.io/data-path"
ANNO_CHECKPOINT_PATH = "raytrain.io/checkpoint-path"

# --- Kueue integration (external, well-known) ---
KUEUE_QUEUE_NAME = "kueue.x-k8s.io/queue-name"
KUEUE_PRIORITY_CLASS = "kueue.x-k8s.io/priority-class"

# --- node label key used for GPU-type affinity ---
NODE_GPU_LABEL = "raytrain.io/gpu-type"


def is_reserved(key: str) -> bool:
    """True if ``key`` is owned by the platform and users may not set it."""
    return key.startswith(RESERVED_PREFIX) or key.startswith("kueue.x-k8s.io/")


def reject_reserved(user_kv: dict[str, str]) -> list[str]:
    """Return the list of user-supplied keys that collide with reserved
    prefixes. Empty list == OK."""
    return [k for k in (user_kv or {}) if is_reserved(k)]
