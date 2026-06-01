"""
K8sClient extensions (Task 7): pod_container_status / wait_pod_deleted /
service_node_ports / node_address. We monkeypatch the lazily-imported
CoreV1Api with a fake so no real cluster is needed.
"""
from __future__ import annotations

import types

import pytest

from raytrain_server.core.k8s_client import K8sClient


class _ApiException(Exception):
    def __init__(self, status):
        self.status = status
        self.reason = "x"


@pytest.fixture(autouse=True)
def _patch_api_exception(monkeypatch):
    # k8s_client imports ApiException lazily from kubernetes.client.rest inside
    # each method; install a fake module so those imports resolve.
    import sys

    fake_rest = types.ModuleType("kubernetes.client.rest")
    fake_rest.ApiException = _ApiException
    fake_client = types.ModuleType("kubernetes.client")
    fake_client.rest = fake_rest
    fake_kubernetes = types.ModuleType("kubernetes")
    fake_kubernetes.client = fake_client
    monkeypatch.setitem(sys.modules, "kubernetes", fake_kubernetes)
    monkeypatch.setitem(sys.modules, "kubernetes.client", fake_client)
    monkeypatch.setitem(sys.modules, "kubernetes.client.rest", fake_rest)
    yield


def _obj(**kw):
    return types.SimpleNamespace(**kw)


class _FakeCore:
    def __init__(self):
        self.pods = {}
        self.services = {}
        self.nodes = {}
        self._pod_list = []      # for list_namespaced_pod
        self._event_list = []    # for list_namespaced_event

    def read_namespaced_pod(self, name, ns):
        if name not in self.pods:
            raise _ApiException(404)
        return self.pods[name]

    def read_namespaced_service(self, name, ns):
        if name not in self.services:
            raise _ApiException(404)
        return self.services[name]

    def read_node(self, name):
        if name not in self.nodes:
            raise _ApiException(404)
        return self.nodes[name]

    def list_namespaced_pod(self, ns, label_selector=None):
        return _obj(items=list(self._pod_list))

    def list_namespaced_event(self, ns):
        return _obj(items=list(self._event_list))


def _client_with(core) -> K8sClient:
    c = K8sClient(in_cluster=False)
    c._core = core  # inject, bypass lazy config load
    return c


def test_pod_container_status_imagepull():
    core = _FakeCore()
    waiting = _obj(reason="ImagePullBackOff")
    cs = _obj(state=_obj(waiting=waiting, terminated=None), ready=False)
    core.pods["p"] = _obj(status=_obj(container_statuses=[cs], phase="Pending"))
    kind, reason = _client_with(core).pod_container_status("p", "ns")
    assert kind == "waiting" and reason == "ImagePullBackOff"


def test_pod_container_status_ready():
    core = _FakeCore()
    cs = _obj(state=_obj(waiting=None, terminated=None), ready=True)
    core.pods["p"] = _obj(status=_obj(container_statuses=[cs], phase="Running"))
    kind, reason = _client_with(core).pod_container_status("p", "ns")
    assert kind == "ready" and reason is None


def test_pod_container_status_creating_is_none():
    core = _FakeCore()
    waiting = _obj(reason="ContainerCreating")
    cs = _obj(state=_obj(waiting=waiting, terminated=None), ready=False)
    core.pods["p"] = _obj(status=_obj(container_statuses=[cs], phase="Pending"))
    kind, _ = _client_with(core).pod_container_status("p", "ns")
    assert kind == "none"  # benign, still scheduling


def test_pod_container_status_notfound():
    kind, reason = _client_with(_FakeCore()).pod_container_status("missing", "ns")
    assert kind == "notfound" and reason is None


def test_wait_pod_deleted_true_when_absent():
    # pod not present → already deleted → True immediately
    assert _client_with(_FakeCore()).wait_pod_deleted("gone", "ns", timeout_s=1) is True


