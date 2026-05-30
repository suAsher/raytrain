"""
Workspace orchestration: build the K8s objects for a user's long-lived dev
environment (CPU pod + RWX PVC + 4 IDEs exposed via a Service).

A Workspace is:
    - 1 Pod running the raytrain-workspace image (Jupyter + code-server +
      PyCharm Projector + sshd + raytrain CLI all baked in)
    - 1 RWX PVC mounted at /home/<user> so code survives pod restarts
    - 1 Service exposing the 4 IDE ports
    - injected: MinIO creds + a fresh raytrain token (so the in-pod CLI can
      submit jobs without the user configuring anything)

Naming convention (DNS-1123 safe):
    pod:     ws-<workspace_id>
    pvc:     ws-<user>-<name>         (one PVC reused across pod restarts)
    service: ws-<workspace_id>

This module only *builds* dicts; K8sClient applies them. That keeps it pure
and unit-testable without a cluster.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# IDE ports inside the workspace pod
PORT_JUPYTER = 8888
PORT_CODE_SERVER = 8080
PORT_PYCHARM = 8887
PORT_SSH = 22

_SAFE = re.compile(r"[^a-z0-9-]")


def sanitize(s: str, maxlen: int = 40) -> str:
    s = _SAFE.sub("-", s.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:maxlen] or "x"


@dataclass
class WorkspaceSpec:
    """Inputs needed to materialize a Workspace."""

    workspace_id: str          # short uuid-ish
    user: str
    tenant: str
    name: str                  # user-facing name
    image: str
    cpu: int = 4
    memory_gi: int = 8
    pvc_gi: int = 100
    namespace: str = "raytrain-workspaces"
    storage_class: str = "longhorn"
    # injected creds / config
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    submission_server: str = ""
    raytrain_token: str = ""
    mlflow_uri: str = ""
    enabled_ides: list[str] = field(default_factory=lambda: ["jupyter", "code", "ssh"])

    @property
    def pod_name(self) -> str:
        return f"ws-{self.workspace_id}"

    @property
    def pvc_name(self) -> str:
        return f"ws-{sanitize(self.user)}-{sanitize(self.name)}"

    @property
    def service_name(self) -> str:
        return f"ws-{self.workspace_id}"

    @property
    def labels(self) -> dict[str, str]:
        return {
            "raytrain.io/component": "workspace",
            "raytrain.io/workspace-id": self.workspace_id,
            "raytrain.io/user": sanitize(self.user),
            "raytrain.io/tenant": sanitize(self.tenant),
        }


def build_pvc_labels(spec: WorkspaceSpec) -> dict[str, str]:
    return {
        "raytrain.io/component": "workspace-pvc",
        "raytrain.io/user": sanitize(spec.user),
    }


def build_pod_manifest(spec: WorkspaceSpec) -> dict[str, Any]:
    """Build the Workspace Pod manifest (plain dict for the K8s API)."""
    env = [
        {"name": "RAYTRAIN_USER", "value": spec.user},
        {"name": "RAYTRAIN_TENANT", "value": spec.tenant},
        {"name": "RAYTRAIN_WORKSPACE_ID", "value": spec.workspace_id},
        {"name": "RAYTRAIN_ENABLED_IDES", "value": ",".join(spec.enabled_ides)},
        {"name": "HOME", "value": f"/home/{spec.user}"},
    ]
    # Inject creds so the in-pod raytrain CLI works with zero user setup.
    if spec.minio_endpoint:
        env += [
            {"name": "AWS_ENDPOINT_URL", "value": spec.minio_endpoint},
            {"name": "S3_ENDPOINT_URL", "value": spec.minio_endpoint},
            {"name": "AWS_ACCESS_KEY_ID", "value": spec.minio_access_key},
            {"name": "AWS_SECRET_ACCESS_KEY", "value": spec.minio_secret_key},
        ]
    if spec.submission_server:
        env.append(
            {"name": "RAYTRAIN_SUBMISSION_SERVER", "value": spec.submission_server}
        )
    if spec.raytrain_token:
        env.append({"name": "RAYTRAIN_TOKEN", "value": spec.raytrain_token})
    if spec.mlflow_uri:
        env.append({"name": "MLFLOW_TRACKING_URI", "value": spec.mlflow_uri})

    ports = [
        {"name": "jupyter", "containerPort": PORT_JUPYTER},
        {"name": "code-server", "containerPort": PORT_CODE_SERVER},
        {"name": "pycharm", "containerPort": PORT_PYCHARM},
        {"name": "ssh", "containerPort": PORT_SSH},
    ]

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": spec.pod_name,
            "namespace": spec.namespace,
            "labels": spec.labels,
        },
        "spec": {
            "restartPolicy": "Always",
            "containers": [
                {
                    "name": "workspace",
                    "image": spec.image,
                    "imagePullPolicy": "IfNotPresent",
                    "env": env,
                    "ports": ports,
                    "resources": {
                        "requests": {
                            "cpu": str(spec.cpu),
                            "memory": f"{spec.memory_gi}Gi",
                        },
                        "limits": {
                            "cpu": str(spec.cpu),
                            "memory": f"{spec.memory_gi}Gi",
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
                    "emptyDir": {"medium": "Memory", "sizeLimit": "2Gi"},
                },
            ],
        },
    }


def build_service_manifest(spec: WorkspaceSpec) -> dict[str, Any]:
    """ClusterIP Service exposing the 4 IDE ports. Ingress (subdomain routing)
    is layered on top by the deploy templates; here we keep it simple."""
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
            "selector": {"raytrain.io/workspace-id": spec.workspace_id},
            "ports": [
                {"name": "jupyter", "port": PORT_JUPYTER, "targetPort": PORT_JUPYTER},
                {"name": "code-server", "port": PORT_CODE_SERVER, "targetPort": PORT_CODE_SERVER},
                {"name": "pycharm", "port": PORT_PYCHARM, "targetPort": PORT_PYCHARM},
                {"name": "ssh", "port": PORT_SSH, "targetPort": PORT_SSH},
            ],
        },
    }


def build_ide_urls(spec: WorkspaceSpec, base_domain: str) -> dict[str, str]:
    """Build user-facing IDE URLs given a wildcard base domain.

    Example base_domain = "raytrain.example.com" →
        jupyter:    https://ws-<id>.raytrain.example.com/jupyter/
        code:       https://ws-<id>.raytrain.example.com/code-server/
        pycharm:    https://ws-<id>.raytrain.example.com/pycharm/
        ssh:        ssh://ws-<id>.raytrain.example.com:22
    """
    host = f"ws-{spec.workspace_id}.{base_domain}" if base_domain else ""
    if not host:
        return {}
    return {
        "jupyter": f"https://{host}/jupyter/",
        "code": f"https://{host}/code-server/",
        "pycharm": f"https://{host}/pycharm/",
        "ssh": f"ssh://{host}:{PORT_SSH}",
    }
