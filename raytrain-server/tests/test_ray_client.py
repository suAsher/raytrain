"""Tests for raytrain_server.core.ray_client."""
from __future__ import annotations

import re

import pytest

from raytrain_server.core.ray_client import (
    JobSubmissionSpec,
    RayClusterClient,
    make_submission_id,
)
from raytrain_server.core.settings import Settings


# ---------------------------------------------------------------------------- #
# make_submission_id
# ---------------------------------------------------------------------------- #


class TestMakeSubmissionId:
    def test_dns_safe_format(self) -> None:
        sid = make_submission_id("zhangsan", "pointcept", "smoke")
        # lowercase, only [a-z0-9-], starts and ends with alnum
        assert re.match(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$", sid), sid
        assert sid.startswith("zhangsan-pointcept-smoke-")

    def test_special_chars_replaced(self) -> None:
        sid = make_submission_id("Foo Bar", "Repo/Name", "exp@1")
        assert "@" not in sid and " " not in sid and "/" not in sid

    def test_truncation_to_63_chars(self) -> None:
        sid = make_submission_id("u" * 30, "r" * 30, "e" * 30)
        assert len(sid) <= 63

    def test_blank_inputs_get_defaults(self) -> None:
        sid = make_submission_id("", "", "")
        assert sid.startswith("anon-repo-run-")


# ---------------------------------------------------------------------------- #
# RayClusterClient
# ---------------------------------------------------------------------------- #


class _FakeJobClient:
    """Stand-in for ray.job_submission.JobSubmissionClient."""

    def __init__(self, address: str) -> None:
        self.address = address
        self.submitted: list[dict] = []
        self.stopped: list[str] = []

    def submit_job(  # noqa: D401
        self,
        *,
        entrypoint: str,
        submission_id: str | None = None,
        runtime_env: dict | None = None,
        metadata: dict | None = None,
        entrypoint_num_cpus=None,
        entrypoint_num_gpus=None,
    ) -> str:
        self.submitted.append(
            {
                "entrypoint": entrypoint,
                "submission_id": submission_id,
                "runtime_env": runtime_env,
                "metadata": metadata,
            }
        )
        return submission_id or "fake-id"

    def stop_job(self, submission_id: str) -> bool:
        self.stopped.append(submission_id)
        return True

    def get_job_status(self, submission_id: str) -> str:
        return "RUNNING"

    def get_job_info(self, submission_id: str):
        class _I:
            status = "RUNNING"
            metadata = {"raytrain.user": "zhangsan"}

        return _I()

    def list_jobs(self) -> list:
        class _J:
            submission_id = "abc"
            status = "RUNNING"
            metadata = {"raytrain.user": "zhangsan"}

        return [_J()]

    def tail_job_logs(self, submission_id: str):  # noqa: D401
        yield f"line-1 for {submission_id}\n"
        yield "line-2\n"


@pytest.fixture
def fake_factory():
    created: list[_FakeJobClient] = []

    def factory(address: str) -> _FakeJobClient:
        c = _FakeJobClient(address)
        created.append(c)
        return c

    factory.created = created  # type: ignore[attr-defined]
    return factory


class TestRayClusterClient:
    def test_address_lookup(self, settings: Settings, fake_factory) -> None:
        cli = RayClusterClient(settings=settings, client_factory=fake_factory)
        assert cli.address_for("h20") == "http://ray-shared-h20:8265"

    def test_unknown_gpu_type_raises(self, settings: Settings, fake_factory) -> None:
        cli = RayClusterClient(settings=settings, client_factory=fake_factory)
        with pytest.raises(ValueError):
            cli.address_for("a100")

    def test_clients_are_cached_per_gpu_type(
        self, settings: Settings, fake_factory
    ) -> None:
        cli = RayClusterClient(settings=settings, client_factory=fake_factory)
        a = cli.get_client("h20")
        b = cli.get_client("h20")
        assert a is b
        assert len(fake_factory.created) == 1

    def test_build_runtime_env_with_code_uri(
        self, settings: Settings, fake_factory
    ) -> None:
        cli = RayClusterClient(settings=settings, client_factory=fake_factory)
        spec = JobSubmissionSpec(
            user="zhangsan",
            tenant="occ",
            gpu_type="h20",
            num_nodes=2,
            gpus_per_node=8,
            entrypoint="python tools/train.py",
            code_uri="s3://raytrain-code/zhangsan/foo.zip",
            code_hash="abc123",
            extra_env={"NCCL_DEBUG": "WARN"},
            extra_pip=["foo>=1.0"],
        )
        rt = cli.build_runtime_env(spec)
        assert rt["working_dir"] == "s3://raytrain-code/zhangsan/foo.zip"
        assert rt["pip"] == ["foo>=1.0"]
        ev = rt["env_vars"]
        assert ev["RAYTRAIN_USER"] == "zhangsan"
        assert ev["RAYTRAIN_TENANT"] == "occ"
        assert ev["RAYTRAIN_GPU_TYPE"] == "h20"
        assert ev["RAYTRAIN_NUM_NODES"] == "2"
        assert ev["RAYTRAIN_GPUS_PER_NODE"] == "8"
        assert ev["RAYTRAIN_CODE_URI"] == "s3://raytrain-code/zhangsan/foo.zip"
        assert ev["RAYTRAIN_CODE_HASH"] == "abc123"
        assert ev["NCCL_DEBUG"] == "WARN"
        assert ev["AWS_ENDPOINT_URL"] == "http://minio:9000"
        assert rt["config"]["setup_timeout_seconds"] == 600

    def test_build_runtime_env_without_code_uri(
        self, settings: Settings, fake_factory
    ) -> None:
        cli = RayClusterClient(settings=settings, client_factory=fake_factory)
        spec = JobSubmissionSpec(
            user="u",
            tenant="t",
            gpu_type="h20",
            num_nodes=1,
            gpus_per_node=1,
            entrypoint="python a.py",
        )
        rt = cli.build_runtime_env(spec)
        assert "working_dir" not in rt
        assert "RAYTRAIN_CODE_URI" not in rt["env_vars"]

    def test_user_env_overrides_defaults(
        self, settings: Settings, fake_factory
    ) -> None:
        """If the caller sets PYTHONUNBUFFERED to '0', user wins."""
        cli = RayClusterClient(settings=settings, client_factory=fake_factory)
        spec = JobSubmissionSpec(
            user="u",
            tenant="t",
            gpu_type="h20",
            num_nodes=1,
            gpus_per_node=1,
            entrypoint="python a.py",
            extra_env={"PYTHONUNBUFFERED": "0"},
        )
        rt = cli.build_runtime_env(spec)
        assert rt["env_vars"]["PYTHONUNBUFFERED"] == "0"

    def test_submit_job_forwards_to_underlying_client(
        self, settings: Settings, fake_factory
    ) -> None:
        cli = RayClusterClient(settings=settings, client_factory=fake_factory)
        spec = JobSubmissionSpec(
            user="zhangsan",
            tenant="occ",
            gpu_type="h20",
            num_nodes=1,
            gpus_per_node=8,
            entrypoint="python tools/train.py --foo bar",
            code_uri="s3://b/k.zip",
            code_hash="h",
        )
        sid = cli.submit_job(spec, submission_id="my-id-123", repo="pointcept")
        assert sid == "my-id-123"
        underlying: _FakeJobClient = fake_factory.created[0]
        assert len(underlying.submitted) == 1
        sub = underlying.submitted[0]
        assert sub["submission_id"] == "my-id-123"
        assert sub["entrypoint"] == "python tools/train.py --foo bar"
        assert sub["metadata"]["raytrain.user"] == "zhangsan"
        assert sub["metadata"]["raytrain.tenant"] == "occ"
        assert sub["metadata"]["raytrain.repo"] == "pointcept"
        assert sub["runtime_env"]["working_dir"] == "s3://b/k.zip"

    def test_stop_returns_true(self, settings: Settings, fake_factory) -> None:
        cli = RayClusterClient(settings=settings, client_factory=fake_factory)
        # Force client creation
        cli.get_client("h20")
        assert cli.stop("h20", "abc") is True
        assert fake_factory.created[0].stopped == ["abc"]

    def test_tail_logs_yields(self, settings: Settings, fake_factory) -> None:
        cli = RayClusterClient(settings=settings, client_factory=fake_factory)
        chunks = list(cli.tail_logs("h20", "abc"))
        assert chunks == ["line-1 for abc\n", "line-2\n"]
