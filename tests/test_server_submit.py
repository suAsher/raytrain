"""
Unit tests for ``POST /v1/jobs`` (raytrain/server/jobs.py).

Covers the acceptance cases for task 7.4:

* happy-path: valid token + body -> 200, submission_id echoed, runtime_env
  assembled correctly (working_dir / config.setup_timeout_seconds / env_vars).
* token rejection: missing/bad token -> 401.
* upstream 5xx retry: transient ``submit_failed`` retried up to 3 times;
  succeeds on the 3rd attempt -> 200; fails all 3 -> 502, called exactly 3x.
* audit log: an ``raytrain.audit`` record carrying submission_id + owner.
* unknown gpu_type -> 400.

These tests run WITHOUT ray installed: a fake RayClusterClient is injected via
``app.dependency_overrides``. Retry backoff is patched to zero so nothing
sleeps.

Run with:
    PYTHONPATH=. python3 -m pytest tests/test_server_submit.py -v
"""
from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path

import jwt
import pytest

# Allow running directly: ROOT/raytrain importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from raytrain.server import jobs as jobs_module  # noqa: E402
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
    """Stand-in for RayClusterClient capturing submit_job calls.

    ``behaviors`` is a list applied per call: an Exception is raised, anything
    else is returned. When exhausted, the last behavior repeats.
    """

    def __init__(self, behaviors):
        self._behaviors = list(behaviors)
        self.calls = []  # list of kwargs dicts
        # Expose a mapping so the route's _cluster_for can resolve a URL.
        self._cluster_urls = {"h20": "http://ray-shared-h20:8265"}

    def submit_job(self, **kwargs):
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self._behaviors) - 1)
        behavior = self._behaviors[idx]
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    """Every test needs the JWT secret configured for token verification."""
    monkeypatch.setenv("RAYTRAIN_JWT_SECRET", JWT_SECRET)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Patch the retry backoff sleep so tests never wait."""
    monkeypatch.setattr(jobs_module.time, "sleep", lambda *_a, **_k: None)


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


def _valid_body() -> dict:
    return {
        "gpu_type": "h20",
        "num_nodes": 2,
        "gpus_per_node": 8,
        "code_uri": "s3://raytrain-code/alice/job.zip",
        "code_hash": "a3f8deadbeef",
        "extra_env": {"FOO": "bar"},
        "metadata": {"config": "configs/x.py"},
        "repo": "pointcept",
        "exp_name": "smoke",
    }


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_happy_path(client):
    fake = FakeRayClient(behaviors=["sub-123"])
    _override_ray(fake)

    resp = client.post("/v1/jobs", json=_valid_body(), headers=auth_header())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["submission_id"] == "sub-123"
    assert body["gpu_type"] == "h20"
    assert body["cluster"]

    # submit_job called exactly once with a well-formed runtime_env.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["gpu_type"] == "h20"
    assert call["entrypoint"] == "python -m raytrain.entrypoint.driver --from-env"

    rt = call["runtime_env"]
    assert rt["working_dir"] == "s3://raytrain-code/alice/job.zip"
    assert rt["config"]["setup_timeout_seconds"] == 600
    env_vars = rt["env_vars"]
    assert env_vars["TRAIN_NODES"] == "2"
    assert env_vars["TRAIN_GPUS_PER_NODE"] == "8"
    assert env_vars["RAYTRAIN_CODE_URI"] == "s3://raytrain-code/alice/job.zip"
    assert env_vars["RAYTRAIN_CODE_HASH"] == "a3f8deadbeef"
    assert env_vars["FOO"] == "bar"

    # metadata carries creator / tenant / gpu_type.
    meta = call["metadata"]
    assert meta["creator"] == "alice"
    assert meta["tenant"] == "team-a"
    assert meta["gpu_type"] == "h20"


def test_happy_path_without_code_uri_omits_working_dir(client):
    fake = FakeRayClient(behaviors=["sub-xyz"])
    _override_ray(fake)

    body = {"gpu_type": "h20", "num_nodes": 1, "gpus_per_node": 1}
    resp = client.post("/v1/jobs", json=body, headers=auth_header())

    assert resp.status_code == 200, resp.text
    rt = fake.calls[0]["runtime_env"]
    assert "working_dir" not in rt
    assert rt["env_vars"]["TRAIN_NODES"] == "1"


# --------------------------------------------------------------------------- #
# token rejection
# --------------------------------------------------------------------------- #
def test_token_rejected_missing(client):
    fake = FakeRayClient(behaviors=["sub-123"])
    _override_ray(fake)

    resp = client.post("/v1/jobs", json=_valid_body())  # no Authorization
    assert resp.status_code == 401
    # ray client must never be touched when auth fails.
    assert fake.calls == []


def test_token_rejected_bad_signature(client):
    fake = FakeRayClient(behaviors=["sub-123"])
    _override_ray(fake)

    bad = jwt.encode(
        {"sub": "mallory", "exp": _now() + dt.timedelta(hours=1)},
        "a-totally-different-secret",
        algorithm="HS256",
    )
    resp = client.post("/v1/jobs", json=_valid_body(), headers=auth_header(bad))
    assert resp.status_code == 401
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# upstream 5xx retry
# --------------------------------------------------------------------------- #
def test_upstream_5xx_retries_then_succeeds(client):
    # Fail twice (transient), succeed on the 3rd attempt.
    fake = FakeRayClient(
        behaviors=[
            RayClientError("submit_failed", "dashboard 503"),
            RayClientError("submit_failed", "dashboard 503"),
            "sub-final",
        ]
    )
    _override_ray(fake)

    resp = client.post("/v1/jobs", json=_valid_body(), headers=auth_header())

    assert resp.status_code == 200, resp.text
    assert resp.json()["submission_id"] == "sub-final"
    assert len(fake.calls) == 3


def test_upstream_5xx_fails_all_three_attempts(client):
    fake = FakeRayClient(
        behaviors=[RayClientError("submit_failed", "dashboard 503")]
    )
    _override_ray(fake)

    resp = client.post("/v1/jobs", json=_valid_body(), headers=auth_header())

    assert resp.status_code in (502, 503)
    body = resp.json()["detail"]
    assert body["code"] == "submit_failed"
    # Retried exactly 3 times.
    assert len(fake.calls) == 3


# --------------------------------------------------------------------------- #
# audit log
# --------------------------------------------------------------------------- #
def test_audit_log_written(client, caplog):
    fake = FakeRayClient(behaviors=["sub-123"])
    _override_ray(fake)

    with caplog.at_level(logging.INFO, logger="raytrain.audit"):
        resp = client.post("/v1/jobs", json=_valid_body(), headers=auth_header())

    assert resp.status_code == 200, resp.text
    audit_records = [r for r in caplog.records if r.name == "raytrain.audit"]
    assert audit_records, "expected an audit record on raytrain.audit"
    text = "\n".join(r.getMessage() for r in audit_records)
    assert "sub-123" in text
    assert "alice" in text


# --------------------------------------------------------------------------- #
# unknown gpu_type
# --------------------------------------------------------------------------- #
def test_unknown_gpu_type_returns_400(client):
    fake = FakeRayClient(
        behaviors=[RayClientError("unknown_gpu_type", "no cluster for tpu")]
    )
    _override_ray(fake)

    body = {**_valid_body(), "gpu_type": "tpu"}
    resp = client.post("/v1/jobs", json=body, headers=auth_header())

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["code"] == "unknown_gpu_type"
    # Not retried -- a client error.
    assert len(fake.calls) == 1
