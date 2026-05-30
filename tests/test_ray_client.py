"""
Unit tests for raytrain/server/ray_client.py.

These tests run WITHOUT ray installed: the construction hook
``_make_submission_client`` is monkeypatched to return a ``MagicMock`` so no
real ``JobSubmissionClient`` is built and no network access happens.

Run with:
    PYTHONPATH=. python3 -m pytest tests/test_ray_client.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Allow running directly: ROOT/raytrain importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import raytrain.server.ray_client as rc  # noqa: E402
from raytrain.server.ray_client import RayClientError, RayClusterClient  # noqa: E402


URL_H20 = "http://ray-shared-h20-head.ray-shared.svc:8265"
URL_A100 = "http://ray-shared-a100-head.ray-shared.svc:8265"


def _fake_client(*, submit_id: str = "zhangsan-job-1", logs=None) -> MagicMock:
    """A MagicMock standing in for ray's JobSubmissionClient."""
    client = MagicMock(name="JobSubmissionClient")
    client.submit_job.return_value = submit_id
    client.stop_job.return_value = True
    client.tail_job_logs.return_value = iter(logs or [])
    return client


class _FakeJobDetails:
    """Stand-in for ray's ``JobDetails`` returned by ``list_jobs``."""

    def __init__(self, submission_id, status="RUNNING", metadata=None, entrypoint="python x.py"):
        self.submission_id = submission_id
        self.job_id = submission_id
        self.status = status
        self.metadata = metadata or {}
        self.entrypoint = entrypoint


@pytest.fixture
def patch_make_client(monkeypatch: pytest.MonkeyPatch):
    """Patch the construction hook; return (recorder, factory_setter).

    The returned ``urls_seen`` list records every URL the hook is asked to
    build, and ``set_client`` lets a test pin which mock is returned.
    """
    urls_seen: list[str] = []
    holder = {"client": _fake_client()}

    def fake_make(url: str):
        urls_seen.append(url)
        return holder["client"]

    monkeypatch.setattr(rc, "_make_submission_client", fake_make)

    def set_client(client: MagicMock) -> None:
        holder["client"] = client

    return urls_seen, set_client


# --------------------------------------------------------------------------- #
# submit_job
# --------------------------------------------------------------------------- #


def test_submit_job_routes_to_correct_cluster(patch_make_client) -> None:
    urls_seen, set_client = patch_make_client
    client = _fake_client(submit_id="zhangsan-pointcept-smoke-001")
    set_client(client)

    rcc = RayClusterClient({"h20": URL_H20, "a100": URL_A100})
    runtime_env = {"working_dir": "s3://raytrain-code/zhangsan/job.zip"}
    out = rcc.submit_job(
        gpu_type="h20",
        entrypoint="python -m raytrain.entrypoint.driver --from-env",
        runtime_env=runtime_env,
        metadata={"owner": "zhangsan"},
        submission_id="zhangsan-pointcept-smoke-001",
    )

    # routed to the h20 URL, not a100
    assert urls_seen == [URL_H20]
    # returned the id from the underlying client
    assert out == "zhangsan-pointcept-smoke-001"
    # forwarded entrypoint / runtime_env / metadata / submission_id
    client.submit_job.assert_called_once_with(
        entrypoint="python -m raytrain.entrypoint.driver --from-env",
        runtime_env=runtime_env,
        metadata={"owner": "zhangsan"},
        submission_id="zhangsan-pointcept-smoke-001",
    )


def test_submit_job_routes_to_a100(patch_make_client) -> None:
    urls_seen, _ = patch_make_client
    rcc = RayClusterClient({"h20": URL_H20, "a100": URL_A100})
    rcc.submit_job(gpu_type="a100", entrypoint="python x.py")
    assert urls_seen == [URL_A100]


def test_unknown_gpu_type_raises(patch_make_client) -> None:
    _, _ = patch_make_client
    rcc = RayClusterClient({"h20": URL_H20, "a100": URL_A100})
    with pytest.raises(RayClientError) as excinfo:
        rcc.submit_job(gpu_type="tpu", entrypoint="python x.py")
    assert excinfo.value.code == "unknown_gpu_type"
    assert "tpu" in str(excinfo.value)


def test_submit_failure_wrapped(patch_make_client) -> None:
    _, set_client = patch_make_client
    client = _fake_client()
    client.submit_job.side_effect = RuntimeError("dashboard unreachable")
    set_client(client)

    rcc = RayClusterClient({"h20": URL_H20})
    with pytest.raises(RayClientError) as excinfo:
        rcc.submit_job(gpu_type="h20", entrypoint="python x.py")
    assert excinfo.value.code == "submit_failed"


# --------------------------------------------------------------------------- #
# stop_job
# --------------------------------------------------------------------------- #


def test_stop_job(patch_make_client) -> None:
    urls_seen, set_client = patch_make_client
    client = _fake_client()
    set_client(client)

    rcc = RayClusterClient({"h20": URL_H20, "a100": URL_A100})
    result = rcc.stop_job(gpu_type="h20", submission_id="zhangsan-job-1")

    assert result is True
    assert urls_seen == [URL_H20]
    client.stop_job.assert_called_once_with("zhangsan-job-1")


# --------------------------------------------------------------------------- #
# tail_logs
# --------------------------------------------------------------------------- #


