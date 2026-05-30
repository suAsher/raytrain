"""
Mock-based acceptance tests for cluster-mode dispatch in the raytrain CLI's
`list` / `stop` / `logs` commands.

Covers task 8.4 of the long-term-evolution spec:
  Route `raytrain logs` / `raytrain stop` / `raytrain list` to the right
  backend based on `cluster_mode`; `list` merges both sources and prefixes
  rows with `[per-job]` / `[shared]`.

All tests are hermetic — no real cluster or Platform server is contacted:
  * `UserConfig.load` is monkeypatched on each command module to return a
    dummy config tuned per test.
  * SHARED-mode tests patch `raytrain.platform_client.PlatformClient` (the
    SOURCE module — the commands import it lazily via
    `from ..platform_client import PlatformClient, PlatformError`) with a fake
    context-manager, and wire the per_job K8s entry points to raise
    AssertionError if touched.
  * PER_JOB-mode tests patch the K8s entry points to fakes returning canned
    data, and patch `PlatformClient` to raise if ever constructed.

Validates: tests/test_cli_status_logs.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import raytrain.cli.status as status_mod  # noqa: E402
import raytrain.cli.logs as logs_mod  # noqa: E402
import raytrain.platform_client as platform_mod  # noqa: E402
from raytrain.cli.status import list_jobs, stop  # noqa: E402
from raytrain.cli.logs import logs as logs_cmd  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _user_cfg(*, submission_server="", token="", default_cluster_mode="per_job"):
    """Minimal stand-in for UserConfig used by these commands."""
    return SimpleNamespace(
        user_name="alice",
        namespace="ray-cluster-3",
        submission_server=submission_server,
        token=token,
        default_cluster_mode=default_cluster_mode,
    )


def _patch_user_cfg(monkeypatch, module, cfg):
    monkeypatch.setattr(module.UserConfig, "load",
                        staticmethod(lambda *a, **k: cfg))


def _fake_platform_client(monkeypatch, *, list_result=None, log_chunks=None,
                          calls=None):
    """Install a fake PlatformClient on the SOURCE module and return `calls`.

    `calls` records method invocations so tests can assert dispatch.
    """
    calls = calls if calls is not None else {}
    calls.setdefault("construct", 0)
    calls.setdefault("list_jobs", 0)
    calls.setdefault("stop_job", [])
    calls.setdefault("stream_logs", [])

    class FakePlatformClient:
        def __init__(self, base_url, token, *a, **k):
            calls["construct"] += 1
            calls["base_url"] = base_url
            calls["token"] = token

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def list_jobs(self, gpu_type):
            calls["list_jobs"] += 1
            calls["list_gpu_type"] = gpu_type
            return list_result or []

        def stop_job(self, submission_id, gpu_type):
            calls["stop_job"].append((submission_id, gpu_type))

        def stream_logs(self, submission_id, gpu_type):
            calls["stream_logs"].append((submission_id, gpu_type))
            for chunk in (log_chunks or []):
                yield chunk

    monkeypatch.setattr(platform_mod, "PlatformClient", FakePlatformClient)
    return calls


def _guard_k8s_status(monkeypatch):
    """Make every status-module K8s entry point fail loudly if touched."""
    def _no(*a, **k):
        raise AssertionError("K8s entry point called in shared mode")

    monkeypatch.setattr(status_mod, "load_kube", _no)
    monkeypatch.setattr(status_mod, "list_rayjobs", _no)
    monkeypatch.setattr(status_mod, "delete_rayjob", _no)


def _guard_k8s_logs(monkeypatch):
    def _no(*a, **k):
        raise AssertionError("K8s entry point called in shared mode (logs)")

    monkeypatch.setattr(logs_mod, "load_kube", _no)


def _guard_platform_not_constructed(monkeypatch):
    """Make constructing a PlatformClient blow up — per_job must never do it."""
    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("PlatformClient constructed in per_job mode")

    monkeypatch.setattr(platform_mod, "PlatformClient", Boom)


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #

def test_list_shared_uses_platform_not_k8s(monkeypatch):
    """`list --cluster-mode shared` hits PlatformClient.list_jobs, not K8s."""
    cfg = _user_cfg(submission_server="http://platform:8080", token="tok")
    _patch_user_cfg(monkeypatch, status_mod, cfg)

    SUBMISSION_ID = "alice-proj-exp-250101-000000"
    calls = _fake_platform_client(
        monkeypatch,
        list_result=[{
            "submission_id": SUBMISSION_ID,
            "status": "RUNNING",
            "metadata": {"raytrain.user": "alice"},
        }],
    )
    _guard_k8s_status(monkeypatch)

    result = CliRunner().invoke(
        list_jobs, ["--cluster-mode", "shared", "--gpu-type", "h20"])

    assert result.exit_code == 0, result.output
    assert calls["list_jobs"] == 1, calls
    assert calls["list_gpu_type"] == "h20"
    assert "[shared]" in result.output, result.output
    assert SUBMISSION_ID in result.output, result.output


def test_list_per_job_uses_k8s_not_platform(monkeypatch):
    """`list --cluster-mode per_job` hits list_rayjobs; PlatformClient unused.

    No submission_server/token configured, so the best-effort shared merge is
    skipped entirely (PlatformClient must never be constructed).
    """
    cfg = _user_cfg()  # no platform creds
    _patch_user_cfg(monkeypatch, status_mod, cfg)

    JOB_NAME = "alice-proj-exp-250101"
    list_calls = {"n": 0}

    def fake_list_rayjobs(ns, owner=None):
        list_calls["n"] += 1
        list_calls["ns"] = ns
        return [{
            "metadata": {
                "name": JOB_NAME,
                "labels": {"raytrain.owner": "alice",
                           "raytrain.gpu_type": "h20",
                           "raytrain.run_id": "deadbeef0000"},
                "annotations": {"raytrain.num_nodes": "2"},
                "creationTimestamp": "",
            },
            "status": {"jobStatus": "RUNNING"},
        }]

    monkeypatch.setattr(status_mod, "load_kube", lambda *a, **k: None)
    monkeypatch.setattr(status_mod, "list_rayjobs", fake_list_rayjobs)
    _guard_platform_not_constructed(monkeypatch)

    result = CliRunner().invoke(list_jobs, ["--cluster-mode", "per_job"])

    assert result.exit_code == 0, result.output
    assert list_calls["n"] == 1, list_calls
    assert "[per-job]" in result.output, result.output
    assert JOB_NAME in result.output, result.output


def test_list_merges_both_sources(monkeypatch):
    """per_job mode with platform creds set merges both sources in one table.

    The best-effort shared listing runs, so output carries BOTH a `[per-job]`
    row (from K8s) and a `[shared]` row (from PlatformClient.list_jobs).
    """
    cfg = _user_cfg(submission_server="http://platform:8080", token="tok")
    _patch_user_cfg(monkeypatch, status_mod, cfg)

    PER_JOB_NAME = "alice-perjob-250101"
    SHARED_ID = "alice-shared-250101-000000"

    def fake_list_rayjobs(ns, owner=None):
        return [{
            "metadata": {
                "name": PER_JOB_NAME,
                "labels": {"raytrain.owner": "alice",
                           "raytrain.gpu_type": "h20",
                           "raytrain.run_id": "abcd0000"},
                "annotations": {"raytrain.num_nodes": "1"},
                "creationTimestamp": "",
            },
            "status": {"jobStatus": "RUNNING"},
        }]

    monkeypatch.setattr(status_mod, "load_kube", lambda *a, **k: None)
    monkeypatch.setattr(status_mod, "list_rayjobs", fake_list_rayjobs)
    calls = _fake_platform_client(
        monkeypatch,
        list_result=[{
            "submission_id": SHARED_ID,
            "status": "RUNNING",
            "metadata": {"raytrain.user": "alice"},
        }],
    )

    result = CliRunner().invoke(list_jobs, ["--cluster-mode", "per_job"])

    assert result.exit_code == 0, result.output
    assert calls["list_jobs"] == 1, calls
    assert "[per-job]" in result.output, result.output
    assert "[shared]" in result.output, result.output
    assert PER_JOB_NAME in result.output, result.output
    assert SHARED_ID in result.output, result.output


def test_list_merge_is_best_effort(monkeypatch):
    """A failing shared listing must not break the per_job table."""
    cfg = _user_cfg(submission_server="http://platform:8080", token="tok")
    _patch_user_cfg(monkeypatch, status_mod, cfg)

    PER_JOB_NAME = "alice-perjob-250101"

    def fake_list_rayjobs(ns, owner=None):
        return [{
            "metadata": {
                "name": PER_JOB_NAME,
                "labels": {"raytrain.owner": "alice"},
                "annotations": {},
                "creationTimestamp": "",
            },
            "status": {"jobStatus": "RUNNING"},
        }]

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("platform unreachable")

    monkeypatch.setattr(status_mod, "load_kube", lambda *a, **k: None)
    monkeypatch.setattr(status_mod, "list_rayjobs", fake_list_rayjobs)
    monkeypatch.setattr(platform_mod, "PlatformClient", Boom)

    result = CliRunner().invoke(list_jobs, ["--cluster-mode", "per_job"])

    assert result.exit_code == 0, result.output
    assert "[per-job]" in result.output, result.output
    assert PER_JOB_NAME in result.output, result.output
    assert "[shared]" not in result.output, result.output


# --------------------------------------------------------------------------- #
# stop
# --------------------------------------------------------------------------- #

def test_stop_shared_calls_platform(monkeypatch):
    """`stop <id> --cluster-mode shared --yes` calls pc.stop_job(id, gpu)."""
    cfg = _user_cfg(submission_server="http://platform:8080", token="tok")
    _patch_user_cfg(monkeypatch, status_mod, cfg)

    SUBMISSION_ID = "alice-proj-exp-250101-000000"
    calls = _fake_platform_client(monkeypatch)
    _guard_k8s_status(monkeypatch)

    result = CliRunner().invoke(
        stop, [SUBMISSION_ID, "--cluster-mode", "shared",
               "--gpu-type", "a100", "--yes"])

    assert result.exit_code == 0, result.output
    assert calls["stop_job"] == [(SUBMISSION_ID, "a100")], calls
    assert "stopped" in result.output, result.output


def test_stop_per_job_calls_k8s(monkeypatch):
    """`stop <name> --cluster-mode per_job --yes` calls delete_rayjob(name, ns)."""
    cfg = _user_cfg()
    _patch_user_cfg(monkeypatch, status_mod, cfg)

    JOB_NAME = "alice-proj-exp-250101"
    delete_calls = []

    monkeypatch.setattr(status_mod, "load_kube", lambda *a, **k: None)
    monkeypatch.setattr(status_mod, "delete_rayjob",
                        lambda name, ns: delete_calls.append((name, ns)))
    _guard_platform_not_constructed(monkeypatch)

    result = CliRunner().invoke(
        stop, [JOB_NAME, "--cluster-mode", "per_job", "--yes"])

    assert result.exit_code == 0, result.output
    assert delete_calls == [(JOB_NAME, "ray-cluster-3")], delete_calls
    assert "deleted" in result.output, result.output


# --------------------------------------------------------------------------- #
# logs
# --------------------------------------------------------------------------- #

def test_logs_shared_streams_from_platform(monkeypatch):
    """`logs <id> --cluster-mode shared` iterates pc.stream_logs chunks."""
    cfg = _user_cfg(submission_server="http://platform:8080", token="tok")
    _patch_user_cfg(monkeypatch, logs_mod, cfg)

    SUBMISSION_ID = "alice-proj-exp-250101-000000"
    chunks = ["hello from ", "shared cluster\n", "[node0] training...\n"]
    calls = _fake_platform_client(monkeypatch, log_chunks=chunks)
    _guard_k8s_logs(monkeypatch)

    result = CliRunner().invoke(
        logs_cmd, [SUBMISSION_ID, "--cluster-mode", "shared",
                   "--gpu-type", "h20"])

    assert result.exit_code == 0, result.output
    assert calls["stream_logs"] == [(SUBMISSION_ID, "h20")], calls
    for chunk in chunks:
        assert chunk in result.output, result.output


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
