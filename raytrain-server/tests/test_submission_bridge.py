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


def test_record_only_when_no_cluster(settings: Settings):
    # A100 is NOT in shared_clusters (only h20 is) → record-only, no submit
    fake = _FakeRay({"h20": "http://ray-shared-h20:8265"})
    ss_mod.set_submission_service(SubmissionService(settings=settings, ray=fake))
    client = _client(settings)

    r = client.post("/v1/console/jobs", headers=_h(settings), json=_body(gpu="A100"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["live"] is False
    assert body["status"] == "Queued"
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


def test_logs_endpoint_streams(settings: Settings):
    fake = _FakeRay({"h20": "http://ray-shared-h20:8265"})
    ss_mod.set_submission_service(SubmissionService(settings=settings, ray=fake))
    client = _client(settings)
    jid = client.post("/v1/console/jobs", headers=_h(settings), json=_body()).json()["id"]

    r = client.get(f"/v1/console/jobs/{jid}/logs", headers=_h(settings))
    assert r.status_code == 200
    assert "loss" in r.text
