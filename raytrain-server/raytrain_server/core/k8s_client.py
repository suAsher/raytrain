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

    def pod_container_status(self, name: str, namespace: str) -> tuple[str, str | None]:
        """Inspect the pod's container states to surface real readiness.

        Returns (kind, reason) where kind ∈ {ready, waiting, terminated, none,
        notfound}:
          - waiting    → reason like ImagePullBackOff / ErrImagePull / CrashLoopBackOff
          - terminated → reason like Error / OOMKilled (Failed pods)
          - ready      → all containers ready
          - none       → no container statuses yet (scheduling)
        Used by WorkspaceService.derive_state to distinguish "not ready" causes.
        """
        from kubernetes.client.rest import ApiException

        try:
            pod = self.core.read_namespaced_pod(name, namespace)
        except ApiException as e:
            if e.status == 404:
                return ("notfound", None)
            raise
        statuses = (pod.status.container_statuses if pod.status else None) or []
        if not statuses:
            return ("none", None)
        for cs in statuses:
            state = cs.state
            if state and state.waiting and state.waiting.reason:
                r = state.waiting.reason
                # ContainerCreating/PodInitializing are benign; treat as none
                if r in ("ContainerCreating", "PodInitializing"):
                    continue
                return ("waiting", r)
            if state and state.terminated and state.terminated.reason:
                if state.terminated.exit_code not in (0, None):
                    return ("terminated", state.terminated.reason)
        if all(cs.ready for cs in statuses):
            return ("ready", None)
        return ("none", None)

    def wait_pod_deleted(self, name: str, namespace: str, timeout_s: int = 60) -> bool:
        """Poll until the pod is gone (NotFound) or timeout. Returns True if
        deleted within the window. Used before re-creating a Terminating pod."""
        import time as _t

        deadline = _t.time() + max(0, timeout_s)
        while _t.time() < deadline:
            if self.pod_phase(name, namespace) == "NotFound":
                return True
            _t.sleep(1.0)
        return self.pod_phase(name, namespace) == "NotFound"

    def service_node_ports(self, name: str, namespace: str) -> dict[str, int]:
        """Return {port_name: nodePort} for a NodePort Service. Empty if the
        service is missing or has no nodePorts assigned yet."""
        from kubernetes.client.rest import ApiException

        try:
            svc = self.core.read_namespaced_service(name, namespace)
        except ApiException as e:
            if e.status == 404:
                return {}
            raise
        out: dict[str, int] = {}
        for p in (svc.spec.ports if svc.spec else []) or []:
            if getattr(p, "node_port", None):
                out[p.name or str(p.port)] = int(p.node_port)
        return out

    def node_address(self, pod_name: str, namespace: str) -> str | None:
        """Best-effort externally-usable host for the node a pod runs on.
        Prefers ExternalIP, falls back to InternalIP. Used for NodePort URLs
        when workspace_node_host is not configured."""
        from kubernetes.client.rest import ApiException

        try:
            pod = self.core.read_namespaced_pod(pod_name, namespace)
        except ApiException:
            return None
        node_name = pod.spec.node_name if pod.spec else None
        if not node_name:
            return None
        try:
            node = self.core.read_node(node_name)
        except ApiException:
            return None
        addrs = (node.status.addresses if node.status else None) or []
        external = next((a.address for a in addrs if a.type == "ExternalIP"), None)
        internal = next((a.address for a in addrs if a.type == "InternalIP"), None)
        return external or internal

    def pod_ip(self, name: str, namespace: str) -> str | None:
        from kubernetes.client.rest import ApiException

        try:
            pod = self.core.read_namespaced_pod(name, namespace)
        except ApiException:
            return None
        return pod.status.pod_ip if pod.status else None

    # -- live job introspection (pods + events by label) ----------------------

    def list_pods_by_label(self, label_selector: str, namespace: str) -> list[dict]:
        """List pods matching ``label_selector`` and return plain dicts with the
        fields the console's Pod table needs. Used to show a live RayJob's real
        head/worker pods (Req 14.5). Empty list when none match / on NotFound."""
        from kubernetes.client.rest import ApiException

        try:
            resp = self.core.list_namespaced_pod(namespace, label_selector=label_selector)
        except ApiException as e:
            if e.status == 404:
                return []
            raise
        import time as _t

        out: list[dict] = []
        for pod in resp.items or []:
            meta = pod.metadata
            spec = pod.spec
            st = pod.status
            labels = (meta.labels or {}) if meta else {}
            statuses = (st.container_statuses if st else None) or []
            restarts = sum(int(cs.restart_count or 0) for cs in statuses)
            ready = bool(statuses) and all(cs.ready for cs in statuses)
            # role from Ray's node-type label
            ntype = labels.get("ray.io/node-type", "")
            role = "head" if ntype == "head" else "worker" if ntype == "worker" else "worker"
            # gpu request (from first container limits)
            gpu = 0
            try:
                containers = (spec.containers if spec else None) or []
                for c in containers:
                    lim = (c.resources.limits if c.resources else None) or {}
                    if "nvidia.com/gpu" in lim:
                        gpu += int(lim["nvidia.com/gpu"])
            except Exception:  # noqa: BLE001 — defensive parse
                gpu = 0
            age_sec = 0
            if st and st.start_time:
                try:
                    age_sec = int(_t.time() - st.start_time.timestamp())
                except Exception:  # noqa: BLE001
                    age_sec = 0
            # last waiting/terminated reason as the human "last event"
            last_event = ""
            for cs in statuses:
                state = cs.state
                if state and state.waiting and state.waiting.reason:
                    last_event = state.waiting.reason
                elif state and state.terminated and state.terminated.reason:
                    last_event = state.terminated.reason
            out.append({
                "name": meta.name if meta else "",
                "role": role,
                "phase": (st.phase if st else None) or "Unknown",
                "node": (spec.node_name if spec else None) or "-",
                "restarts": restarts,
                "gpu": gpu,
                "age_sec": age_sec,
                "ip": (st.pod_ip if st else None) or "-",
                "ready": ready,
                "last_event": last_event,
            })
        return out

    def list_pod_events(self, label_selector: str, namespace: str) -> list[dict]:
        """List recent K8s events for pods matching ``label_selector``. We first
        resolve pod names from the selector, then read events involving them.
        Returns plain dicts (ts/type/reason/object/message/raw)."""
        from kubernetes.client.rest import ApiException
        import time as _t

        try:
            pods = self.core.list_namespaced_pod(namespace, label_selector=label_selector)
        except ApiException as e:
            if e.status == 404:
                return []
            raise
        names = {p.metadata.name for p in (pods.items or []) if p.metadata}
        if not names:
            return []
        try:
            evs = self.core.list_namespaced_event(namespace)
        except ApiException:
            return []
        out: list[dict] = []
        for e in evs.items or []:
            involved = e.involved_object
            if not involved or involved.name not in names:
                continue
            ts = e.last_timestamp or e.event_time or (e.metadata.creation_timestamp if e.metadata else None)
            iso = ""
            if ts:
                try:
                    iso = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(ts.timestamp()))
                except Exception:  # noqa: BLE001
                    iso = ""
            out.append({
                "ts": iso,
                "type": e.type or "Normal",
                "reason": e.reason or "",
                "object": f"{involved.kind}/{involved.name}",
                "message": e.message or "",
                "raw": e.reason or "",
            })
        out.sort(key=lambda x: x["ts"])
        return out

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
