"""
Render a RayJob manifest from a parsed `.raytrain.yaml` + user config + CLI flags.

The rendered YAML is submitted via the k8s API. The framework also uploads a
small ConfigMap containing the serialized manifest + the resolved "plan" (how
many nodes, which datasets, which args) so the driver inside the head pod can
read them without any CLI arguments that might not survive env interpolation.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .manifest import Manifest, DatasetMount
from .user_config import UserConfig


def _parse_memory_str(s: str) -> int:
    """Parse K8s memory string like '512Gi', '64Gi', '150Gi' to bytes."""
    s = s.strip()
    if s.endswith("Ti"):
        return int(float(s[:-2]) * 1024 ** 4)
    elif s.endswith("Gi"):
        return int(float(s[:-2]) * 1024 ** 3)
    elif s.endswith("Mi"):
        return int(float(s[:-2]) * 1024 ** 2)
    elif s.endswith("Ki"):
        return int(float(s[:-2]) * 1024)
    else:
        return int(s)

_TEMPLATES = Path(__file__).parent / "templates"
_jinja = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    undefined=StrictUndefined,
    autoescape=False,
    keep_trailing_newline=True,
    trim_blocks=False,
    lstrip_blocks=False,
)

RAY_VERSION = "2.54.1"
DEFAULT_TTL_AFTER_FINISH = 3600  # 1h for inspection before GC


@dataclass
class Plan:
    """Resolved numbers & paths that the head-pod driver will consume."""
    job_name: str
    run_id: str                # MLflow run id
    user: str
    repo_name: str
    num_nodes: int
    gpus_per_node: int
    cpus_per_node: int
    gpu_type: str
    config_name: str
    config_path: str           # path inside container
    save_path: str             # absolute path used as save_path option
    datasets: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    launcher_type: str = "native_ddp"
    launcher_entrypoint: str = ""
    launcher_args: list[str] = field(default_factory=list)
    launcher_env: dict[str, str] = field(default_factory=dict)
    workdir: str = ""
    extra_options: dict[str, Any] = field(default_factory=dict)
    data_source: dict[str, Any] | None = None  # serialized DataSource for Lance/Parquet streaming
    cpu_workers: int = 0  # CPU-only workers for Ray Data transforms
    # M0: code-as-submission via runtime_env.working_dir
    # When code_uri is set, RayJob template injects:
    #   runtimeEnvYAML.working_dir = code_uri
    # Driver reads the unzipped path from RAY_RUNTIME_ENV_WORKING_DIR.
    code_uri: str | None = None              # s3://raytrain-code/<user>/<job>.zip
    code_hash: str | None = None             # sha256 hex
    code_size_bytes: int = 0                 # for audit / display

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.__dict__, sort_keys=False)


@dataclass
class RenderInputs:
    manifest: Manifest
    user_cfg: UserConfig
    plan: Plan
    image: str
    service_account: str
    payload_configmap: str
    ttl_seconds_after_finished: int = DEFAULT_TTL_AFTER_FINISH
    object_store_memory_bytes: int = 0  # 0 = auto-compute from manifest

    @staticmethod
    def compute_object_store_bytes(manifest) -> int:
        """Compute object store memory from manifest resources.
        If object_store_memory is set explicitly, use it.
        Otherwise default to 30% of memory_per_node.
        """
        osm = manifest.resources.object_store_memory
        if osm:
            return _parse_memory_str(osm)
        # Default: 30% of memory_per_node
        mem_bytes = _parse_memory_str(manifest.resources.memory_per_node)
        return int(mem_bytes * 0.3)


def render_rayjob(inp: RenderInputs) -> str:
    """Render the RayJob YAML as a string."""
    m, u, p = inp.manifest, inp.user_cfg, inp.plan

    # Auto-compute object store memory if not explicitly set
    object_store_memory_bytes = inp.object_store_memory_bytes
    if object_store_memory_bytes == 0:
        if m.resources.object_store_memory:
            # Parse from manifest (e.g. "150Gi", "64Gi")
            osm = m.resources.object_store_memory
            if osm.endswith("Gi"):
                object_store_memory_bytes = int(osm[:-2]) * 1024 ** 3
            elif osm.endswith("Mi"):
                object_store_memory_bytes = int(osm[:-2]) * 1024 ** 2
            else:
                object_store_memory_bytes = int(osm)
        else:
            # Default: 30% of memory_per_node
            mem_str = m.resources.memory_per_node
            if mem_str.endswith("Gi"):
                total_bytes = int(mem_str[:-2]) * 1024 ** 3
            elif mem_str.endswith("Mi"):
                total_bytes = int(mem_str[:-2]) * 1024 ** 2
            else:
                total_bytes = 64 * 1024 ** 3  # fallback 64Gi
            object_store_memory_bytes = int(total_bytes * 0.3)

    env_vars = {
        # MinIO endpoint & related boto3 settings. Credentials come via secret.
        "AWS_ENDPOINT_URL": u.minio.endpoint,
        "AWS_DEFAULT_REGION": u.minio.region,
        "AWS_EC2_METADATA_DISABLED": "true",
        "S3_ENDPOINT_URL": u.minio.endpoint,
        # MLflow
        "MLFLOW_TRACKING_URI": u.mlflow.tracking_uri,
        "MLFLOW_S3_ENDPOINT_URL": u.minio.endpoint,
        # Required by the driver to find the plan
        "RAYTRAIN_RUN_ID": p.run_id,
        "RAYTRAIN_NAMESPACE": u.namespace,
        # Good NCCL defaults for debugging
        "NCCL_DEBUG": "WARN",
    }

    if p.data_source:
        env_vars["RAYTRAIN_DATA_SOURCE_TYPE"] = str(p.data_source.get("type", ""))
        env_vars["RAYTRAIN_DATA_SOURCE_URI"] = str(p.data_source.get("uri", ""))
        env_vars["RAYTRAIN_DATA_SOURCE_VERSION"] = str(
            p.data_source.get("version", "latest")
        )
        if p.data_source.get("filter"):
            env_vars["RAYTRAIN_DATA_SOURCE_FILTER"] = str(p.data_source["filter"])
        if p.data_source.get("columns"):
            env_vars["RAYTRAIN_DATA_SOURCE_COLUMNS"] = ",".join(
                str(c) for c in p.data_source["columns"]
            )

    # M0: expose code bundle metadata to driver / training subprocess. The
    # actual `working_dir` injection happens in the Jinja template (so Ray
    # itself can fetch the zip) — these env vars are for audit / debug.
    if p.code_uri:
        env_vars["RAYTRAIN_CODE_URI"] = p.code_uri
    if p.code_hash:
        env_vars["RAYTRAIN_CODE_HASH"] = p.code_hash
    if p.code_size_bytes:
        env_vars["RAYTRAIN_CODE_SIZE_BYTES"] = str(p.code_size_bytes)

    # Ray Train entrypoints launch child Ray actors; render launcher.env into all
    # Ray pods, not just the driver subprocess, so workers inherit the same
    # data/cache/NCCL settings.
    env_vars.update({str(k): str(v) for k, v in (p.launcher_env or {}).items()})

    # Secrets: credentials are injected via secretKeyRef, never rendered into YAML.
    env_from_secrets = [
        {"name": "AWS_ACCESS_KEY_ID",
         "secret": f"raytrain-creds-{p.job_name}", "key": "aws_access_key_id"},
        {"name": "AWS_SECRET_ACCESS_KEY",
         "secret": f"raytrain-creds-{p.job_name}", "key": "aws_secret_access_key"},
        {"name": "MINIO_ACCESS_KEY",
         "secret": f"raytrain-creds-{p.job_name}", "key": "aws_access_key_id"},
        {"name": "MINIO_SECRET_KEY",
         "secret": f"raytrain-creds-{p.job_name}", "key": "aws_secret_access_key"},
        {"name": "MLFLOW_TRACKING_USERNAME",
         "secret": f"raytrain-creds-{p.job_name}", "key": "mlflow_username"},
        {"name": "MLFLOW_TRACKING_PASSWORD",
         "secret": f"raytrain-creds-{p.job_name}", "key": "mlflow_password"},
    ]

    ctx = {
        "job_name": p.job_name,
        "namespace": u.namespace,
        "user": u.user_name,
        "repo_name": p.repo_name,
        "gpu_type": p.gpu_type,
        "config_name": p.config_name,
        "mlflow_run_id": p.run_id,
        "num_nodes": p.num_nodes,
        "gpus_per_node": p.gpus_per_node,
        "cpus_per_node": p.cpus_per_node,
        "memory_per_node": m.resources.memory_per_node,
        "shm_size": m.resources.shm_size,
        "image": inp.image,
        "service_account": inp.service_account,
        "payload_configmap": inp.payload_configmap,
        "ttl_seconds_after_finished": inp.ttl_seconds_after_finished,
        "ray_version": RAY_VERSION,
        "env_vars": env_vars,
        "env_from_secrets": env_from_secrets,
        "object_store_memory_bytes": object_store_memory_bytes,
        "cpu_workers": p.cpu_workers,
        # M0: code-as-submission（None 时模板不写 working_dir）
        "code_uri": p.code_uri,
        "code_hash": p.code_hash,
        "code_size_bytes": p.code_size_bytes,
    }
    template = _jinja.get_template("rayjob.yaml.j2")
    return template.render(**ctx)


def payload_configmap_body(manifest: Manifest, plan: Plan) -> dict[str, Any]:
    """
    Build the ConfigMap that carries the manifest + resolved plan into the pods.
    The driver reads /raytrain/manifest.yaml and /raytrain/plan.yaml.
    """
    from dataclasses import asdict
    manifest_dict = {
        "apiVersion": manifest.api_version,
        "image": manifest.image,
        "workdir": manifest.workdir,
        "launcher": asdict(manifest.launcher),
        "resources": asdict(manifest.resources),
        "datasets": [{"name": d.name, "s3": d.s3, "mount": d.mount} for d in manifest.datasets],
        "data_source": asdict(manifest.data_source) if manifest.data_source else None,
        "artifacts": list(manifest.artifacts),
        "name": manifest.name,
        "repo_name": manifest.repo_name,
        "save_path": manifest.save_path,
    }
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"raytrain-payload-{plan.job_name}"},
        "data": {
            "manifest.yaml": yaml.safe_dump(manifest_dict, sort_keys=False),
            "plan.yaml": plan.to_yaml(),
        },
    }


def creds_secret_body(user_cfg: UserConfig, job_name: str) -> dict[str, Any]:
    """Secret with MinIO + MLflow creds, short-lived, tied to this job name."""
    def b64(s: str) -> str:
        return base64.b64encode(s.encode()).decode()

    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": f"raytrain-creds-{job_name}"},
        "data": {
            "aws_access_key_id":     b64(user_cfg.minio.access_key),
            "aws_secret_access_key": b64(user_cfg.minio.secret_key),
            "mlflow_username":       b64(user_cfg.mlflow.username),
            "mlflow_password":       b64(user_cfg.mlflow.password),
        },
    }
