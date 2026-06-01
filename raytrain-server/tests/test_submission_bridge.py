"""
Console ↔ real Ray submission bridge.

Verifies that creating a job through /v1/console/jobs actually drives the Ray
submission path when a shared cluster is configured for the gpu_type, injects
code-as-submission (working_dir) + Ray Data/Lance env, reconciles live status,
and that without a configured cluster it degrades to a queued platform record.

Uses an injected fake RayClusterClient (no real Ray needed), same approach as
test_admin_quota.
"""
from __future__ import annotations

from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from raytrain_server.core import jobs_store as js_mod
from raytrain_server.core import queues_store as qs_mod
from raytrain_server.core import submission_service as ss_mod
from raytrain_server.core.jwt_auth import issue_token
from raytrain_server.core.settings import Settings
from raytrain_server.core.submission_service import SubmissionService
from raytrain_server.main import create_app


class _FakeRay:
    """Records the spec it was asked to submit; serves status back."""

    def __init__(self, clusters: dict[str, str]):
        self._clusters = clusters
        self.submitted: list = []
        self._status = "RUNNING"

    def address_for(self, gpu_type: str) -> str:
        return self._clusters[gpu_type]

    def submit_job(self, spec, submission_id: str, repo: str) -> str:
        self.submitted.append(spec)
        return submission_id

    def get_status(self, gpu_type: str, submission_id: str) -> str:
        return self._status

    def stop(self, gpu_type: str, submission_id: str) -> bool:
        self._status = "STOPPED"
        return True

    def tail_logs(self, gpu_type: str, submission_id: str) -> Iterator[str]:
        yield "epoch 1 | loss 4.2\n"


@pytest.fixture(autouse=True)
def _reset():
    js_mod.set_job_store(js_mod.JobStore())
    qs_mod.set_queue_store(qs_mod.QueueStore())
    yield
    js_mod.set_job_store(js_mod.JobStore())
    qs_mod.set_queue_store(qs_mod.QueueStore())
    ss_mod.set_submission_service(None)  # type: ignore[arg-type]
    # tests that swap the user store reset it here too (avoid cross-test leak)
    from raytrain_server.core import users as _users_mod
    _users_mod.set_user_store(_users_mod.UserStore())


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings=settings))


def _h(settings, user="alice", tenant="team-a", role="user"):
    tok, _ = issue_token(user, tenant=tenant, role=role, settings=settings)
    return {"Authorization": f"Bearer {tok}"}


def _body(gpu="H20", dataset="s3://datasets/scannet.lance"):
    return {
        "name": "real-run",
        "project": "pointcept",
        "queue": "h20-shared",
        "image": "raytrain/pointcept:cu124-v3",
        "entrypoint": "python tools/train.py --config configs/x.py",
        "resources": {"gpuType": gpu, "nodes": 2, "gpusPerNode": 8},
        "mounts": {"datasetUri": dataset, "checkpointUri": "s3://ck/x"},
        "code_uri": "s3://raytrain-code/alice/real-run.zip",
    }


def test_real_submit_when_cluster_configured(settings: Settings):
    # settings fixture configures shared_clusters={"h20": ...}
    fake = _FakeRay({"h20": "http://ray-shared-h20:8265"})
    ss_mod.set_submission_service(SubmissionService(settings=settings, ray=fake))
    client = _client(settings)

    r = client.post("/v1/console/jobs", headers=_h(settings), json=_body())
    assert r.status_code == 201, r.text
    body = r.json()
    # job went live: has a submission id + Starting status + live flag
    assert body["live"] is True
    assert body["submissionId"]
    assert body["status"] == "Starting"

    # the spec we sent to Ray carried code-as-submission + Lance env
    assert len(fake.submitted) == 1
    spec = fake.submitted[0]
    assert spec.code_uri == "s3://raytrain-code/alice/real-run.zip"
    assert spec.extra_env["RAYTRAIN_DATA_SOURCE_URI"] == "s3://datasets/scannet.lance"
    assert spec.num_nodes == 2 and spec.gpus_per_node == 8


def test_no_cluster_is_rejected(settings: Settings):
    # A100 is NOT in shared_clusters (only h20 is) → Req 5.4: reject, don't fake
    fake = _FakeRay({"h20": "http://ray-shared-h20:8265"})
    ss_mod.set_submission_service(SubmissionService(settings=settings, ray=fake))
    client = _client(settings)

    r = client.post("/v1/console/jobs", headers=_h(settings), json=_body(gpu="A100"))
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "NO_CLUSTER"
    assert fake.submitted == []


def test_reconcile_updates_status(settings: Settings):
    fake = _FakeRay({"h20": "http://ray-shared-h20:8265"})
    ss_mod.set_submission_service(SubmissionService(settings=settings, ray=fake))
    client = _client(settings)
    jid = client.post("/v1/console/jobs", headers=_h(settings), json=_body()).json()["id"]

    fake._status = "SUCCEEDED"
    d = client.get(f"/v1/console/jobs/{jid}", headers=_h(settings))
    assert d.json()["status"] == "Succeeded"


def test_cancel_stops_ray(settings: Settings):
    fake = _FakeRay({"h20": "http://ray-shared-h20:8265"})
    ss_mod.set_submission_service(SubmissionService(settings=settings, ray=fake))
    client = _client(settings)
    jid = client.post("/v1/console/jobs", headers=_h(settings), json=_body()).json()["id"]

    c = client.post(f"/v1/console/jobs/{jid}/cancel", headers=_h(settings))
    assert c.json()["status"] == "Cancelled"
    assert fake._status == "STOPPED"


