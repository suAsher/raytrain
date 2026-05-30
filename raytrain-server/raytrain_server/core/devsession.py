"""
DevSession orchestration: a short-lived GPU pod bound to a Workspace.

Difference from Workspace:
    - has GPU (1..8 cards)
    - uses the GPU training image (not the CPU IDE base)
    - mounts the SAME PVC as its parent Workspace (shared code)
    - auto-reclaimed after idle timeout (heartbeat-driven)

The user debugs on a GPU here, then "submits to training" reusing the same
code. Reclaim keeps GPUs from being squatted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

PORT_JUPYTER = 8888
PORT_CODE_SERVER = 8080
PORT_SSH = 22

_SAFE = re.compile(r"[^a-z0-9-]")


def sanitize(s: str, maxlen: int = 40) -> str:
    s = _SAFE.sub("-", s.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:maxlen] or "x"


@dataclass
class DevSessionSpec:
    session_id: str
    workspace_id: str
    user: str
    tenant: str
    image: str            # GPU training image
    gpu_type: str
    gpu_count: int
    pvc_name: str         # parent Workspace's PVC (shared code)
    namespace: str = "raytrain-dev"
    cpu: int = 8
    memory_gi: int = 64
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    raytrain_token: str = ""
    mlflow_uri: str = ""
    enabled_ides: list[str] = field(default_factory=lambda: ["jupyter", "code", "ssh"])

    @property
    def pod_name(self) -> str:
        return f"dev-{self.session_id}"

    @property
    def service_name(self) -> str:
        return f"dev-{self.session_id}"

    @property
    def labels(self) -> dict[str, str]:
        return {
            "raytrain.io/component": "devsession",
            "raytrain.io/session-id": self.session_id,
            "raytrain.io/workspace-id": self.workspace_id,
            "raytrain.io/user": sanitize(self.user),
            "raytrain.io/tenant": sanitize(self.tenant),
            "raytrain.io/gpu-type": sanitize(self.gpu_type),
        }


def build_pod_manifest(spec: DevSessionSpec) -> dict[str, Any]:
    env = [
        {"name": "RAYTRAIN_USER", "value": spec.user},
        {"name": "RAYTRAIN_TENANT", "value": spec.tenant},
        {"name": "RAYTRAIN_SESSION_ID", "value": spec.session_id},
        {"name": "RAYTRAIN_WORKSPACE_ID", "value": spec.workspace_id},
        {"name": "RAYTRAIN_ENABLED_IDES", "value": ",".join(spec.enabled_ides)},
        {"name": "HOME", "value": f"/home/{spec.user}"},
        {"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"},
    ]
    if spec.minio_endpoint:
        env += [
            {"name": "AWS_ENDPOINT_URL", "value": spec.minio_endpoint},
            {"name": "S3_ENDPOINT_URL", "value": spec.minio_endpoint},
            {"name": "AWS_ACCESS_KEY_ID", "value": spec.minio_access_key},
            {"name": "AWS_SECRET_ACCESS_KEY", "value": spec.minio_secret_key},
        ]
    if spec.raytrain_token:
        env.append({"name": "RAYTRAIN_TOKEN", "value": spec.raytrain_token})
    if spec.mlflow_uri:
        env.append({"name": "MLFLOW_TRACKING_URI", "value": spec.mlflow_uri})

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": spec.pod_name,
            "namespace": spec.namespace,
            "labels": spec.labels,
        },
        "spec": {
            "restartPolicy": "Never",
            "nodeSelector": {"gpu": spec.gpu_type},
            "containers": [
                {
                    "name": "devsession",
                    "image": spec.image,
                    "imagePullPolicy": "IfNotPresent",
                    "env": env,
                    "ports": [
                        {"name": "jupyter", "containerPort": PORT_JUPYTER},
                        {"name": "code-server", "containerPort": PORT_CODE_SERVER},
                        {"name": "ssh", "containerPort": PORT_SSH},
                    ],
                    "resources": {
                        "requests": {
                            "cpu": str(spec.cpu),
                            "memory": f"{spec.memory_gi}Gi",
                            "nvidia.com/gpu": str(spec.gpu_count),
                        },
                        "limits": {
                            "cpu": str(spec.cpu),
                            "memory": f"{spec.memory_gi}Gi",
                            "nvidia.com/gpu": str(spec.gpu_count),
                        },
                    },
                    "volumeMounts": [
                        {"name": "home", "mountPath": f"/home/{spec.user}"},
                        {"name": "dshm", "mountPath": "/dev/shm"},
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "home",
                    "persistentVolumeClaim": {"claimName": spec.pvc_name},
                },
                {
                    "name": "dshm",
                    "emptyDir": {"medium": "Memory", "sizeLimit": "16Gi"},
                },
            ],
        },
    }


def build_service_manifest(spec: DevSessionSpec) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": spec.service_name,
            "namespace": spec.namespace,
            "labels": spec.labels,
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {"raytrain.io/session-id": spec.session_id},
            "ports": [
                {"name": "jupyter", "port": PORT_JUPYTER, "targetPort": PORT_JUPYTER},
                {"name": "code-server", "port": PORT_CODE_SERVER, "targetPort": PORT_CODE_SERVER},
                {"name": "ssh", "port": PORT_SSH, "targetPort": PORT_SSH},
            ],
        },
    }


def build_ide_urls(spec: DevSessionSpec, base_domain: str) -> dict[str, str]:
    host = f"dev-{spec.session_id}.{base_domain}" if base_domain else ""
    if not host:
        return {}
    return {
        "jupyter": f"https://{host}/jupyter/",
        "code": f"https://{host}/code-server/",
        "ssh": f"ssh://{host}:{PORT_SSH}",
    }