def test_wait_pod_deleted_timeout_when_present():
    core = _FakeCore()
    core.pods["p"] = _obj(status=_obj(container_statuses=[], phase="Running"))
    assert _client_with(core).wait_pod_deleted("p", "ns", timeout_s=1) is False


def test_service_node_ports():
    core = _FakeCore()
    ports = [
        _obj(name="jupyter", port=8888, node_port=30888),
        _obj(name="ssh", port=22, node_port=30022),
        _obj(name="noport", port=1, node_port=None),
    ]
    core.services["svc"] = _obj(spec=_obj(ports=ports))
    got = _client_with(core).service_node_ports("svc", "ns")
    assert got == {"jupyter": 30888, "ssh": 30022}


def test_node_address_prefers_external():
    core = _FakeCore()
    core.pods["p"] = _obj(spec=_obj(node_name="node1"), status=_obj(container_statuses=[]))
    core.nodes["node1"] = _obj(status=_obj(addresses=[
        _obj(type="InternalIP", address="10.0.0.5"),
        _obj(type="ExternalIP", address="1.2.3.4"),
    ]))
    assert _client_with(core).node_address("p", "ns") == "1.2.3.4"


def test_node_address_falls_back_internal():
    core = _FakeCore()
    core.pods["p"] = _obj(spec=_obj(node_name="node1"), status=_obj(container_statuses=[]))
    core.nodes["node1"] = _obj(status=_obj(addresses=[
        _obj(type="InternalIP", address="10.0.0.5"),
    ]))
    assert _client_with(core).node_address("p", "ns") == "10.0.0.5"


# -- list_pods_by_label / list_pod_events (Task 20) -------------------------- #


def _pod(name, *, node_type, phase, node="n1", restarts=0, gpu=0, ready=True, ip="10.0.0.1"):
    state = _obj(waiting=None, terminated=None)
    cs = _obj(state=state, ready=ready, restart_count=restarts)
    limits = {"nvidia.com/gpu": gpu} if gpu else {}
    container = _obj(resources=_obj(limits=limits))
    return _obj(
        metadata=_obj(name=name, labels={"ray.io/node-type": node_type}),
        spec=_obj(node_name=node, containers=[container]),
        status=_obj(phase=phase, container_statuses=[cs], pod_ip=ip, start_time=None),
    )


def test_list_pods_by_label_maps_roles_and_gpu():
    core = _FakeCore()
    core._pod_list = [
        _pod("ray-head", node_type="head", phase="Running", gpu=0),
        _pod("ray-worker-0", node_type="worker", phase="Running", gpu=8, restarts=1),
    ]
    pods = _client_with(core).list_pods_by_label("ray.io/job-submission-id=sid-1", "ns")
    assert len(pods) == 2
    head = [p for p in pods if p["role"] == "head"][0]
    worker = [p for p in pods if p["role"] == "worker"][0]
    assert head["name"] == "ray-head" and head["phase"] == "Running"
    assert worker["gpu"] == 8 and worker["restarts"] == 1


def test_list_pod_events_filters_to_matching_pods():
    core = _FakeCore()
    core._pod_list = [_pod("ray-head", node_type="head", phase="Pending")]
    ts = _obj(timestamp=lambda: 1_700_000_000.0)
    core._event_list = [
        _obj(involved_object=_obj(kind="Pod", name="ray-head"), type="Warning",
             reason="FailedScheduling", message="0/3 nodes available",
             last_timestamp=ts, event_time=None, metadata=_obj(creation_timestamp=ts)),
        _obj(involved_object=_obj(kind="Pod", name="other-pod"), type="Normal",
             reason="Started", message="x",
             last_timestamp=ts, event_time=None, metadata=_obj(creation_timestamp=ts)),
    ]
    events = _client_with(core).list_pod_events("ray.io/job-submission-id=sid-1", "ns")
    assert len(events) == 1
    assert events[0]["reason"] == "FailedScheduling"
    assert events[0]["object"] == "Pod/ray-head"


def test_list_pods_by_label_empty_when_none():
    assert _client_with(_FakeCore()).list_pods_by_label("x=y", "ns") == []