def test_retry_resubmits_to_ray(settings: Settings):
    # Bug fix #2: Retry must actually re-run on Ray, not just create a record.
    fake = _FakeRay({"h20": "http://ray-shared-h20:8265"})
    ss_mod.set_submission_service(SubmissionService(settings=settings, ray=fake))
    client = _client(settings)
    jid = client.post("/v1/console/jobs", headers=_h(settings), json=_body()).json()["id"]
    assert len(fake.submitted) == 1  # original submit

    r = client.post(f"/v1/console/jobs/{jid}/retry", headers=_h(settings))
    assert r.status_code == 201, r.text
    body = r.json()
    # the retry is itself live (re-submitted) and carries the original code_uri
    assert body["live"] is True
    assert body["submissionId"]
    assert len(fake.submitted) == 2  # retry triggered a SECOND real submit
    assert fake.submitted[1].code_uri == "s3://raytrain-code/alice/real-run.zip"


def test_console_submit_enforces_quota_and_grants(settings: Settings):
    # Bug fix #1: the console submit path must run the SAME quota/authz checks
    # as /v1/jobs (it previously bypassed them entirely).
    from raytrain_server.core import users as users_mod
    from raytrain_server.core.users import UserRecord, UserQuota

    fake = _FakeRay({"h20": "http://ray-shared-h20:8265"})
    ss_mod.set_submission_service(SubmissionService(settings=settings, ray=fake))
    store = users_mod.UserStore()
    users_mod.set_user_store(store)
    # alice: 8-GPU cap, only allowed project "pointcept", image prefix "raytrain/"
    store.create(UserRecord(
        user="alice", tenant="team-a", role="user",
        quota=UserQuota(max_gpus=8),
        projects=["pointcept"], image_prefixes=["raytrain/"],
    ))
    client = _client(settings)

    # ask = 2 nodes * 8 = 16 GPUs > cap 8 → 403 QUOTA_EXCEEDED, no submit
    r = client.post("/v1/console/jobs", headers=_h(settings), json=_body())
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert fake.submitted == []

    # project not granted → 403 PROJECT_FORBIDDEN
    bad_proj = _body()
    bad_proj["project"] = "secret-proj"
    bad_proj["resources"] = {"gpuType": "H20", "nodes": 1, "gpusPerNode": 1}
    r2 = client.post("/v1/console/jobs", headers=_h(settings), json=bad_proj)
    assert r2.status_code == 403 and r2.json()["error"]["code"] == "PROJECT_FORBIDDEN"

    # within cap + granted project + allowed image → submits
    ok = _body()
    ok["resources"] = {"gpuType": "H20", "nodes": 1, "gpusPerNode": 4}
    r3 = client.post("/v1/console/jobs", headers=_h(settings), json=ok)
    assert r3.status_code == 201, r3.text
    assert r3.json()["live"] is True
    users_mod.set_user_store(users_mod.UserStore())


def test_logs_endpoint_uses_loki(settings: Settings):
    from raytrain_server.core import loki_client as loki_mod
    from raytrain_server.core.loki_client import FakeLokiClient, LogLine

    fake = _FakeRay({"h20": "http://ray-shared-h20:8265"})
    ss_mod.set_submission_service(SubmissionService(settings=settings, ray=fake))
    loki_mod.set_loki_client(FakeLokiClient([
        LogLine("2026-01-01T10:00:00Z", "worker-0", "INFO", "epoch 1 loss 4.2"),
    ]))
    client = _client(settings)
    jid = client.post("/v1/console/jobs", headers=_h(settings), json=_body()).json()["id"]

    r = client.get(f"/v1/console/jobs/{jid}/logs", headers=_h(settings))
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "loki"
    assert "loss" in body["lines"][0]["text"]
    loki_mod.set_loki_client(None)


# --------------------------------------------------------------------------- #
# StatusReconciler (Task 15 / Req 5.7 / Property 7)
# --------------------------------------------------------------------------- #


def test_status_reconciler_advances_and_respects_terminal(settings: Settings):
    from raytrain_server.core.jobs_store import JobStore, PlatformJob, JobResources
    from raytrain_server.core.status_reconciler import reconcile_once

    store = JobStore()
    js_mod.set_job_store(store)
    fake = _FakeRay({"h20": "http://ray-shared-h20:8265"})
    svc = SubmissionService(settings=settings, ray=fake)

    # a live, non-terminal job
    live = PlatformJob(
        id="job-live", name="x", user="alice", tenant="t", project="p",
        queue="h20-shared", status="Running", submission_id="sid-1",
        resources=JobResources(gpu_type="H20", nodes=1, gpus_per_node=8),
    )
    store.create(live)
    # a terminal job — must never be re-polled/rewritten
    term = PlatformJob(
        id="job-term", name="y", user="alice", tenant="t", project="p",
        queue="h20-shared", status="Succeeded", submission_id="sid-2",
        resources=JobResources(gpu_type="H20", nodes=1, gpus_per_node=8),
    )
    store.create(term)

    fake._status = "FAILED"
    changed = reconcile_once(store, svc)
    assert "job-live" in changed
    assert store.get("job-live").status == "Failed"
    # terminal untouched (Property 7)
    assert "job-term" not in changed
    assert store.get("job-term").status == "Succeeded"
