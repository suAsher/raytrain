"""
Admin user lifecycle + per-user quota enforcement, end-to-end through the API.

Covers the product flow:
  - admin creates a user with grants + a per-user GPU quota
  - that user can submit within budget
  - the same user is rejected (403) once the ask would exceed the cap
  - admin updates the quota → the previously-rejected submit now passes
  - non-admin cannot hit /v1/admin/*
  - /v1/quota shows the caller's caps + usage

Uses the same fake RayClusterClient pattern as test_endpoints.py so no real
Ray cluster is needed. In-memory stores are reset per test via the autouse
fixture (the platform runs with in-memory stores when RAYTRAIN_DATABASE_URL is
unset, which is the case in tests).
"""
from __future__ import annotations

from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from raytrain_server.api.jobs import _ray_client
from raytrain_server.core import store as store_mod
from raytrain_server.core import users as users_mod
from raytrain_server.core.jwt_auth import issue_token
from raytrain_server.core.settings import Settings
from raytrain_server.main import create_app


class _FakeRay:
    """Minimal fake: records submissions, lists this-test's jobs back so the
    quota usage probe can sum running GPUs."""

    def __init__(self) -> None:
        self.submitted: list[dict] = []
        self._meta: dict[str, dict] = {}

    def address_for(self, gpu_type: str) -> str:
        return f"http://ray-shared-{gpu_type}:8265"

    def build_runtime_env(self, spec):
        return {"working_dir": spec.code_uri, "env_vars": {}, "config": {}}

    def submit_job(self, spec, submission_id: str, repo: str) -> str:
        self.submitted.append({"id": submission_id, "user": spec.user})
        self._meta[submission_id] = dict(spec.metadata)
        self._meta[submission_id]["raytrain.user"] = spec.user
        return submission_id

    def list_jobs(self, gpu_type: str):
        out = []
        for sid, meta in self._meta.items():
            j = type("J", (), {})()
            j.submission_id = sid
            j.status = "RUNNING"
            j.metadata = dict(meta)
            out.append(j)
        return out

    def get_info(self, gpu_type, submission_id):
        i = type("I", (), {})()
        i.status = "RUNNING"
        i.metadata = self._meta.get(submission_id, {})
        return i

    def stop(self, gpu_type, submission_id):
        return True

    def tail_logs(self, gpu_type, submission_id) -> Iterator[str]:
        yield "x\n"


@pytest.fixture(autouse=True)
def _reset_stores():
    """Fresh in-memory stores each test so users/jobs don't leak across tests."""
    users_mod.set_user_store(users_mod.UserStore())
    store_mod.set_devsession_store(store_mod.DevSessionStore())
    yield
    users_mod.set_user_store(users_mod.UserStore())
    store_mod.set_devsession_store(store_mod.DevSessionStore())


@pytest.fixture
def fake_ray():
    return _FakeRay()


@pytest.fixture
def client(settings: Settings, fake_ray) -> TestClient:
    app = create_app(settings=settings)
    app.dependency_overrides[_ray_client] = lambda: fake_ray
    return TestClient(app)


def _admin_h(settings):
    tok, _ = issue_token("root", tenant="default", role="admin", settings=settings)
    return {"Authorization": f"Bearer {tok}"}


def _user_h(settings, user="alice", tenant="team-a"):
    tok, _ = issue_token(user, tenant=tenant, role="user", settings=settings)
    return {"Authorization": f"Bearer {tok}"}


def _submit_body(gpus_per_node: int, num_nodes: int = 1) -> dict:
    return {
        "repo": "pointcept",
        "exp_name": "exp1",
        "gpu_type": "h20",
        "num_nodes": num_nodes,
        "gpus_per_node": gpus_per_node,
        "entrypoint": "python tools/train.py --config configs/x.py",
        "code_uri": "s3://raytrain-code/alice/job.zip",
    }


# --------------------------------------------------------------------------- #
# admin create / update
# --------------------------------------------------------------------------- #


