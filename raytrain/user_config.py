"""
Per-user client config at `~/.raytrain/config.yaml`.

Holds MinIO credentials, MLflow URI + credentials, default namespace, default
image overrides, etc. Never committed to a training repo.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml

DEFAULT_PATH = Path("~/.raytrain/config.yaml").expanduser()

DEFAULT_NAMESPACE = "ray-cluster-3"
DEFAULT_MLFLOW_URI = "http://mlflow.mlflow.svc.cluster.local:5000"
DEFAULT_MINIO_ENDPOINT = "http://172.31.16.3:30950"


@dataclass
class MinioConfig:
    endpoint: str = DEFAULT_MINIO_ENDPOINT
    access_key: str = ""
    secret_key: str = ""
    secure: bool = False
    region: str = "us-east-1"


@dataclass
class MlflowConfig:
    tracking_uri: str = DEFAULT_MLFLOW_URI
    username: str = ""
    password: str = ""


@dataclass
class UserConfig:
    user_name: str = ""
    namespace: str = DEFAULT_NAMESPACE
    minio: MinioConfig = field(default_factory=MinioConfig)
    mlflow: MlflowConfig = field(default_factory=MlflowConfig)

    # per-user output locations on MinIO. {user} is substituted on use.
    exp_bucket: str = "u-{user}-exp"
    scratch_bucket: str = "u-{user}-scratch"

    # shared bucket where client code zips are uploaded for working_dir sync
    code_bucket: str = "raytrain-code"

    # where to cache datasets on GPU worker nodes (already mounted into pods)
    node_cache_path: str = "/mnt/ray-cache"

    # default ray image override; if empty, manifest.image is used
    default_image: str = ""

    # M1: Platform shared mode (cluster_mode=shared). When non-empty, the CLI
    # talks to the raytrain Control Plane over HTTPS and does NOT touch K8s.
    # Required for shared mode; ignored in legacy per_job mode.
    submission_server: str = ""    # e.g. http://node-ip:30810
    token: str = ""                # raytrain JWT
    default_cluster_mode: str = "per_job"  # "per_job" | "shared"

    # shared mode: map of gpu_type -> ray head dashboard URL, e.g.
    # {"h20": "http://ray-shared-h20-head.ray-shared.svc:8265", "a100": "..."}.
    # Usually provided by the platform admin; edit config.yaml to set these.
    shared_clusters: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def load(path: Path | None = None) -> "UserConfig":
        p = path or DEFAULT_PATH
        if not p.exists():
            raise FileNotFoundError(
                f"user config not found at {p}. Run `raytrain configure` first."
            )
        with p.open() as f:
            raw = yaml.safe_load(f) or {}
        return UserConfig(
            user_name=raw.get("user_name", ""),
            namespace=raw.get("namespace", DEFAULT_NAMESPACE),
            minio=MinioConfig(**(raw.get("minio") or {})),
            mlflow=MlflowConfig(**(raw.get("mlflow") or {})),
            exp_bucket=raw.get("exp_bucket", "u-{user}-exp"),
            scratch_bucket=raw.get("scratch_bucket", "u-{user}-scratch"),
            code_bucket=raw.get("code_bucket", "raytrain-code"),
            node_cache_path=raw.get("node_cache_path", "/mnt/ray-cache"),
            default_image=raw.get("default_image", ""),
            submission_server=raw.get("submission_server", ""),
            token=raw.get("token", ""),
            default_cluster_mode=raw.get("default_cluster_mode", "per_job"),
            shared_clusters=raw.get("shared_clusters", {}) or {},
        )

    def save(self, path: Path | None = None) -> Path:
        p = path or DEFAULT_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w") as f:
            yaml.safe_dump(asdict(self), f, sort_keys=False)
        # restrict permissions, config carries credentials
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
        return p

    def user_exp_bucket(self) -> str:
        return self.exp_bucket.format(user=self.user_name)

    def user_scratch_bucket(self) -> str:
        return self.scratch_bucket.format(user=self.user_name)
