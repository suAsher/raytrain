"""
Thin wrapper over the k8s python client. Keeps CLI code readable.
"""
from __future__ import annotations

import time
from typing import Any, Iterator

import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException

RAYJOB_GROUP = "ray.io"
RAYJOB_VERSION = "v1"
RAYJOB_PLURAL = "rayjobs"


def load_kube() -> None:
    """Load kubeconfig from default locations; assume Kasm already has it."""
    try:
        config.load_kube_config()
    except Exception:
        # fall back to in-cluster for when running inside a pod
        config.load_incluster_config()


def apply_yaml_docs(docs: list[dict[str, Any]], namespace: str) -> None:
    """Apply a list of k8s objects (ConfigMap, Secret, Service, RayJob)."""
    core = client.CoreV1Api()
    custom = client.CustomObjectsApi()
    for doc in docs:
        kind = doc.get("kind")
        if kind == "ConfigMap":
            _apply_configmap(core, doc, namespace)
        elif kind == "Secret":
            _apply_secret(core, doc, namespace)
        elif kind == "Service":
            _apply_service(core, doc, namespace)
        elif kind == "RayJob":
            _apply_rayjob(custom, doc, namespace)
        else:
            raise ValueError(f"unsupported kind in apply: {kind}")


def _apply_configmap(core: client.CoreV1Api, body: dict, ns: str) -> None:
    name = body["metadata"]["name"]
    try:
        core.replace_namespaced_config_map(name, ns, body)
    except ApiException as e:
        if e.status == 404:
            core.create_namespaced_config_map(ns, body)
        else:
            raise


def _apply_secret(core: client.CoreV1Api, body: dict, ns: str) -> None:
    name = body["metadata"]["name"]
    try:
        core.replace_namespaced_secret(name, ns, body)
    except ApiException as e:
        if e.status == 404:
            core.create_namespaced_secret(ns, body)
        else:
            raise


def _apply_service(core: client.CoreV1Api, body: dict, ns: str) -> None:
    name = body["metadata"]["name"]
    try:
        core.replace_namespaced_service(name, ns, body)
    except ApiException as e:
        if e.status == 404:
            core.create_namespaced_service(ns, body)
        else:
            raise


def _apply_rayjob(api: client.CustomObjectsApi, body: dict, ns: str) -> None:
    name = body["metadata"]["name"]
    try:
        api.create_namespaced_custom_object(
            RAYJOB_GROUP, RAYJOB_VERSION, ns, RAYJOB_PLURAL, body
        )
    except ApiException as e:
        if e.status == 409:
            raise RuntimeError(
                f"RayJob {name!r} already exists in {ns}. "
                f"Choose a different --name or delete the existing one."
            ) from e
        raise


def delete_rayjob(name: str, namespace: str) -> None:
    custom = client.CustomObjectsApi()
    core = client.CoreV1Api()

    # 1. Delete RayJob
    try:
        custom.delete_namespaced_custom_object(
            RAYJOB_GROUP, RAYJOB_VERSION, namespace, RAYJOB_PLURAL, name,
            propagation_policy="Background",
        )
    except Exception:
        pass

    # 2. Delete Dashboard Service
    try:
        core.delete_namespaced_service(f"{name}-dashboard", namespace)
    except Exception:
        pass

    # 3. Delete ConfigMap
    try:
        core.delete_namespaced_config_map(f"raytrain-payload-{name}", namespace)
    except Exception:
        pass

    # 4. Delete Secret
    try:
        core.delete_namespaced_secret(f"raytrain-creds-{name}", namespace)
    except Exception:
        pass


def get_rayjob(name: str, namespace: str) -> dict[str, Any]:
    custom = client.CustomObjectsApi()
    return custom.get_namespaced_custom_object(
        RAYJOB_GROUP, RAYJOB_VERSION, namespace, RAYJOB_PLURAL, name,
    )


def list_rayjobs(namespace: str, owner: str | None = None) -> list[dict]:
    custom = client.CustomObjectsApi()
    selector = f"raytrain.owner={owner}" if owner else None
    resp = custom.list_namespaced_custom_object(
        RAYJOB_GROUP, RAYJOB_VERSION, namespace, RAYJOB_PLURAL,
        label_selector=selector,
    )
    return resp.get("items", [])


def parse_docs(yaml_text: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(yaml_text) if d]


def stream_pod_logs(
    namespace: str,
    pod_name: str,
    container: str | None = None,
    follow: bool = True,
) -> Iterator[str]:
    core = client.CoreV1Api()
    w = core.read_namespaced_pod_log(
        pod_name,
        namespace,
        container=container,
        follow=follow,
        _preload_content=False,
        tail_lines=200 if follow else None,
    )
    try:
        for chunk in w.stream():
            yield chunk.decode("utf-8", errors="replace")
    finally:
        w.release_conn()


def list_pods_for_rayjob(name: str, namespace: str) -> list[client.V1Pod]:
    core = client.CoreV1Api()
    pods = core.list_namespaced_pod(namespace).items
    out = []
    for p in pods:
        pod_name = p.metadata.name or ""
        labels = p.metadata.labels or {}
        # Match 1: RayCluster pods (head + worker) via ray.io/cluster label
        cluster = labels.get("ray.io/cluster", "")
        if cluster and cluster.startswith(name):
            out.append(p)
            continue
        # Match 2: Submitter pod (pod name starts with job name, no head/worker in name)
        if pod_name.startswith(name) and "head" not in pod_name and "worker" not in pod_name:
            out.append(p)
    return out


def wait_for_head_pod(name: str, namespace: str, timeout_s: int = 300) -> client.V1Pod:
    """Block until the head pod of the RayJob is Running (or Pending->Running)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        pods = list_pods_for_rayjob(name, namespace)
        for p in pods:
            role = (p.metadata.labels or {}).get("raytrain.role")
            if role == "head" and p.status and p.status.phase in ("Running", "Succeeded"):
                return p
        time.sleep(3)
    raise TimeoutError(f"head pod of RayJob {name} did not become ready in {timeout_s}s")
