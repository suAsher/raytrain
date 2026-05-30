"""Tests for Workspace orchestration (core.workspace) + /v1/workspaces API."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from raytrain_server.api.workspaces import _k8s, _store
from raytrain_server.core import workspace as ws
from raytrain_server.core.jwt_auth import issue_token
from raytrain_server.core.settings import Settings
from raytrain_server.core.store import WorkspaceStore
from raytrain_server.main import create_app


# --------------------------------------------------------------------------- #
# core.workspace (pure builders)
# --------------------------------------------------------------------------- #


def _spec(**over) -> ws.WorkspaceSpec:
    base = dict(
        workspace_id="abc123",
        user="zhangsan",
        tenant="occ",
        name="my-proj",
        image="img:latest",
    )
    base.update(over)
    return ws.WorkspaceSpec(**base)


class TestNaming:
    def test_pod_pvc_service_names(self) -> None:
        s = _spec()
        assert s.pod_name == "ws-abc123"
        assert s.service_name == "ws-abc123"
        assert s.pvc_name == "ws-zhangsan-my-proj"

    def test_sanitize_strips_unsafe(self) -> None:
        assert ws.sanitize("Foo_Bar 123") == "foo-bar-123"
        assert ws.sanitize("a@@@b") == "a-b"


class TestPodManifest:
    def test_basic_structure(self) -> None:
        s = _spec()
        m = ws.build_pod_manifest(s)
        assert m["kind"] == "Pod"
        assert m["metadata"]["name"] == "ws-abc123"
        assert m["metadata"]["labels"]["raytrain.io/workspace-id"] == "abc123"
        c = m["spec"]["containers"][0]
        assert c["image"] == "img:latest"
        port_names = {p["name"] for p in c["ports"]}
        assert port_names == {"jupyter", "code-server", "pycharm", "ssh"}

    def test_injects_minio_creds(self) -> None:
        s = _spec(
            minio_endpoint="http://minio:9000",
            minio_access_key="ak",
            minio_secret_key="sk",
        )
        m = ws.build_pod_manifest(s)
        env = {e["name"]: e.get("value") for e in m["spec"]["containers"][0]["env"]}
        assert env["AWS_ENDPOINT_URL"] == "http://minio:9000"
        assert env["AWS_ACCESS_KEY_ID"] == "ak"
        assert env["AWS_SECRET_ACCESS_KEY"] == "sk"

    def test_injects_token(self) -> None:
        s = _spec(raytrain_token="jwt-xyz")
        m = ws.build_pod_manifest(s)
        env = {e["name"]: e.get("value") for e in m["spec"]["containers"][0]["env"]}
        assert env["RAYTRAIN_TOKEN"] == "jwt-xyz"

    def test_mounts_home_pvc(self) -> None:
        s = _spec()
        m = ws.build_pod_manifest(s)
        vols = {v["name"]: v for v in m["spec"]["volumes"]}
        assert vols["home"]["persistentVolumeClaim"]["claimName"] == "ws-zhangsan-my-proj"


class TestIdeUrls:
    def test_with_domain(self) -> None:
        s = _spec()
        urls = ws.build_ide_urls(s, "raytrain.example.com")
        assert urls["jupyter"] == "https://ws-abc123.raytrain.example.com/jupyter/"
        assert urls["code"] == "https://ws-abc123.raytrain.example.com/code-server/"
        assert urls["ssh"] == "ssh://ws-abc123.raytrain.example.com:22"

    def test_no_domain_returns_empty(self) -> None:
        assert ws.build_ide_urls(_spec(), "") == {}


# --------------------------------------------------------------------------- #
# /v1/workspaces API (fake K8s)
# --------------------------------------------------------------------------- #


class _FakeK8s:
    def __init__(self) -> None:
        self.pods: dict[str, str] = {}     # name -> phase
        self.pvcs: set[str] = set()
        self.services: set[str] = set()

    def ensure_pvc(self, name, namespace, size_gi, storage_class, access_mode="ReadWriteMany", labels=None):
        self.pvcs.add(name)
        return name

    def delete_pvc(self, name, namespace):
        self.pvcs.discard(name)

    def create_pod(self, manifest, namespace):
        name = manifest["metadata"]["name"]
        self.pods[name] = "Running"
        return name

    def delete_pod(self, name, namespace):
        self.pods.pop(name, None)

    def pod_phase(self, name, namespace):
        return self.pods.get(name, "NotFound")

    def pod_ip(self, name, namespace):
        return "10.0.0.5"

    def ensure_service(self, manifest, namespace):
        self.services.add(manifest["metadata"]["name"])
        return manifest["metadata"]["name"]

    def delete_service(self, name, namespace):
        self.services.discard(name)


@pytest.fixture
def app(settings: Settings):
    # fresh store per test
    from raytrain_server.core import store as store_mod
    store_mod._store = WorkspaceStore()

    app = create_app(settings=settings)
    fake = _FakeK8s()
    app.dependency_overrides[_k8s] = lambda: fake
    app.state.fake_k8s = fake  # type: ignore[attr-defined]
    yield app
    store_mod._store = None


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def user_token(settings: Settings) -> str:
    t, _ = issue_token("zhangsan", tenant="occ", role="user", settings=settings)
    return t


def _h(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


class TestWorkspaceApi:
    def test_create_then_get(self, client, user_token, app) -> None:
        r = client.post(
            "/v1/workspaces",
            json={"name": "my-proj"},
            headers=_h(user_token),
        )
        assert r.status_code == 201, r.text
        wid = r.json()["id"]
        assert r.json()["state"] == "running"
        # pod + pvc + service created
        assert app.state.fake_k8s.pods
        assert app.state.fake_k8s.pvcs
        assert app.state.fake_k8s.services

        g = client.get(f"/v1/workspaces/{wid}", headers=_h(user_token))
        assert g.status_code == 200
        assert g.json()["pod_phase"] == "Running"

    def test_requires_token(self, client) -> None:
        r = client.post("/v1/workspaces", json={"name": "x"})
        assert r.status_code == 401

    def test_list_only_own(self, client, user_token, settings) -> None:
        client.post("/v1/workspaces", json={"name": "a"}, headers=_h(user_token))
        other, _ = issue_token("lisi", settings=settings)
        client.post("/v1/workspaces", json={"name": "b"}, headers=_h(other))

        r = client.get("/v1/workspaces", headers=_h(user_token))
        assert r.status_code == 200
        names = [w["name"] for w in r.json()]
        assert names == ["a"]

    def test_quota_enforced(self, client, user_token) -> None:
        for i in range(3):
            r = client.post(
                "/v1/workspaces", json={"name": f"w{i}"}, headers=_h(user_token)
            )
            assert r.status_code == 201
        # 4th exceeds DEFAULT_MAX_WORKSPACES=3
        r = client.post(
            "/v1/workspaces", json={"name": "w4"}, headers=_h(user_token)
        )
        assert r.status_code == 402

    def test_stop_keeps_pvc(self, client, user_token, app) -> None:
        wid = client.post(
            "/v1/workspaces", json={"name": "p"}, headers=_h(user_token)
        ).json()["id"]
        r = client.post(f"/v1/workspaces/{wid}/stop", headers=_h(user_token))
        assert r.status_code == 200
        assert r.json()["state"] == "stopped"
        # pod gone, pvc stays
        assert not app.state.fake_k8s.pods
        assert app.state.fake_k8s.pvcs

    def test_start_recreates_pod(self, client, user_token, app) -> None:
        wid = client.post(
            "/v1/workspaces", json={"name": "p"}, headers=_h(user_token)
        ).json()["id"]
        client.post(f"/v1/workspaces/{wid}/stop", headers=_h(user_token))
        r = client.post(f"/v1/workspaces/{wid}/start", headers=_h(user_token))
        assert r.status_code == 200
        assert r.json()["state"] == "running"
        assert app.state.fake_k8s.pods

    def test_delete_with_pvc(self, client, user_token, app) -> None:
        wid = client.post(
            "/v1/workspaces", json={"name": "p"}, headers=_h(user_token)
        ).json()["id"]
        r = client.delete(
            f"/v1/workspaces/{wid}",
            params={"delete_pvc": "true"},
            headers=_h(user_token),
        )
        assert r.status_code == 204
        assert not app.state.fake_k8s.pods
        assert not app.state.fake_k8s.pvcs
        assert not app.state.fake_k8s.services

    def test_other_user_cannot_get(self, client, user_token, settings) -> None:
        wid = client.post(
            "/v1/workspaces", json={"name": "p"}, headers=_h(user_token)
        ).json()["id"]
        other, _ = issue_token("lisi", settings=settings)
        r = client.get(f"/v1/workspaces/{wid}", headers=_h(other))
        assert r.status_code == 403

    def test_admin_can_get_any(self, client, user_token, settings) -> None:
        wid = client.post(
            "/v1/workspaces", json={"name": "p"}, headers=_h(user_token)
        ).json()["id"]
        admin, _ = issue_token("root", role="admin", settings=settings)
        r = client.get(f"/v1/workspaces/{wid}", headers=_h(admin))
        assert r.status_code == 200