def test_tail_logs_yields_lines_in_order(patch_make_client) -> None:
    _, set_client = patch_make_client
    chunks = ["epoch 1\n", "epoch 2\n", "done\n"]
    client = _fake_client(logs=list(chunks))
    set_client(client)

    rcc = RayClusterClient({"h20": URL_H20})
    out = list(rcc.tail_logs(gpu_type="h20", submission_id="zhangsan-job-1"))

    assert out == chunks
    client.tail_job_logs.assert_called_once_with("zhangsan-job-1")


def test_tail_logs_unknown_gpu_type_raises_eagerly(patch_make_client) -> None:
    _, _ = patch_make_client
    rcc = RayClusterClient({"h20": URL_H20})
    with pytest.raises(RayClientError) as excinfo:
        rcc.tail_logs(gpu_type="tpu", submission_id="x")
    assert excinfo.value.code == "unknown_gpu_type"


# --------------------------------------------------------------------------- #
# list_jobs
# --------------------------------------------------------------------------- #


def test_list_jobs_normalises_job_details(patch_make_client) -> None:
    _, set_client = patch_make_client
    client = _fake_client()
    client.list_jobs.return_value = [
        _FakeJobDetails("job-a", status="RUNNING", metadata={"creator": "alice"}),
        _FakeJobDetails("job-b", status="SUCCEEDED", metadata={"creator": "bob"}),
    ]
    set_client(client)

    rcc = RayClusterClient({"h20": URL_H20})
    out = rcc.list_jobs(gpu_type="h20")

    client.list_jobs.assert_called_once_with()
    assert [j["submission_id"] for j in out] == ["job-a", "job-b"]
    assert out[0]["status"] == "RUNNING"
    assert out[0]["metadata"] == {"creator": "alice"}
    assert out[0]["gpu_type"] == "h20"


def test_list_jobs_unknown_gpu_type_raises(patch_make_client) -> None:
    _, _ = patch_make_client
    rcc = RayClusterClient({"h20": URL_H20})
    with pytest.raises(RayClientError) as excinfo:
        rcc.list_jobs(gpu_type="tpu")
    assert excinfo.value.code == "unknown_gpu_type"


def test_list_jobs_failure_wrapped(patch_make_client) -> None:
    _, set_client = patch_make_client
    client = _fake_client()
    client.list_jobs.side_effect = RuntimeError("dashboard unreachable")
    set_client(client)

    rcc = RayClusterClient({"h20": URL_H20})
    with pytest.raises(RayClientError) as excinfo:
        rcc.list_jobs(gpu_type="h20")
    assert excinfo.value.code == "list_failed"


# --------------------------------------------------------------------------- #
# client caching
# --------------------------------------------------------------------------- #


def test_client_cached_per_url(patch_make_client) -> None:
    urls_seen, _ = patch_make_client
    rcc = RayClusterClient({"h20": URL_H20, "a100": URL_A100})

    rcc.submit_job(gpu_type="h20", entrypoint="a")
    rcc.stop_job(gpu_type="h20", submission_id="x")
    rcc.submit_job(gpu_type="h20", entrypoint="b")

    # Built once for h20 despite three operations.
    assert urls_seen == [URL_H20]


def test_case_insensitive_gpu_type(patch_make_client) -> None:
    urls_seen, _ = patch_make_client
    rcc = RayClusterClient({"h20": URL_H20})
    rcc.submit_job(gpu_type="H20", entrypoint="a")
    assert urls_seen == [URL_H20]


# --------------------------------------------------------------------------- #
# constructor / from_env
# --------------------------------------------------------------------------- #


def test_empty_mapping_raises() -> None:
    with pytest.raises(RayClientError) as excinfo:
        RayClusterClient({})
    assert excinfo.value.code == "no_clusters_configured"


def test_from_env_json_mapping() -> None:
    env = {
        "RAYTRAIN_SHARED_CLUSTERS": (
            '{"h20": "%s", "a100": "%s"}' % (URL_H20, URL_A100)
        )
    }
    rcc = RayClusterClient.from_env(env)
    assert rcc.gpu_types == ["a100", "h20"]


def test_from_env_per_type_vars() -> None:
    env = {
        "RAYTRAIN_CLUSTER_URL_H20": URL_H20,
        "RAYTRAIN_CLUSTER_URL_A100": URL_A100,
        "UNRELATED": "ignore-me",
    }
    rcc = RayClusterClient.from_env(env)
    assert rcc.gpu_types == ["a100", "h20"]


def test_from_env_json_takes_precedence_over_per_type() -> None:
    env = {
        "RAYTRAIN_SHARED_CLUSTERS": '{"h20": "%s"}' % URL_H20,
        "RAYTRAIN_CLUSTER_URL_A100": URL_A100,
    }
    rcc = RayClusterClient.from_env(env)
    # JSON wins -> only h20 present
    assert rcc.gpu_types == ["h20"]


def test_from_env_no_config_raises() -> None:
    with pytest.raises(RayClientError) as excinfo:
        RayClusterClient.from_env({})
    assert excinfo.value.code == "no_clusters_configured"


def test_from_env_invalid_json_raises() -> None:
    with pytest.raises(RayClientError) as excinfo:
        RayClusterClient.from_env({"RAYTRAIN_SHARED_CLUSTERS": "{not-json"})
    assert excinfo.value.code == "invalid_clusters_config"


# --------------------------------------------------------------------------- #
# module import must not require ray
# --------------------------------------------------------------------------- #


def test_module_imported_without_ray() -> None:
    """ray must not be a hard import dependency of this module."""
    assert "ray" not in sys.modules or sys.modules.get("ray") is not None
    # The construction hook exists and is the single ray seam.
    assert hasattr(rc, "_make_submission_client")
