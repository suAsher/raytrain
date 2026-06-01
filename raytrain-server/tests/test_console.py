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
from raytrain_server.core import kueue_reader as kq_mod
from raytrain_server.core import artifact_store as art_mod
from raytrain_server.core.kueue_reader import FakeKueueReader, QueueInfo
from raytrain_server.core.jwt_auth import issue_token
from raytrain_server.core.settings import Settings
from raytrain_server.main import create_app


_FAKE_QUEUES = [
    QueueInfo("h20-shared", "raytrain", "cq-h20", "H20", 64, 0, 0, 0),
    QueueInfo("a100-research", "raytrain", "cq-a100", "A100", 32, 0, 0, 0),
]


@pytest.fixture(autouse=True)
def _reset_stores():
    js_mod.set_job_store(js_mod.JobStore())
    qs_mod.set_queue_store(qs_mod.QueueStore())
    # Real Ray submission is exercised in test_submission_bridge; here we just
    # need the SubmissionService to not actually submit. The settings fixture
    # has shared_clusters={"h20":...} so cluster_configured(H20)=True passes the
    # create-time gate; submit() with a dummy ray records-only on failure.
    ss_mod.set_submission_service(
        ss_mod.SubmissionService(settings=_PassThroughSettings(), ray=object())
    )
    kq_mod.set_kueue_reader(FakeKueueReader(_FAKE_QUEUES))
    # No artifact store → artifacts list is empty + 'unavailable' (real-data only).
    art_mod.set_artifact_store(None)
    yield
    js_mod.set_job_store(js_mod.JobStore())
    qs_mod.set_queue_store(qs_mod.QueueStore())
    ss_mod.set_submission_service(None)  # type: ignore[arg-type]
    kq_mod.set_kueue_reader(None)  # type: ignore[arg-type]
    art_mod.set_artifact_store(None)  # type: ignore[arg-type]


class _PassThroughSettings:
    """Settings stub: no shared clusters → SubmissionService.submit is a no-op
    record-only path (so console job records stay deterministic for these tests)."""

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
    # Req 14.5 — a record-only job (no real Ray submission) does NOT synthesize
    # pods/events; they are explicitly empty + flagged unavailable.
    assert body["pods"] == []
    assert body["pods_source"] == "unavailable"
    # Req 14.6 — no artifact store configured → artifacts empty + unavailable.
    assert body["artifacts"] == []
    assert body["artifacts_source"] == "unavailable"


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


def test_queues_from_kueue(client, settings):
    # /queues now returns the cluster's real Kueue queues (FakeKueueReader here),
    # not a hardcoded seed; recentJobs is enriched from the JobStore.
    h = _h(settings)
    client.post("/v1/console/jobs", headers=h, json=_job_body("q1", nodes=2))
    q = client.get("/v1/console/queues", headers=h)
    assert q.status_code == 200
    names = {x["name"] for x in q.json()}
    assert names == {"h20-shared", "a100-research"}   # exactly the fake Kueue set
    shared = [x for x in q.json() if x["name"] == "h20-shared"][0]
    assert shared["source"] == "kueue"
    assert any(rj["name"] == "q1" for rj in shared["recentJobs"])


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


def test_artifacts_from_store(client, settings):
    # With a FakeArtifactStore injected, the detail + /artifacts endpoints serve
    # REAL artifacts listed from the job's checkpoint URI (Req 14.6).
    from raytrain_server.core.artifact_store import Artifact, FakeArtifactStore

    art_mod.set_artifact_store(
        FakeArtifactStore([
            Artifact("epoch_5.pth", "checkpoint", "1.8 GB", "s3://ck/x/epoch_5.pth", ""),
            Artifact("eval.json", "eval", "10 KB", "s3://ck/x/eval.json", ""),
        ])
    )
    h = _h(settings)
    jid = client.post("/v1/console/jobs", headers=h, json=_job_body()).json()["id"]

    d = client.get(f"/v1/console/jobs/{jid}", headers=h).json()
    assert d["artifacts_source"] == "minio"
    assert {a["name"] for a in d["artifacts"]} == {"epoch_5.pth", "eval.json"}

    rows = client.get("/v1/console/artifacts", headers=h).json()
    assert any(r["name"] == "epoch_5.pth" and r["jobId"] == jid for r in rows)
