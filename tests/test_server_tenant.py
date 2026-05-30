"""
Unit tests for multi-tenant isolation (task 9.3) in raytrain/server/jobs.py.

Two parts are covered:

PART A -- ``RAYTRAIN_TENANT`` injection on the submit path:
  * a token carrying ``tenant`` injects ``env_vars["RAYTRAIN_TENANT"]``;
  * a token without a tenant claim leaves the key absent.

PART B -- ``tenant_isolation: strict`` guard on existing-job operations:
  * cross-tenant ``GET /v1/jobs/{id}/logs`` -> 403 ``tenant_forbidden`` and
    the upstream ``tail_logs`` is never called;
  * same-tenant logs -> 200 stream;
  * isolation off (default) -> cross-tenant logs allowed (backward compat);
  * cross-tenant ``DELETE /v1/jobs/{id}`` under strict -> 403.

These tests run WITHOUT ray installed: a fake RayClusterClient is injected via
``app.dependency_overrides``. Auth uses HS256 self-signed tokens, mirroring
tests/test_server_submit.py and tests/test_server_logs.py. Strict mode is opted
into per-test via ``monkeypatch.setenv(RAYTRAIN_TENANT_ISOLATION, "strict")``.

Run with:
    PYTHONPATH=. python3 -m pytest tests/test_server_tenant.py -v
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import jwt
import pytest

# Allow running directly: ROOT/raytrain importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from raytrain.server.app import app  # noqa: E402
from raytrain.server.jobs import get_ray_client  # noqa: E402


JWT_SECRET = "test-secret-key-at-least-32-bytes-long-xx"


# --------------------------------------------------------------------------- #
# Helpers / fakes
# --------------------------------------------------------------------------- #
def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def mint_token(sub: str = "alice", tenant: str | None = "team-a") -> str:
    """Mint a valid HS256 raytrain self-signed token.

    ``tenant=None`` mints a token WITHOUT a tenant claim.
    """
    claims = {"sub": sub, "iss": "raytrain", "exp": _now() + dt.timedelta(hours=1)}
    if tenant is not None:
        claims["tenant"] = tenant
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def auth_header(token: str | None = None) -> dict:
    tok = token if token is not None else mint_token()
    return {"Authorization": f"Bearer {tok}"}


class FakeSubmitClient:
    """Stand-in for RayClusterClient capturing submit_job kwargs (PART A)."""

    def __init__(self, submit_id: str = "sub-1"):
        self._submit_id = submit_id
        self.calls = []  # list of kwargs dicts
        self._cluster_urls = {"h20": "http://ray-shared-h20:8265"}

    def submit_job(self, **kwargs):
        self.calls.append(kwargs)
        return self._submit_id


class FakeExistingJobClient:
    """Stand-in for RayClusterClient for existing-job ops (PART B).

    * ``job_tenant`` is the tenant recorded in the target job's metadata; it is
      returned by both ``get_job_info`` and ``list_jobs``.
    * ``tail_logs`` records its calls so a test can assert it is NOT invoked
      when the tenant guard rejects the request.
    """

    def __init__(self, *, job_tenant="team-b", submission_id="sub-123",
                 tail_chunks=None, cluster_urls=None):
        self._job_tenant = job_tenant
        self._submission_id = submission_id
        self._tail_chunks = tail_chunks if tail_chunks is not None else ["line1\n"]
        self._cluster_urls = cluster_urls or {"h20": "http://ray-shared-h20:8265"}
        self.tail_calls = []
        self.stop_calls = []
        self.info_calls = []
        self.list_calls = []

    @property
    def gpu_types(self):
        return sorted(self._cluster_urls)

    def _job_dict(self):
        meta = {"creator": "someone"}
        if self._job_tenant is not None:
            meta["tenant"] = self._job_tenant
        return {
            "submission_id": self._submission_id,
            "job_id": self._submission_id,
            "status": "RUNNING",
            "entrypoint": "python x.py",
            "metadata": meta,
            "gpu_type": "h20",
        }

    def get_job_info(self, gpu_type, submission_id):
        self.info_calls.append((gpu_type, submission_id))
        return self._job_dict()

    def list_jobs(self, gpu_type):
        self.list_calls.append(gpu_type)
        return [self._job_dict()]

    def tail_logs(self, gpu_type, submission_id):
        self.tail_calls.append((gpu_type, submission_id))

        def _gen():
            for chunk in self._tail_chunks:
                yield chunk

        return _gen()

    def stop_job(self, gpu_type, submission_id):
        self.stop_calls.append((gpu_type, submission_id))
        return True


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    """Every test needs the JWT secret configured for token verification."""
    monkeypatch.setenv("RAYTRAIN_JWT_SECRET", JWT_SECRET)


@pytest.fixture(autouse=True)
def _isolation_off_by_default(monkeypatch):
    """Default state: isolation OFF (env unset) -> backward-compatible.

    Tests that need strict mode opt in explicitly via ``monkeypatch.setenv``.
    """
    monkeypatch.delenv("RAYTRAIN_TENANT_ISOLATION", raising=False)


@pytest.fixture
def client():
    """A TestClient with server errors surfaced as responses (not raised)."""
    return TestClient(app, raise_server_exceptions=False)


def _override_ray(fake):
    app.dependency_overrides[get_ray_client] = lambda: fake


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_ray_client, None)


def _submit_body() -> dict:
    return {
        "gpu_type": "h20",
        "num_nodes": 1,
        "gpus_per_node": 1,
        "code_uri": "s3://raytrain-code/alice/job.zip",
    }


# --------------------------------------------------------------------------- #
# PART A -- RAYTRAIN_TENANT injection on submit
# --------------------------------------------------------------------------- #
def test_submit_injects_tenant_env(client):
    fake = FakeSubmitClient(submit_id="sub-1")
    _override_ray(fake)

    resp = client.post(
        "/v1/jobs", json=_submit_body(), headers=auth_header(mint_token(tenant="team-a"))
    )

    assert resp.status_code == 200, resp.text
    assert len(fake.calls) == 1
    env_vars = fake.calls[0]["runtime_env"]["env_vars"]
    assert env_vars["RAYTRAIN_TENANT"] == "team-a"
    # metadata still carries the tenant too (unchanged behaviour).
    assert fake.calls[0]["metadata"]["tenant"] == "team-a"


def test_submit_no_tenant_no_env(client):
    fake = FakeSubmitClient(submit_id="sub-2")
    _override_ray(fake)

    resp = client.post(
        "/v1/jobs", json=_submit_body(), headers=auth_header(mint_token(tenant=None))
    )

    assert resp.status_code == 200, resp.text
    assert len(fake.calls) == 1
    env_vars = fake.calls[0]["runtime_env"]["env_vars"]
    assert "RAYTRAIN_TENANT" not in env_vars
    # No tenant claim -> no tenant in metadata either.
    assert "tenant" not in fake.calls[0]["metadata"]


def test_submit_tenant_env_not_spoofable_via_extra_env(client):
    # A caller cannot override their token-derived tenant via extra_env: the
    # token claim is authoritative.
    fake = FakeSubmitClient(submit_id="sub-3")
    _override_ray(fake)

    body = {**_submit_body(), "extra_env": {"RAYTRAIN_TENANT": "team-evil"}}
    resp = client.post(
        "/v1/jobs", json=body, headers=auth_header(mint_token(tenant="team-a"))
    )

    assert resp.status_code == 200, resp.text
    env_vars = fake.calls[0]["runtime_env"]["env_vars"]
    assert env_vars["RAYTRAIN_TENANT"] == "team-a"


# --------------------------------------------------------------------------- #
# PART B -- strict tenant isolation on logs / delete
# --------------------------------------------------------------------------- #
def test_logs_cross_tenant_forbidden_strict(client, monkeypatch):
    monkeypatch.setenv("RAYTRAIN_TENANT_ISOLATION", "strict")
    # Caller is team-a; the job belongs to team-b.
    fake = FakeExistingJobClient(job_tenant="team-b", submission_id="sub-123")
    _override_ray(fake)

    resp = client.get(
        "/v1/jobs/sub-123/logs",
        params={"gpu_type": "h20"},
        headers=auth_header(mint_token(sub="alice", tenant="team-a")),
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "tenant_forbidden"
    # The upstream log stream must never be touched for a forbidden caller.
    assert fake.tail_calls == []


def test_logs_same_tenant_allowed_strict(client, monkeypatch):
    monkeypatch.setenv("RAYTRAIN_TENANT_ISOLATION", "strict")
    # Caller and job are both team-a.
    fake = FakeExistingJobClient(
        job_tenant="team-a", submission_id="sub-123", tail_chunks=["hello\n"]
    )
    _override_ray(fake)

    resp = client.get(
        "/v1/jobs/sub-123/logs",
        params={"gpu_type": "h20"},
        headers=auth_header(mint_token(sub="alice", tenant="team-a")),
    )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "data: hello\n" in resp.text
    assert fake.tail_calls == [("h20", "sub-123")]


def test_logs_isolation_off_allows_cross_tenant(client):
    # Default (env unset) -> isolation off -> cross-tenant logs allowed.
    fake = FakeExistingJobClient(
        job_tenant="team-b", submission_id="sub-123", tail_chunks=["hi\n"]
    )
    _override_ray(fake)

    resp = client.get(
        "/v1/jobs/sub-123/logs",
        params={"gpu_type": "h20"},
        headers=auth_header(mint_token(sub="alice", tenant="team-a")),
    )

    assert resp.status_code == 200, resp.text
    assert "data: hi\n" in resp.text
    # Streaming proceeded -> tail_logs was invoked despite tenant mismatch.
    assert fake.tail_calls == [("h20", "sub-123")]


def test_delete_cross_tenant_forbidden_strict(client, monkeypatch):
    monkeypatch.setenv("RAYTRAIN_TENANT_ISOLATION", "strict")
    fake = FakeExistingJobClient(job_tenant="team-b", submission_id="sub-123")
    _override_ray(fake)

    resp = client.delete(
        "/v1/jobs/sub-123",
        params={"gpu_type": "h20"},
        headers=auth_header(mint_token(sub="alice", tenant="team-a")),
    )

    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"]["code"] == "tenant_forbidden"
    # The stop must never be issued for a forbidden caller.
    assert fake.stop_calls == []


def test_delete_same_tenant_allowed_strict(client, monkeypatch):
    monkeypatch.setenv("RAYTRAIN_TENANT_ISOLATION", "strict")
    fake = FakeExistingJobClient(job_tenant="team-a", submission_id="sub-123")
    _override_ray(fake)

    resp = client.delete(
        "/v1/jobs/sub-123",
        params={"gpu_type": "h20"},
        headers=auth_header(mint_token(sub="alice", tenant="team-a")),
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["stopped"] is True
    assert fake.stop_calls == [("h20", "sub-123")]


def test_list_jobs_strict_filters_to_caller_tenant(client, monkeypatch):
    monkeypatch.setenv("RAYTRAIN_TENANT_ISOLATION", "strict")

    class _ListClient:
        def __init__(self):
            self._cluster_urls = {"h20": "http://h20:8265"}

        @property
        def gpu_types(self):
            return ["h20"]

        def list_jobs(self, gpu_type):
            return [
                {
                    "submission_id": "job-a",
                    "status": "RUNNING",
                    "metadata": {"creator": "alice", "tenant": "team-a"},
                    "gpu_type": "h20",
                },
                {
                    "submission_id": "job-b",
                    "status": "RUNNING",
                    "metadata": {"creator": "alice", "tenant": "team-b"},
                    "gpu_type": "h20",
                },
            ]

    fake = _ListClient()
    _override_ray(fake)

    resp = client.get(
        "/v1/jobs",
        params={"owner": "alice"},
        headers=auth_header(mint_token(sub="alice", tenant="team-a")),
    )

    assert resp.status_code == 200, resp.text
    jobs = resp.json()
    # Only the team-a job survives the strict tenant filter.
    assert {j["submission_id"] for j in jobs} == {"job-a"}
