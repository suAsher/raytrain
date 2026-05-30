"""
Thin wrapper over the Kubernetes Python client for Workspace / DevSession pod
lifecycle.

The control plane is the ONLY component that holds K8s credentials. Users
never touch K8s — they ask the API to create/stop a Workspace and the server
does it with its own ServiceAccount.

Design:
    - Construct lazily; ``load_config`` auto-detects in-cluster vs kubeconfig.
    - Methods return plain dicts / status strings so the API layer stays
      framework-agnostic and easy to mock in tests.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class K8sClient:
    """Create / inspect / delete Workspace + DevSession pods + their PVCs."""

    def __init__(self, in_cluster: bool | None = None) -> None:
        self._in_cluster = in_cluster
        self._core = None  # lazy

    # -- lazy setup -----------------------------------------------------------

    def _ensure(self) -> None:
        if self._core is not None:
            return
        from kubernetes import client, config

        if self._in_cluster is True:
            config.load_incluster_config()
        elif self._in_cluster is False:
            config.load_kube_config()
        else:
            try:
                config.load_incluster_config()
            except Exception:
                config.load_kube_config()
        self._core = client.CoreV1Api()

    @property
    def core(self):
        self._ensure()
        return self._core

    # -- PVC ------------------------------------------------------------------

    def ensure_pvc(
        self,
        name: str,
        namespace: str,
        size_gi: int,
        storage_class: str = "longhorn",
        access_mode: str = "ReadWriteMany",
        labels: dict[str, str] | None = None,
    ) -> str:
        """Create the PVC if missing; return its name. Idempotent."""
        from kubernetes import client
        from kubernetes.client.rest import ApiException

        try:
            self.core.read_namespaced_persistent_volume_claim(name, namespace)
            return name  # already exists
        except ApiException as e:
            if e.status != 404:
                raise

        body = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=name, labels=labels or {}),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=[access_mode],
                storage_class_name=storage_class,
                resources=client.V1ResourceRequirements(
                    requests={"storage": f"{size_gi}Gi"}
                ),
            ),
        )
        self.core.create_namespaced_persistent_volume_claim(namespace, body)
        return name

    def delete_pvc(self, name: str, namespace: str) -> None:
        from kubernetes.client.rest import ApiException

        try:
            self.core.delete_namespaced_persistent_volume_claim(name, namespace)
        except ApiException as e:
            if e.status != 404:
                raise

    # -- Pod ------------------------------------------------------------------

    def create_pod(self, manifest: dict[str, Any], namespace: str) -> str:
        """Create a pod from a plain dict manifest. Returns pod name."""
        from kubernetes.client.rest import ApiException

        name = manifest["metadata"]["name"]
        try:
            self.core.create_namespaced_pod(namespace, manifest)
        except ApiException as e:
            if e.status == 409:
                raise RuntimeError(
                    f"pod {name!r} already exists in {namespace}"
                ) from e
            raise
        return name

    def delete_pod(self, name: str, namespace: str) -> None:
        from kubernetes.client.rest import ApiException

        try:
            self.core.delete_namespaced_pod(
                name, namespace, grace_period_seconds=10
            )
        except ApiException as e:
            if e.status != 404:
                raise

    def pod_phase(self, name: str, namespace: str) -> str:
        """Return pod phase (Pending/Running/Succeeded/Failed) or 'NotFound'."""
        from kubernetes.client.rest import ApiException

        try:
            pod = self.core.read_namespaced_pod(name, namespace)
        except ApiException as e:
            if e.status == 404:
                return "NotFound"
            raise
        return (pod.status.phase if pod.status else None) or "Unknown"

    def pod_ip(self, name: str, namespace: str) -> str | None:
        from kubernetes.client.rest import ApiException

        try:
            pod = self.core.read_namespaced_pod(name, namespace)
        except ApiException:
            return None
        return pod.status.pod_ip if pod.status else None

    # -- Service / Ingress ----------------------------------------------------

    def ensure_service(self, manifest: dict[str, Any], namespace: str) -> str:
        from kubernetes.client.rest import ApiException

        name = manifest["metadata"]["name"]
        try:
            self.core.create_namespaced_service(namespace, manifest)
        except ApiException as e:
            if e.status != 409:
                raise
        return name

    def delete_service(self, name: str, namespace: str) -> None:
        from kubernetes.client.rest import ApiException

        try:
            self.core.delete_namespaced_service(name, namespace)
        except ApiException as e:
            if e.status != 404:
                raise
