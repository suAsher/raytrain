"""
Admin catalog resources (projects / quota_groups / runtime_images) + queues
CRUD, end-to-end. Reads open to any user; writes admin-only.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from raytrain_server.core import queues_store as qs_mod
from raytrain_server.core import resources_store as rs_mod
from raytrain_server.core.jwt_auth import issue_token
from raytrain_server.core.settings import Settings
from raytrain_server.main import create_app


@pytest.fixture(autouse=True)
def _reset():
    rs_mod.set_resource_store(rs_mod.ResourceStore())
    qs_mod.set_queue_store(qs_mod.QueueStore())
    yield
    rs_mod.set_resource_store(rs_mod.ResourceStore())
    qs_mod.set_queue_store(qs_mod.QueueStore())


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings=settings))


def _h(settings, role="admin", user="root"):
    tok, _ = issue_token(user, tenant="default", role=role, settings=settings)
    return {"Authorization": f"Bearer {tok}"}


def test_list_seeded_projects(client, settings):
    r = client.get("/v1/admin/resources/project", headers=_h(settings, role="user"))
    assert r.status_code == 200
    names = [x["name"] for x in r.json()]
    assert "pointcept" in names


def test_unknown_kind_400(client, settings):
    r = client.get("/v1/admin/resources/bogus", headers=_h(settings))
    assert r.status_code == 400


def test_create_update_delete_project(client, settings):
    h = _h(settings)
    c = client.post("/v1/admin/resources/project", headers=h,
                    json={"name": "new-proj", "spec": {"owner": "alice"}})
    assert c.status_code == 201, c.text
    rid = c.json()["id"]

    u = client.patch(f"/v1/admin/resources/project/{rid}", headers=h,
                     json={"name": "new-proj", "spec": {"owner": "bob"}, "enabled": False})
    assert u.status_code == 200
    assert u.json()["spec"]["owner"] == "bob"
    assert u.json()["enabled"] is False

    d = client.delete(f"/v1/admin/resources/project/{rid}", headers=h)
    assert d.status_code == 204


def test_non_admin_cannot_write(client, settings):
    uh = _h(settings, role="user", user="alice")
    r = client.post("/v1/admin/resources/project", headers=uh, json={"name": "x"})
    assert r.status_code == 403


def test_runtime_image_crud(client, settings):
    h = _h(settings)
    c = client.post("/v1/admin/resources/runtime_image", headers=h,
                    json={"name": "raytrain/foo:v1", "spec": {"uri": "raytrain/foo:v1", "cuda": "12.4"}})
    assert c.status_code == 201
    lst = client.get("/v1/admin/resources/runtime_image", headers=h).json()
    assert any(x["name"] == "raytrain/foo:v1" for x in lst)


def test_queue_create_conflict_delete(client, settings):
    h = _h(settings)
    c = client.post("/v1/admin/queues", headers=h,
                    json={"name": "h20-new", "gpu_type": "H20", "nominal": 16})
    assert c.status_code == 201, c.text
    # duplicate
    assert client.post("/v1/admin/queues", headers=h,
                       json={"name": "h20-new"}).status_code == 409
    # delete
    assert client.delete("/v1/admin/queues/h20-new", headers=h).status_code == 204
    assert client.delete("/v1/admin/queues/h20-new", headers=h).status_code == 404
