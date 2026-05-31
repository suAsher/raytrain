"""
Username/password login → JWT, end-to-end.

Covers:
  - admin creates a user WITH a password → that user can /v1/auth/login
  - returned token works on a protected endpoint (/v1/auth/me)
  - wrong password / unknown user / no-password user / disabled → 401
  - admin can reset password via PATCH
  - password hash never leaks through admin user views
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from raytrain_server.core import users as users_mod
from raytrain_server.core.jwt_auth import issue_token
from raytrain_server.core.settings import Settings
from raytrain_server.core.users import hash_password, verify_password
from raytrain_server.main import create_app


@pytest.fixture(autouse=True)
def _reset_users():
    users_mod.set_user_store(users_mod.UserStore())
    yield
    users_mod.set_user_store(users_mod.UserStore())


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings=settings))


def _admin_h(settings):
    tok, _ = issue_token("root", tenant="default", role="admin", settings=settings)
    return {"Authorization": f"Bearer {tok}"}


def test_hash_roundtrip():
    h = hash_password("s3cret-pw")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("s3cret-pw", h)
    assert not verify_password("wrong", h)


def test_create_with_password_then_login(client, settings):
    r = client.post("/v1/admin/users", headers=_admin_h(settings),
                    json={"user": "alice", "tenant": "team-a", "password": "hunter2pw"})
    assert r.status_code == 201, r.text
    # password hash must not be exposed
    assert "password" not in r.json()["user"]
    assert "password_hash" not in r.json()["user"]

    lr = client.post("/v1/auth/login", json={"username": "alice", "password": "hunter2pw"})
    assert lr.status_code == 200, lr.text
    body = lr.json()
    assert body["user"] == "alice" and body["role"] == "user"
    token = body["token"]

    me = client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["user"] == "alice"


def test_wrong_password_401(client, settings):
    client.post("/v1/admin/users", headers=_admin_h(settings),
                json={"user": "alice", "password": "hunter2pw"})
    r = client.post("/v1/auth/login", json={"username": "alice", "password": "nope"})
    assert r.status_code == 401


def test_unknown_user_401(client):
    r = client.post("/v1/auth/login", json={"username": "ghost", "password": "whatever"})
    assert r.status_code == 401


def test_user_without_password_cannot_login(client, settings):
    # token-only user (no password set)
    client.post("/v1/admin/users", headers=_admin_h(settings), json={"user": "tokonly"})
    r = client.post("/v1/auth/login", json={"username": "tokonly", "password": "x123456"})
    assert r.status_code == 401


def test_disabled_user_cannot_login(client, settings):
    h = _admin_h(settings)
    client.post("/v1/admin/users", headers=h, json={"user": "alice", "password": "hunter2pw"})
    client.patch("/v1/admin/users/alice", headers=h, json={"enabled": False})
    r = client.post("/v1/auth/login", json={"username": "alice", "password": "hunter2pw"})
    assert r.status_code == 401


def test_admin_reset_password(client, settings):
    h = _admin_h(settings)
    client.post("/v1/admin/users", headers=h, json={"user": "alice", "password": "oldpass1"})
    # reset
    up = client.patch("/v1/admin/users/alice", headers=h, json={"password": "newpass1"})
    assert up.status_code == 200
    assert client.post("/v1/auth/login", json={"username": "alice", "password": "oldpass1"}).status_code == 401
    assert client.post("/v1/auth/login", json={"username": "alice", "password": "newpass1"}).status_code == 200
