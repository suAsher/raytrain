"""
Workspace真实生命周期 (Tasks 8–12, Req 1/2/3/4, Property 1).

- derive_state 全分支：NotFound/Pending/Running-ready/ImagePullBackOff/Failed/stopping
- create 不再假 running（state=creating）
- 镜像校验
- 停后启：等 Terminating 删净；超时→409 FriendlyError
- running 时出 NodePort IDE URL；非 running 不出
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from raytrain_server.api.workspaces import _k8s
from raytrain_server.core import store as store_mod
from raytrain_server.core.jwt_auth import issue_token
from raytrain_server.core.settings import Settings
from raytrain_server.core.store import WorkspaceRecord
from raytrain_server.core.workspace_service import derive_state, validate_image
from raytrain_server.core.errors import FriendlyError
from raytrain_server.main import create_app


# ---------- Fake K8s ----------

class FakeK8s:
    def __init__(self):
        self._phase = {}          # pod -> phase
        self._container = {}       # pod -> (kind, reason)
        self._node_ports = {}      # svc -> {name: port}
        self._node_addr = "10.0.0.9"
        self.created_pods = []
        self.deleted_pods = []

    # lifecycle used by API
    def ensure_pvc(self, **kw): return kw.get("name")
    def create_pod(self, manifest, ns):
        name = manifest["metadata"]["name"]
        self.created_pods.append(name)
        self._phase[name] = "Pending"
        self._container[name] = ("none", None)
        return name
    def ensure_service(self, manifest, ns): return manifest["metadata"]["name"]
    def delete_pod(self, name, ns): self.deleted_pods.append(name); self._phase[name] = "NotFound"
    def delete_service(self, name, ns): pass
    def delete_pvc(self, name, ns): pass

    # reads used by derive_state / urls
    def pod_phase(self, name, ns): return self._phase.get(name, "NotFound")
    def pod_container_status(self, name, ns): return self._container.get(name, ("none", None))
    def service_node_ports(self, name, ns): return self._node_ports.get(name, {})
    def node_address(self, pod, ns): return self._node_addr
    def wait_pod_deleted(self, name, ns, timeout_s=60): return self.pod_phase(name, ns) == "NotFound"

    # test helpers
    def set_running(self, pod, svc):
        self._phase[pod] = "Running"
        self._container[pod] = ("ready", None)
        self._node_ports[svc] = {"jupyter": 30888, "code-server": 30808, "ssh": 30022}


@pytest.fixture(autouse=True)
def _reset():
    store_mod.set_workspace_store(store_mod.WorkspaceStore())
    yield
    store_mod.set_workspace_store(store_mod.WorkspaceStore())


@pytest.fixture
def fake_k8s():
    return FakeK8s()


@pytest.fixture
def client(settings: Settings, fake_k8s):
    app = create_app(settings=settings)
    app.dependency_overrides[_k8s] = lambda: fake_k8s
    return TestClient(app, raise_server_exceptions=False)


def _h(settings, user="alice", role="user"):
    tok, _ = issue_token(user, tenant="t1", role=role, settings=settings)
    return {"Authorization": f"Bearer {tok}"}


# ---------- derive_state unit (Property 1) ----------

def _rec(state="creating", pod="ws-1", svc="ws-1"):
    return WorkspaceRecord(id="1", user="a", tenant="t", name="n", image="img:1",
                           cpu=4, memory_gi=8, pvc_gi=100, state=state,
                           pod_name=pod, service_name=svc)


def test_derive_notfound_is_stopped(fake_k8s):
    d = derive_state(_rec(), fake_k8s, "ns")
    assert d.state == "stopped" and d.pod_phase == "NotFound"


def test_derive_pending_is_starting(fake_k8s):
    fake_k8s._phase["ws-1"] = "Pending"; fake_k8s._container["ws-1"] = ("none", None)
    assert derive_state(_rec(), fake_k8s, "ns").state == "starting"


def test_derive_running_ready_is_running(fake_k8s):
    fake_k8s._phase["ws-1"] = "Running"; fake_k8s._container["ws-1"] = ("ready", None)
    assert derive_state(_rec(), fake_k8s, "ns").state == "running"


def test_derive_imagepull_is_error_with_reason(fake_k8s):
    fake_k8s._phase["ws-1"] = "Pending"
    fake_k8s._container["ws-1"] = ("waiting", "ImagePullBackOff")
    d = derive_state(_rec(), fake_k8s, "ns")
    assert d.state == "error" and d.reason == "ImagePullBackOff"


def test_derive_failed_is_error(fake_k8s):
    fake_k8s._phase["ws-1"] = "Failed"; fake_k8s._container["ws-1"] = ("terminated", "OOMKilled")
    assert derive_state(_rec(), fake_k8s, "ns").state == "error"


def test_derive_stopping_while_pod_present(fake_k8s):
    fake_k8s._phase["ws-1"] = "Running"; fake_k8s._container["ws-1"] = ("ready", None)
    assert derive_state(_rec(state="stopping"), fake_k8s, "ns").state == "stopping"


# ---------- image validation ----------

def test_validate_image_ok():
    validate_image("registry:5000/repo/img:tag")  # no raise


def test_validate_image_rejects_empty():
    with pytest.raises(FriendlyError):
        validate_image("")


# ---------- API: create no longer fakes running ----------

def test_create_is_creating_not_running(client, settings):
    r = client.post("/v1/workspaces", headers=_h(settings), json={"name": "ws"})
    assert r.status_code == 201, r.text
    assert r.json()["state"] in ("creating", "starting")  # never "running" on create


def test_create_rejects_bad_image(client, settings):
    r = client.post("/v1/workspaces", headers=_h(settings),
                    json={"name": "ws", "image": "bad image with spaces"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_IMAGE"


# ---------- API: running shows NodePort URLs ----------

def test_running_workspace_exposes_ide_urls(client, settings, fake_k8s):
    wid = client.post("/v1/workspaces", headers=_h(settings), json={"name": "ws"}).json()["id"]
    rec = store_mod.get_workspace_store().get(wid)
    fake_k8s.set_running(rec.pod_name, rec.service_name)
    r = client.get(f"/v1/workspaces/{wid}", headers=_h(settings))
    body = r.json()
    assert body["state"] == "running"
    assert body["ide_urls"]["jupyter"].startswith("http://10.0.0.9:30888")
    assert body["ide_urls"]["ssh"].startswith("ssh://10.0.0.9:30022")


def test_non_running_has_no_ide_urls(client, settings):
    wid = client.post("/v1/workspaces", headers=_h(settings), json={"name": "ws"}).json()["id"]
    r = client.get(f"/v1/workspaces/{wid}", headers=_h(settings))
    assert r.json()["ide_urls"] == {}


# ---------- API: stop then start ----------

def test_stop_then_start(client, settings, fake_k8s):
    wid = client.post("/v1/workspaces", headers=_h(settings), json={"name": "ws"}).json()["id"]
    s = client.post(f"/v1/workspaces/{wid}/stop", headers=_h(settings))
    assert s.status_code == 200 and s.json()["state"] in ("stopping", "stopped")
    # pod is now NotFound (fake delete) → start should succeed
    st = client.post(f"/v1/workspaces/{wid}/start", headers=_h(settings))
    assert st.status_code == 200, st.text
    assert st.json()["state"] in ("creating", "starting")


def test_start_blocks_when_old_pod_terminating(client, settings, fake_k8s, monkeypatch):
    wid = client.post("/v1/workspaces", headers=_h(settings), json={"name": "ws"}).json()["id"]
    rec = store_mod.get_workspace_store().get(wid)
    # simulate pod stuck Terminating: still present + wait returns False
    fake_k8s._phase[rec.pod_name] = "Running"
    monkeypatch.setattr(fake_k8s, "wait_pod_deleted", lambda *a, **k: False)
    r = client.post(f"/v1/workspaces/{wid}/start", headers=_h(settings))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "WORKSPACE_TERMINATING"
