"""Tests for DevSession + Dataset registry APIs (M3)."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from raytrain_server.api.devsessions import _k8s as dev_k8s
from raytrain_server.api.workspaces import _k8s as ws_k8s
from raytrain_server.core import store as store_mod
from raytrain_server.core.devsession import DevSessionSpec, build_pod_manifest
from raytrain_server.core.jwt_auth import issue_token
from raytrain_server.core.settings import Settings
from raytrain_server.core.store import (
    DevSessionStore,
    WorkspaceStore,
    DatasetStore,
)
from raytrain_server.main import create_app


# --------------------------------------------------------------------------- #
# DevSession pod builder (pure)
# --------------------------------------------------------------------------- #


def test_devsession_pod_requests_gpu() -> None:
    spec = DevSessionSpec(
        session_id="s1", workspace_id="w1", user="z", tenant="occ",
        image="gpu:img", gpu_type="h20", gpu_count=2, pvc_name="ws-z-proj",
    )
    m = build_pod_manifest(spec)
    res = m["spec"]["containers"][0]["resources"]
    assert res["requests"]["nvidia.com/gpu"] == "2"
    assert res["limits"]["nvidia.com/gpu"] == "2"
    assert m["spec"]["nodeSelector"]["gpu"] == "h20"
    # shares parent PVC
    vols = {v["name"]: v for v in m["spec"]["volumes"]}
    assert vols["home"]["persistentVolumeClaim"]["claimName"] == "ws-z-proj"


def test_devsession_record_expiry() -> None:
    from raytrain_server.core.store import DevSessionRecord
    now = time.time()
    rec = DevSessionRecord(
        id="s", workspace_id="w", user="u", tenant="t", image="i",
        gpu_type="h20", gpu_count=1, pvc_name="p",
        idle_timeout_s=100, max_lifetime_s=1000,
        last_seen_at=now - 200, created_at=now - 200,
    )
    assert rec.is_expired(now) is True

    rec2 = DevSessionRecord(
        id="s2", workspace_id="w", user="u", tenant="t", image="i",
        gpu_type="h20", gpu_count=1, pvc_name="p",
        idle_timeout_s=10000, max_lifetime_s=100000,
        last_seen_at=now, created_at=now,
    )
    assert rec2.is_expired(now) is False


# --------------------------------------------------------------------------- #
# API harness
# --------------------------------------------------------------------------- #


class _FakeK8s:
    def __init__(self):
        self.pods = {}
        self.pvcs = set()
        self.services = set()

    def ensure_pvc(self, name, namespace, size_gi, storage_class, access_mode="ReadWriteMany", labels=None):
        self.pvcs.add(name); return name

    def delete_pvc(self, name, namespace):
        self.pvcs.discard(name)

    def create_pod(self, manifest, namespace):
        n = manifest["metadata"]["name"]; self.pods[n] = "Running"; return n

    def delete_pod(self, name, namespace):
        self.pods.pop(name, None)

    def pod_phase(self, name, namespace):
        return self.pods.get(name, "NotFound")

    def pod_ip(self, name, namespace):
        return "10.0.0.9"

    def ensure_service(self, manifest, namespace):
        self.services.add(manifest["metadata"]["name"]); return manifest["metadata"]["name"]

    def delete_service(self, name, namespace):
        self.services.discard(name)


@pytest.fixture
def app(settings: Settings):
    store_mod._store = WorkspaceStore()
    store_mod._dev_store = DevSessionStore()
    store_mod._dataset_store = DatasetStore()
    app = create_app(settings=settings)
    fake = _FakeK8s()
    app.dependency_overrides[ws_k8s] = lambda: fake
    app.dependency_overrides[dev_k8s] = lambda: fake
    app.state.fake_k8s = fake
    yield app
    store_mod._store = None
    store_mod._dev_store = None
    store_mod._dataset_store = None


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def user_token(settings):
    t, _ = issue_token("zhangsan", tenant="occ", settings=settings)
    return t


def _h(t):
    return {"Authorization": f"Bearer {t}"}


def _make_workspace(client, tok) -> str:
    return client.post(
        "/v1/workspaces", json={"name": "proj"}, headers=_h(tok)
    ).json()["id"]


# --------------------------------------------------------------------------- #
# DevSession API
# --------------------------------------------------------------------------- #


class TestDevSessionApi:
    def test_create_requires_parent_workspace(self, client, user_token) -> None:
        r = client.post(
            "/v1/dev-sessions",
            json={"workspace_id": "nope", "gpu_type": "h20", "gpu_count": 1},
            headers=_h(user_token),
        )
        assert r.status_code == 404

    def test_create_happy_path(self, client, user_token, app) -> None:
        wid = _make_workspace(client, user_token)
        r = client.post(
            "/v1/dev-sessions",
            json={"workspace_id": wid, "gpu_type": "h20", "gpu_count": 2},
            headers=_h(user_token),
        )
        assert r.status_code == 201, r.text
        assert r.json()["gpu_count"] == 2
        assert r.json()["state"] == "running"
        assert app.state.fake_k8s.pods

    def test_gpu_quota_enforced(self, client, user_token) -> None:
        wid = _make_workspace(client, user_token)
        # 8 is the cap; request 8 ok, then +1 fails
        r1 = client.post(
            "/v1/dev-sessions",
            json={"workspace_id": wid, "gpu_type": "h20", "gpu_count": 8},
            headers=_h(user_token),
        )
        assert r1.status_code == 201
        r2 = client.post(
            "/v1/dev-sessions",
            json={"workspace_id": wid, "gpu_type": "h20", "gpu_count": 1},
            headers=_h(user_token),
        )
        assert r2.status_code == 402

    def test_heartbeat_updates_last_seen(self, client, user_token) -> None:
        wid = _make_workspace(client, user_token)
        sid = client.post(
            "/v1/dev-sessions",
            json={"workspace_id": wid, "gpu_type": "h20", "gpu_count": 1},
            headers=_h(user_token),
        ).json()["id"]
        r = client.post(f"/v1/dev-sessions/{sid}/heartbeat", headers=_h(user_token))
        assert r.status_code == 200

    def test_delete_releases(self, client, user_token, app) -> None:
        wid = _make_workspace(client, user_token)
        sid = client.post(
            "/v1/dev-sessions",
            json={"workspace_id": wid, "gpu_type": "h20", "gpu_count": 1},
            headers=_h(user_token),
        ).json()["id"]
        r = client.delete(f"/v1/dev-sessions/{sid}", headers=_h(user_token))
        assert r.status_code == 204
        assert not any("dev-" in p for p in app.state.fake_k8s.pods)


# --------------------------------------------------------------------------- #
# Dataset registry API
# --------------------------------------------------------------------------- #


class TestDatasetApi:
    def test_register_lance_without_scan(self, client, user_token) -> None:
        r = client.post(
            "/v1/datasets",
            json={
                "name": "nuscenes-train",
                "type": "lance",
                "uri": "s3://occ-lance/nuscenes_v1",
                "visibility": "tenant",
                "tags": ["lidar", "nuscenes"],
                "scan_metadata": False,
            },
            headers=_h(user_token),
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "nuscenes-train"
        assert body["visibility"] == "tenant"
        assert body["owner"] == "zhangsan"
        assert body["tags"] == ["lidar", "nuscenes"]

    def test_visibility_filtering(self, client, user_token, settings) -> None:
        # zhangsan registers a private dataset
        client.post(
            "/v1/datasets",
            json={"name": "mine", "type": "lance", "uri": "s3://b/x",
                  "visibility": "private", "scan_metadata": False},
            headers=_h(user_token),
        )
        # lisi (different tenant) registers a public one
        lisi, _ = issue_token("lisi", tenant="nlp", settings=settings)
        client.post(
            "/v1/datasets",
            json={"name": "shared", "type": "lance", "uri": "s3://b/y",
                  "visibility": "public", "scan_metadata": False},
            headers=_h(lisi),
        )
        # zhangsan sees his private + lisi's public, not lisi's private
        r = client.get("/v1/datasets", headers=_h(user_token))
        names = {d["name"] for d in r.json()}
        assert "mine" in names
        assert "shared" in names

    def test_private_not_visible_cross_user(self, client, user_token, settings) -> None:
        did = client.post(
            "/v1/datasets",
            json={"name": "secret", "type": "lance", "uri": "s3://b/z",
                  "visibility": "private", "scan_metadata": False},
            headers=_h(user_token),
        ).json()["id"]
        lisi, _ = issue_token("lisi", tenant="nlp", settings=settings)
        r = client.get(f"/v1/datasets/{did}", headers=_h(lisi))
        assert r.status_code == 403

    def test_patch_visibility(self, client, user_token) -> None:
        did = client.post(
            "/v1/datasets",
            json={"name": "ds", "type": "lance", "uri": "s3://b/z",
                  "visibility": "private", "scan_metadata": False},
            headers=_h(user_token),
        ).json()["id"]
        r = client.patch(
            f"/v1/datasets/{did}",
            json={"visibility": "public", "tags": ["new"]},
            headers=_h(user_token),
        )
        assert r.status_code == 200
        assert r.json()["visibility"] == "public"
        assert r.json()["tags"] == ["new"]

    def test_only_owner_can_delete(self, client, user_token, settings) -> None:
        did = client.post(
            "/v1/datasets",
            json={"name": "ds", "type": "lance", "uri": "s3://b/z",
                  "visibility": "public", "scan_metadata": False},
            headers=_h(user_token),
        ).json()["id"]
        lisi, _ = issue_token("lisi", tenant="nlp", settings=settings)
        r = client.delete(f"/v1/datasets/{did}", headers=_h(lisi))
        assert r.status_code == 403

    def test_invalid_type_rejected(self, client, user_token) -> None:
        r = client.post(
            "/v1/datasets",
            json={"name": "x", "type": "csv", "uri": "s3://b/z",
                  "scan_metadata": False},
            headers=_h(user_token),
        )
        assert r.status_code == 400
