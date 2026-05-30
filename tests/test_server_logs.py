"""
Unit tests for the job lifecycle endpoints added in task 7.5
(raytrain/server/jobs.py):

* ``GET    /v1/jobs/{submission_id}/logs`` -- SSE log stream.
* ``DELETE /v1/jobs/{submission_id}``      -- stop a running job.
* ``GET    /v1/jobs?owner=``               -- list jobs, filtered by owner.

These tests run WITHOUT ray installed: a fake RayClusterClient is injected via
``app.dependency_overrides``. Auth uses HS256 self-signed tokens, mirroring
tests/test_server_submit.py.

The "timeout" part of the acceptance is covered by asserting the SSE generator
terminates cleanly when the upstream tail iterator is exhausted *or* raises --
the buffered TestClient read returning (rather than hanging) is the evidence.

Run with:
    PYTHONPATH=. python3 -m pytest tests/test_server_logs.py -v
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
from raytrain.server.ray_client import RayClientError  # noqa: E402


JWT_SECRET = "test-secret-key-at-least-32-bytes-long-xx"


# --------------------------------------------------------------------------- #
# Helpers / fakes
# --------------------------------------------------------------------------- #
def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def mint_token(sub: str = "alice", tenant: str | None = "team-a") -> str:
    """Mint a valid HS256 raytrain self-signed token."""
    claims = {"sub": sub, "iss": "raytrain", "exp": _now() + dt.timedelta(hours=1)}
    if tenant is not None:
        claims["tenant"] = tenant
    return jwt.encode(claims, JWT_SECRET, algorithm="HS256")


def auth_header(token: str | None = None) -> dict:
    tok = token if token is not None else mint_token()
    return {"Authorization": f"Bearer {tok}"}


class FakeRayClient:
    """Stand-in for RayClusterClient exposing tail_logs / stop_job / list_jobs.

    * ``tail_chunks`` is what ``tail_logs`` yields (or an Exception to raise).
    * ``stop_result`` is what ``stop_job`` returns (or an Exception to raise).
    * ``jobs_by_gpu`` maps gpu_type -> list of job dicts for ``list_jobs``.
    """

    def __init__(self, *, tail_chunks=None, stop_result=True, jobs_by_gpu=None,
                 cluster_urls=None):
        self._tail_chunks = tail_chunks
        self._stop_result = stop_result
        self._jobs_by_gpu = jobs_by_gpu or {}
        self._cluster_urls = cluster_urls or {"h20": "http://ray-shared-h20:8265"}
        self.tail_calls = []
        self.stop_calls = []
        self.list_calls = []

    @property
    def gpu_types(self):
        return sorted(self._cluster_urls)

    def tail_logs(self, gpu_type, submission_id):
        self.tail_calls.append((gpu_type, submission_id))
        if isinstance(self._tail_chunks, Exception):
            raise self._tail_chunks

        def _gen():
            for chunk in (self._tail_chunks or []):
                if isinstance(chunk, Exception):
                    raise chunk
                yield chunk

        return _gen()

    def stop_job(self, gpu_type, submission_id):
        self.stop_calls.append((gpu_type, submission_id))
        if isinstance(self._stop_result, Exception):
            raise self._stop_result
        return self._stop_result

    def list_jobs(self, gpu_type):
        self.list_calls.append(gpu_type)
        result = self._jobs_by_gpu.get(gpu_type, [])
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    """Every test needs the JWT secret configured for token verification."""
    monkeypatch.setenv("RAYTRAIN_JWT_SECRET", JWT_SECRET)


@pytest.fixture
def client():
    """A TestClient with server errors surfaced as responses (not raised)."""
    return TestClient(app, raise_server_exceptions=False)


def _override_ray(fake: FakeRayClient):
    app.dependency_overrides[get_ray_client] = lambda: fake


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_ray_client, None)


# --------------------------------------------------------------------------- #
# GET /v1/jobs/{id}/logs -- SSE
# --------------------------------------------------------------------------- #
def test_logs_stream_orders_lines(client):
    fake = FakeRayClient(tail_chunks=["line1\n", "line2\n", "line3\n"])
    _override_ray(fake)

    resp = client.get("/v1/jobs/sub-123/logs", headers=auth_header())

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")

    body = resp.text
    # Each chunk is wrapped as an SSE data frame.
    assert "data: line1\n" in body
    assert "data: line2\n" in body
    assert "data: line3\n" in body
    # Ordering preserved: line1 before line2 before line3.
    assert body.index("line1") < body.index("line2") < body.index("line3")
    # tail_logs was called with the submission_id from the URL.
    assert fake.tail_calls and fake.tail_calls[0][1] == "sub-123"


def test_logs_stream_explicit_gpu_type(client):
    fake = FakeRayClient(
        tail_chunks=["a\n"],
        cluster_urls={"h20": "http://h20:8265", "a100": "http://a100:8265"},
    )
    _override_ray(fake)

    resp = client.get(
        "/v1/jobs/sub-9/logs", params={"gpu_type": "a100"}, headers=auth_header()
    )

    assert resp.status_code == 200, resp.text
    # Routed straight to the requested gpu_type (no try-each).
    assert fake.tail_calls == [("a100", "sub-9")]


def test_logs_requires_auth(client):
    fake = FakeRayClient(tail_chunks=["line1\n"])
    _override_ray(fake)

    resp = client.get("/v1/jobs/sub-123/logs")  # no Authorization
    assert resp.status_code == 401
    assert fake.tail_calls == []


def test_logs_unknown_gpu_type_returns_400(client):
    fake = FakeRayClient(
        tail_chunks=RayClientError("unknown_gpu_type", "no cluster for tpu")
    )
    _override_ray(fake)

    resp = client.get(
        "/v1/jobs/sub-123/logs", params={"gpu_type": "tpu"}, headers=auth_header()
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "unknown_gpu_type"


def test_logs_upstream_error_returns_502(client):
    fake = FakeRayClient(tail_chunks=RayClientError("tail_failed", "dashboard down"))
    _override_ray(fake)

    resp = client.get(
        "/v1/jobs/sub-123/logs", params={"gpu_type": "h20"}, headers=auth_header()
    )

    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "tail_failed"


def test_logs_stream_terminates_when_upstream_ends(client):
    # An empty upstream iterator must yield a finite (empty) SSE body, not hang.
    fake = FakeRayClient(tail_chunks=[])
    _override_ray(fake)

    resp = client.get("/v1/jobs/sub-empty/logs", headers=auth_header())

    assert resp.status_code == 200, resp.text
    # Buffered read returned -> generator terminated cleanly. No data frames.
    assert "data:" not in resp.text


def test_logs_stream_handles_mid_stream_failure(client):
    # Upstream yields two lines then raises -> stream ends gracefully (the
    # buffered read returns rather than hanging or 500-ing post-headers).
    fake = FakeRayClient(
        tail_chunks=["line1\n", "line2\n", RayClientError("tail_failed", "boom")]
    )
    _override_ray(fake)

    resp = client.get("/v1/jobs/sub-mid/logs", headers=auth_header())

    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "data: line1\n" in body
    assert "data: line2\n" in body
    # Terminal error frame emitted so the client knows the stream was cut.
    assert "event: error" in body


# --------------------------------------------------------------------------- #
# DELETE /v1/jobs/{id}
# --------------------------------------------------------------------------- #
def test_delete_job_stops(client):
    fake = FakeRayClient(stop_result=True)
    _override_ray(fake)

    resp = client.delete(
        "/v1/jobs/sub-123", params={"gpu_type": "h20"}, headers=auth_header()
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["submission_id"] == "sub-123"
    assert body["stopped"] is True
    assert fake.stop_calls == [("h20", "sub-123")]


def test_delete_job_requires_auth(client):
    fake = FakeRayClient(stop_result=True)
    _override_ray(fake)

    resp = client.delete("/v1/jobs/sub-123")  # no Authorization
    assert resp.status_code == 401
    assert fake.stop_calls == []


def test_delete_job_unknown_gpu_type_returns_400(client):
    fake = FakeRayClient(
        stop_result=RayClientError("unknown_gpu_type", "no cluster for tpu")
    )
    _override_ray(fake)

    resp = client.delete(
        "/v1/jobs/sub-123", params={"gpu_type": "tpu"}, headers=auth_header()
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "unknown_gpu_type"


def test_delete_job_audit_log(client, caplog):
    import logging

    fake = FakeRayClient(stop_result=True)
    _override_ray(fake)

    with caplog.at_level(logging.INFO, logger="raytrain.audit"):
        resp = client.delete(
            "/v1/jobs/sub-777", params={"gpu_type": "h20"}, headers=auth_header()
        )

    assert resp.status_code == 200, resp.text
    audit = [r for r in caplog.records if r.name == "raytrain.audit"]
    text = "\n".join(r.getMessage() for r in audit)
    assert "sub-777" in text
    assert "alice" in text
    assert "stopped" in text


# --------------------------------------------------------------------------- #
# GET /v1/jobs?owner=
# --------------------------------------------------------------------------- #
def _job(submission_id, creator, status="RUNNING", gpu_type="h20"):
    return {
        "submission_id": submission_id,
        "job_id": submission_id,
        "status": status,
        "entrypoint": "python x.py",
        "metadata": {"creator": creator},
        "gpu_type": gpu_type,
    }


def test_list_jobs_filters_by_owner(client):
    fake = FakeRayClient(
        jobs_by_gpu={
            "h20": [
                _job("job-a", "alice"),
                _job("job-b", "bob"),
                _job("job-c", "alice"),
            ]
        }
    )
    _override_ray(fake)

    resp = client.get("/v1/jobs", params={"owner": "alice"}, headers=auth_header())

    assert resp.status_code == 200, resp.text
    jobs = resp.json()
    assert {j["submission_id"] for j in jobs} == {"job-a", "job-c"}
    assert all(j["metadata"]["creator"] == "alice" for j in jobs)


def test_list_jobs_default_returns_own_jobs(client):
    # Authenticated as alice; no owner= -> only alice's jobs.
    fake = FakeRayClient(
        jobs_by_gpu={
            "h20": [
                _job("job-a", "alice"),
                _job("job-b", "bob"),
            ]
        }
    )
    _override_ray(fake)

    resp = client.get("/v1/jobs", headers=auth_header(mint_token(sub="alice")))

    assert resp.status_code == 200, resp.text
    jobs = resp.json()
    assert [j["submission_id"] for j in jobs] == ["job-a"]


def test_list_jobs_aggregates_across_gpu_types(client):
    fake = FakeRayClient(
        jobs_by_gpu={
            "h20": [_job("job-a", "alice", gpu_type="h20")],
            "a100": [_job("job-d", "alice", gpu_type="a100")],
        },
        cluster_urls={"h20": "http://h20:8265", "a100": "http://a100:8265"},
    )
    _override_ray(fake)

    resp = client.get("/v1/jobs", params={"owner": "alice"}, headers=auth_header())

    assert resp.status_code == 200, resp.text
    jobs = resp.json()
    assert {j["submission_id"] for j in jobs} == {"job-a", "job-d"}
    assert sorted(fake.list_calls) == ["a100", "h20"]


def test_list_jobs_requires_auth(client):
    fake = FakeRayClient(jobs_by_gpu={"h20": []})
    _override_ray(fake)

    resp = client.get("/v1/jobs")  # no Authorization
    assert resp.status_code == 401
    assert fake.list_calls == []
