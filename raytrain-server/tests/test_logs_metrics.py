"""
Loki logs (Task 18 / Req 8) and Prometheus metrics (Task 19 / Req 10), end to
end through /v1/console/jobs/{id}/logs and /metrics. Property 2: real data or
explicit 'unavailable' / FriendlyError — never synthesized.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from raytrain_server.core import jobs_store as js_mod
from raytrain_server.core import loki_client as loki_mod
from raytrain_server.core import prometheus_client as prom_mod
from raytrain_server.core.jobs_store import JobResources, PlatformJob
from raytrain_server.core.loki_client import FakeLokiClient, LogLine
from raytrain_server.core.prometheus_client import FakePrometheusClient, MetricSeries
from raytrain_server.core.jwt_auth import issue_token
from raytrain_server.core.settings import Settings
from raytrain_server.main import create_app


@pytest.fixture(autouse=True)
def _reset():
    js_mod.set_job_store(js_mod.JobStore())
    loki_mod.set_loki_client(None)
    prom_mod.set_prometheus_client(None)
    yield
    js_mod.set_job_store(js_mod.JobStore())
    loki_mod.set_loki_client(None)
    prom_mod.set_prometheus_client(None)


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings=settings), raise_server_exceptions=False)


def _h(settings, user="alice"):
    tok, _ = issue_token(user, tenant="t1", role="admin", settings=settings)
    return {"Authorization": f"Bearer {tok}"}


def _live_job():
    j = PlatformJob(
        id="job-x", name="run", user="alice", tenant="t1", project="p",
        queue="h20-shared", status="Running", submission_id="alice-p-run-1",
        resources=JobResources(gpu_type="H20", nodes=1, gpus_per_node=8),
        created_at=time.time() - 600,
    )
    js_mod.get_job_store().create(j)
    return j


# ---------------- Loki ----------------

def test_logs_from_loki(client, settings):
    _live_job()
    loki_mod.set_loki_client(FakeLokiClient([
        LogLine("2026-01-01T10:00:00Z", "worker-0", "INFO", "epoch 1 loss 4.2"),
        LogLine("2026-01-01T10:01:00Z", "worker-0", "ERROR", "CUDA OOM"),
    ]))
    r = client.get("/v1/console/jobs/job-x/logs", headers=_h(settings))
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "loki"
    assert len(body["lines"]) == 2
    assert body["lines"][1]["level"] == "ERROR"


def test_logs_unavailable_when_no_loki(client, settings):
    _live_job()
    # no loki client configured → explicit unavailable, not synthesized
    r = client.get("/v1/console/jobs/job-x/logs", headers=_h(settings))
    assert r.status_code == 200
    assert r.json()["source"] == "unavailable"
    assert r.json()["lines"] == []


def test_logs_loki_failure_is_friendly(client, settings):
    _live_job()
    loki_mod.set_loki_client(FakeLokiClient(fail=True))
    r = client.get("/v1/console/jobs/job-x/logs", headers=_h(settings))
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "LOKI_UNAVAILABLE"


def test_logs_container_filter(client, settings):
    _live_job()
    loki_mod.set_loki_client(FakeLokiClient([
        LogLine("t", "worker-0", "INFO", "a"),
        LogLine("t", "ray-head", "INFO", "b"),
    ]))
    r = client.get("/v1/console/jobs/job-x/logs?container=ray-head", headers=_h(settings))
    lines = r.json()["lines"]
    assert len(lines) == 1 and lines[0]["container"] == "ray-head"


# ---------------- Prometheus ----------------

def test_metrics_from_prometheus(client, settings):
    _live_job()
    prom_mod.set_prometheus_client(FakePrometheusClient())
    r = client.get("/v1/console/jobs/job-x/metrics", headers=_h(settings))
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "prometheus"
    by_metric = {s["metric"]: s for s in body["series"]}
    assert by_metric["gpu_util"]["source"] == "prometheus"
    assert by_metric["gpu_util"]["points"][0]["value"] == 88.0
    # empty metric flagged unavailable, not faked
    assert by_metric["gpu_mem"]["source"] == "unavailable"
    assert by_metric["gpu_mem"]["points"] == []


def test_metrics_unavailable_when_no_prom(client, settings):
    _live_job()
    r = client.get("/v1/console/jobs/job-x/metrics", headers=_h(settings))
    assert r.status_code == 200
    assert r.json()["source"] == "unavailable"


def test_metrics_prom_failure_is_friendly(client, settings):
    _live_job()
    prom_mod.set_prometheus_client(FakePrometheusClient(fail=True))
    r = client.get("/v1/console/jobs/job-x/metrics", headers=_h(settings))
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "PROM_UNAVAILABLE"


def test_http_prom_aggregates(monkeypatch):
    # unit: HttpPrometheusClient averages series per timestamp
    from raytrain_server.core.prometheus_client import HttpPrometheusClient

    c = HttpPrometheusClient("http://prom")
    monkeypatch.setattr(c, "_query_range", lambda *a, **k: [
        {"values": [[1700000000, "80"], [1700000060, "90"]]},
        {"values": [[1700000000, "100"]]},
    ])
    series = c.job_metrics("sid", 0, 1, 60)
    gpu = [s for s in series if s.metric == "gpu_util"][0]
    # ts 1700000000 avg(80,100)=90
    assert gpu.points[0]["value"] == 90.0
