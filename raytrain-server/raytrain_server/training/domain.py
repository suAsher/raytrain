"""
Domain models for the training workbench.

Pure dataclasses + enums — no K8s, no FastAPI, no DB. These represent the
user's *training intent* and the platform's resolved view of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class JobState(str, Enum):
    """Aggregated lifecycle state (per spec state machine)."""

    DRAFT = "Draft"
    SUBMITTED = "Submitted"
    QUEUED = "Queued"
    ADMITTED = "Admitted"
    STARTING = "Starting"
    RUNNING = "Running"
    SUCCEEDED = "Succeeded"
    FAILED = "Failed"
    CANCELLING = "Cancelling"
    CANCELLED = "Cancelled"
    CLEANING = "Cleaning"
    UNKNOWN = "Unknown"


class CodeSourceKind(str, Enum):
    GIT = "git"           # clone a git repo at submit
    WORKING_DIR = "working_dir"  # zip already uploaded to object store
    IMAGE = "image"       # code baked into the image


class MountMode(str, Enum):
    RO = "ReadOnly"
    RWO = "ReadWriteOnce"
    RWX = "ReadWriteMany"


# --------------------------------------------------------------------------- #
# Sub-objects of a training intent
# --------------------------------------------------------------------------- #


@dataclass
class CodeSource:
    kind: CodeSourceKind = CodeSourceKind.IMAGE
    # git: repo + ref; working_dir: s3 uri; image: nothing extra
    git_repo: str = ""
    git_ref: str = "main"
    working_dir_uri: str = ""


@dataclass
class DatasetMount:
    name: str
    uri: str               # s3:// or pvc://<claim>
    mount_path: str        # e.g. /data
    mode: MountMode = MountMode.RO


@dataclass
class CheckpointConfig:
    # path inside the container where the job writes checkpoints
    mount_path: str = "/checkpoints"
    # backing store: pvc://<claim> or s3://bucket/prefix
    uri: str = ""
    # access mode of the backing PVC (used to validate multi-node)
    mode: MountMode = MountMode.RWX


@dataclass
class ResourceSpec:
    gpu_type: str = "h20"
    nodes: int = 1
    gpus_per_node: int = 1
    cpus_per_node: int = 8
    memory_per_node_gi: int = 64
    # Ray head resources (CPU-only)
    head_cpu: int = 2
    head_memory_gi: int = 8

    @property
    def num_workers(self) -> int:
        return self.nodes

    @property
    def total_gpus(self) -> int:
        return self.nodes * self.gpus_per_node

    @property
    def is_multi_node(self) -> bool:
        return self.nodes > 1


@dataclass
class TrainingJob:
    """The user's training intent + resolved platform context."""

    # identity / context (resolved by the service from the caller)
    name: str
    creator: str
    creator_id: str
    project: str
    tenant: str
    quota_group: str
    queue: str
    namespace: str

    # what to run
    image: str
    command: str                     # the shell entrypoint, e.g. "python train.py"
    code_source: CodeSource = field(default_factory=CodeSource)

    # resources
    resources: ResourceSpec = field(default_factory=ResourceSpec)
    priority: int = 0

    # data & checkpoints
    datasets: list[DatasetMount] = field(default_factory=list)
    checkpoint: CheckpointConfig = field(default_factory=CheckpointConfig)

    # extras
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)

    # ids (set by service)
    job_id: str = ""
    run_id: str = ""

    # lifecycle knobs
    ttl_seconds_after_finished: int = 3600
