"""
Console backing API (/v1/console/*) end-to-end through FastAPI.

Covers the workbench flows the web console depends on:
  - create job → list → detail (timeline/pods/events/metrics/artifacts present)
  - cancel / retry lifecycle
  - queues + overview roll up live used/pending from job records
  - tenant/owner visibility
  - auth required
Uses in-memory stores reset per test (demo seeding disabled in test settings).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from raytrain_server.core import jobs_store as js_mod
from raytrain_server.core import queues_store as qs_mod
from raytrain_server.core import submission_service as ss_mod
from raytrain_server.core.jwt_auth import issue_token
from raytrain_server.core.settings import Settings
from raytrain_server.main import create_app


@pytest.fixture(autouse=True)
def _reset_stores():
    js_mod.set_job_store(js_mod.JobStore())
    qs_mod.set_queue_store(qs_mod.QueueStore())
    # Force record-only mode (no real Ray) so console behavior is deterministic
    # regardless of the settings fixture's shared_clusters.
    ss_mod.set_submission_service(
        ss_mod.SubmissionService(settings=_NoClusterSettings(), ray=object())
    )
    yield
    js_mod.set_job_store(js_mod.JobStore())
    qs_mod.set_queue_store(qs_mod.QueueStore())
    ss_mod.set_submission_service(None)  # type: ignore[arg-type]


class _NoClusterSettings:
    """Minimal settings stub: no shared clusters → submission stays record-only."""

    shared_clusters: dict = {}


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings=settings))


def _h(settings, user="alice", tenant="team-a", role="user"):
    tok, _ = issue_token(user, tenant=tenant, role=role, settings=settings)
    return {"Authorization": f"Bearer {tok}"}


def _job_body(name="my-run", nodes=1, gpn=8, gpu="H20", queue="h20-shared"):
    return {
        "name": name,
        "project": "pointcept",
        "queue": queue,
        "priority": "normal",
        "image": "raytrain/pointcept:cu124-v3",
        "entrypoint": "python tools/train.py --config configs/x.py",
        "resources": {"gpuType": gpu, "nodes": nodes, "gpusPerNode": gpn},
        "mounts": {"datasetUri": "minio://datasets/pointcept", "checkpointUri": "minio://ck/x"},
    }


def test_auth_required(client):
    assert client.get("/v1/console/jobs").status_code in (401, 403)


def test_create_list_detail(client, settings):
    h = _h(settings)
    r = client.post("/v1/console/jobs", headers=h, json=_job_body())
    assert r.status_code == 201, r.text
    jid = r.json()["id"]
    assert r.json()["status"] == "Queued"

    lst = client.get("/v1/console/jobs", headers=h)
    assert lst.status_code == 200
    assert any(j["id"] == jid for j in lst.json())

    d = client.get(f"/v1/console/jobs/{jid}", headers=h)
    assert d.status_code == 200, d.text
    body = d.json()
    # rich detail payloads present
    for key in ("timeline", "pods", "events", "logs", "metrics", "artifacts", "rayJobYaml"):
        assert key in body
    assert len(body["timeline"]) >= 5
    assert any(p["role"] == "head" for p in body["pods"])


def test_cancel_and_retry(client, settings):
    h = _h(settings)
    jid = client.post("/v1/console/jobs", headers=h, json=_job_body()).json()["id"]

    c = client.post(f"/v1/console/jobs/{jid}/cancel", headers=h)
    assert c.status_code == 200
    assert c.json()["status"] == "Cancelled"

    rr = client.post(f"/v1/console/jobs/{jid}/retry", headers=h)
    assert rr.status_code == 201, rr.text
    new = rr.json()
    assert new["id"] != jid
    assert new["status"] == "Queued"
    assert new["name"].endswith("-retry")


def test_queues_rollup(client, settings):
    h = _h(settings)
    # two queued jobs in h20-shared → pending should reflect both
    client.post("/v1/console/jobs", headers=h, json=_job_body("q1", nodes=2))
    client.post("/v1/console/jobs", headers=h, json=_job_body("q2"))
    q = client.get("/v1/console/queues", headers=h)
    assert q.status_code == 200
    shared = [x for x in q.json() if x["name"] == "h20-shared"][0]
    assert shared["pending"] >= 2


def test_overview_counts(client, settings):
    h = _h(settings)
    client.post("/v1/console/jobs", headers=h, json=_job_body("a"))
    client.post("/v1/console/jobs", headers=h, json=_job_body("b"))
    o = client.get("/v1/console/overview", headers=h)
    assert o.status_code == 200, o.text
    body = o.json()
    assert body["counts"]["Queued"] >= 2
    assert "pools" in body and len(body["pools"]) >= 1


def test_tenant_visibility(client, settings):
    # alice (team-a) creates a job; bob (team-b) must not see it
    a = _h(settings, user="alice", tenant="team-a")
    b = _h(settings, user="bob", tenant="team-b")
    jid = client.post("/v1/console/jobs", headers=a, json=_job_body("secret")).json()["id"]
    assert client.get(f"/v1/console/jobs/{jid}", headers=b).status_code == 403
    names = [j["name"] for j in client.get("/v1/console/jobs", headers=b).json()]
    assert "secret" not in names


def test_admin_sees_all(client, settings):
    a = _h(settings, user="alice", tenant="team-a")
    adm = _h(settings, user="root", tenant="ops", role="admin")
    client.post("/v1/console/jobs", headers=a, json=_job_body("alice-job"))
    names = [j["name"] for j in client.get("/v1/console/jobs", headers=adm).json()]
    assert "alice-job" in names


def test_experiments_grouping(client, settings):
    h = _h(settings)
    body = _job_body("e1")
    body["experiment"] = "sweep-lr"
    client.post("/v1/console/jobs", headers=h, json=body)
    body2 = _job_body("e2")
    body2["experiment"] = "sweep-lr"
    client.post("/v1/console/jobs", headers=h, json=body2)
    exps = client.get("/v1/console/experiments", headers=h).json()
    sweep = [e for e in exps if e["name"] == "sweep-lr"]
    assert sweep and sweep[0]["runs"] == 2