def test_admin_creates_user_with_quota(client, settings):
    r = client.post(
        "/v1/admin/users",
        headers=_admin_h(settings),
        json={
            "user": "alice", "tenant": "team-a", "role": "user",
            "quota": {"max_gpus": 2, "max_jobs": 1},
            "projects": ["proj-a"], "datasets": ["scannet"],
            "issue_token": True, "token_days": 30,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["user"] == "alice"
    assert body["user"]["quota"]["max_gpus"] == 2
    assert body["token"]                      # a usable token was minted
    assert body["token_expires_at"] > 0


def test_non_admin_cannot_create_user(client, settings):
    r = client.post(
        "/v1/admin/users",
        headers=_user_h(settings),
        json={"user": "bob"},
    )
    assert r.status_code == 403


def test_duplicate_user_conflict(client, settings):
    h = _admin_h(settings)
    client.post("/v1/admin/users", headers=h, json={"user": "alice"})
    r = client.post("/v1/admin/users", headers=h, json={"user": "alice"})
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# quota enforcement at submit
# --------------------------------------------------------------------------- #


def test_submit_within_quota_ok(client, settings):
    client.post("/v1/admin/users", headers=_admin_h(settings),
                json={"user": "alice", "tenant": "team-a",
                      "quota": {"max_gpus": 2}})
    # ask for exactly 2 GPUs (1 node x 2) == cap → allowed
    r = client.post("/v1/jobs", headers=_user_h(settings),
                    json=_submit_body(gpus_per_node=2))
    assert r.status_code == 202, r.text


def test_submit_over_quota_rejected(client, settings):
    client.post("/v1/admin/users", headers=_admin_h(settings),
                json={"user": "alice", "tenant": "team-a",
                      "quota": {"max_gpus": 2}})
    # ask for 4 GPUs > cap 2 → 403 with a Chinese quota message
    r = client.post("/v1/jobs", headers=_user_h(settings),
                    json=_submit_body(gpus_per_node=4))
    assert r.status_code == 403, r.text
    assert "GPU" in r.json()["detail"]


def test_quota_counts_existing_running_jobs(client, settings):
    """Cap 4; first job takes 2 (ok), second job asks 4 → 2 used + 4 > 4 → 403."""
    client.post("/v1/admin/users", headers=_admin_h(settings),
                json={"user": "alice", "tenant": "team-a",
                      "quota": {"max_gpus": 4, "max_jobs": 5}})
    h = _user_h(settings)
    r1 = client.post("/v1/jobs", headers=h, json=_submit_body(gpus_per_node=2))
    assert r1.status_code == 202, r1.text
    r2 = client.post("/v1/jobs", headers=h, json=_submit_body(gpus_per_node=4))
    assert r2.status_code == 403, r2.text


def test_admin_update_quota_unblocks_submit(client, settings):
    ah = _admin_h(settings)
    client.post("/v1/admin/users", headers=ah,
                json={"user": "alice", "tenant": "team-a",
                      "quota": {"max_gpus": 1}})
    uh = _user_h(settings)
    # 2 GPUs > cap 1 → rejected
    assert client.post("/v1/jobs", headers=uh,
                       json=_submit_body(gpus_per_node=2)).status_code == 403
    # admin raises the cap to 8
    up = client.patch("/v1/admin/users/alice", headers=ah,
                      json={"quota": {"max_gpus": 8}})
    assert up.status_code == 200, up.text
    # now the same submit passes
    assert client.post("/v1/jobs", headers=uh,
                       json=_submit_body(gpus_per_node=2)).status_code == 202


def test_disabled_user_blocked(client, settings):
    ah = _admin_h(settings)
    client.post("/v1/admin/users", headers=ah,
                json={"user": "alice", "tenant": "team-a",
                      "quota": {"max_gpus": 8}})
    client.patch("/v1/admin/users/alice", headers=ah, json={"enabled": False})
    r = client.post("/v1/jobs", headers=_user_h(settings),
                    json=_submit_body(gpus_per_node=1))
    assert r.status_code == 403
    assert "禁用" in r.json()["detail"]


def test_unknown_user_unlimited(client, settings):
    """No user record → no caps enforced (bootstrap-friendly)."""
    r = client.post("/v1/jobs", headers=_user_h(settings, user="ghost"),
                    json=_submit_body(gpus_per_node=8))
    assert r.status_code == 202, r.text


def test_admin_bypasses_quota(client, settings):
    # even with a tiny cap on the admin's own user record, admin role bypasses
    client.post("/v1/admin/users", headers=_admin_h(settings),
                json={"user": "root", "quota": {"max_gpus": 1}})
    r = client.post("/v1/jobs", headers=_admin_h(settings),
                    json=_submit_body(gpus_per_node=8))
    assert r.status_code == 202, r.text


# --------------------------------------------------------------------------- #
# self-service quota view
# --------------------------------------------------------------------------- #


def test_my_quota_shows_caps(client, settings):
    client.post("/v1/admin/users", headers=_admin_h(settings),
                json={"user": "alice", "tenant": "team-a",
                      "quota": {"max_gpus": 4}})
    r = client.get("/v1/quota", headers=_user_h(settings))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"] == "alice"
    assert body["quota"]["max_gpus"] == 4
    assert body["remaining"]["gpus"] == 4   # nothing used yet
