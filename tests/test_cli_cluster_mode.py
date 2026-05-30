"""
Acceptance tests for cluster-mode resolution precedence in `raytrain submit`.

Covers task 8.2 of the long-term-evolution spec:
  Resolution order (highest first):
    1. CLI flag `--cluster-mode`
    2. namespace ConfigMap `raytrain-defaults` (key `default_cluster_mode`)
    3. UserConfig.default_cluster_mode
    4. final fallback "per_job"

These run hermetically (no real cluster) by monkeypatching the tiny
`_configmap_cluster_mode` reader so the kubernetes client is never touched.

Validates: tests/test_cli_cluster_mode.py::test_priority_order
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import raytrain.cli.submit as submit_mod  # noqa: E402
import raytrain.platform_client as platform_mod  # noqa: E402
from raytrain.cli.submit import _resolve_cluster_mode, submit  # noqa: E402
from raytrain.code_sync import CodeBundle  # noqa: E402

from test_render import build_dummy  # noqa: E402


def _user_cfg(default_cluster_mode):
    """Minimal stand-in for UserConfig: the resolver only reads
    `default_cluster_mode` and is handed `namespace` separately."""
    return SimpleNamespace(default_cluster_mode=default_cluster_mode)


def test_priority_order(monkeypatch):
    """Prove the four-tier precedence: CLI > ConfigMap > UserConfig > per_job."""

    # A controllable stub for the ConfigMap tier. We also record how many
    # times it was invoked so we can assert the CLI value short-circuits it.
    calls = {"n": 0}

    def fake_configmap(value):
        def _reader(namespace):
            calls["n"] += 1
            return value
        return _reader

    # 1. CLI wins over everything (configmap=per_job, user=per_job → shared).
    monkeypatch.setattr(submit_mod, "_configmap_cluster_mode",
                        fake_configmap("per_job"))
    assert _resolve_cluster_mode("shared", _user_cfg("per_job"), "ns") == "shared"

    # 2. CLI=None → ConfigMap wins over UserConfig.
    monkeypatch.setattr(submit_mod, "_configmap_cluster_mode",
                        fake_configmap("shared"))
    assert _resolve_cluster_mode(None, _user_cfg("per_job"), "ns") == "shared"

    # 3. CLI=None, ConfigMap=None → UserConfig wins.
    monkeypatch.setattr(submit_mod, "_configmap_cluster_mode",
                        fake_configmap(None))
    assert _resolve_cluster_mode(None, _user_cfg("shared"), "ns") == "shared"

    # 4. CLI=None, ConfigMap=None, user empty → final fallback "per_job".
    monkeypatch.setattr(submit_mod, "_configmap_cluster_mode",
                        fake_configmap(None))
    assert _resolve_cluster_mode(None, _user_cfg(""), "ns") == "per_job"

    # 4b. invalid user value → defensive fallback to "per_job".
    monkeypatch.setattr(submit_mod, "_configmap_cluster_mode",
                        fake_configmap(None))
    assert _resolve_cluster_mode(None, _user_cfg("bogus"), "ns") == "per_job"


def test_cli_value_short_circuits_configmap(monkeypatch):
    """A CLI value must NOT trigger the ConfigMap reader at all.

    This guarantees `--cluster-mode shared` never forces a kube connection in
    environments without kubeconfig.
    """
    calls = {"n": 0}

    def _reader(namespace):
        calls["n"] += 1
        return "per_job"

    monkeypatch.setattr(submit_mod, "_configmap_cluster_mode", _reader)

    assert _resolve_cluster_mode("shared", _user_cfg("per_job"), "ns") == "shared"
    assert calls["n"] == 0, "ConfigMap reader must not be called when CLI provides a value"

    # Sanity: when CLI is None, the reader IS consulted.
    assert _resolve_cluster_mode(None, _user_cfg("per_job"), "ns") == "per_job"
    assert calls["n"] == 1


def test_configmap_reader_is_best_effort(monkeypatch):
    """`_configmap_cluster_mode` must never raise, even if kube access fails.

    We simulate a kube failure by making `load_kube` raise; the function should
    swallow it and return None so resolution falls through to the next tier.
    """
    def boom():
        raise RuntimeError("no kubeconfig available")

    # Patch the kube loader imported lazily inside the reader.
    import raytrain.kube as kube_mod
    monkeypatch.setattr(kube_mod, "load_kube", boom)

    # Must not raise; returns None (no value at this tier).
    assert submit_mod._configmap_cluster_mode("ns") is None

    # And the full resolver still works, falling back to user config.
    assert _resolve_cluster_mode(None, _user_cfg("shared"), "ns") == "shared"


# --------------------------------------------------------------------------- #
# task 8.3: shared-mode submit branch — full `submit` command, no K8s.
# --------------------------------------------------------------------------- #


def test_shared_submit(monkeypatch):
    """Drive the full `submit` Click command in SHARED mode end-to-end.

    Proves the shared branch:
      * resolves to `_submit_shared` (via `--cluster-mode shared`),
      * packages + uploads code and calls `PlatformClient.submit_job` exactly
        once, surfacing the returned `submission_id` in the CLI output,
      * prints the staged `[n/total]` progress (4 stages with code-sync on),
      * never touches any K8s API.

    Hermetic: Manifest/UserConfig loads, code packaging, and the PlatformClient
    HTTP layer are all stubbed; the per_job K8s entry points are monkeypatched
    to fail loudly if the code path ever reaches them.
    """
    manifest, user_cfg, _plan = build_dummy()
    # shared mode requires a platform endpoint + token; code-sync must be on so
    # we exercise the package -> upload -> submit path (4 stages).
    assert manifest.code_sync.enabled is True
    user_cfg.submission_server = "http://platform:8080"
    user_cfg.token = "tok"
    # default can be anything since we pass --cluster-mode shared explicitly.
    user_cfg.default_cluster_mode = "per_job"

    monkeypatch.setattr(submit_mod.Manifest, "load",
                        staticmethod(lambda *a, **k: manifest))
    monkeypatch.setattr(submit_mod.UserConfig, "load",
                        staticmethod(lambda *a, **k: user_cfg))

    # Fake packaging so we don't zip the repo / need a real workdir.
    fake_bundle = CodeBundle(
        zip_path=Path("/tmp/raytrain-code-fake.zip"),
        sha256="a" * 64,
        size_bytes=12_345_678,
        file_count=7,
    )
    monkeypatch.setattr(submit_mod, "build_code_zip",
                        lambda *a, **k: fake_bundle)

    # --- fail-if-called guards on the per_job (K8s) entry points -----------
    # `_submit_shared` must never load kubeconfig or apply YAML. If shared mode
    # accidentally fell through to the per_job path, these would trip and the
    # test would fail with a clear message.
    k8s_calls = {"load_kube": 0, "apply_yaml_docs": 0}

    def _no_kube(*a, **k):
        k8s_calls["load_kube"] += 1
        raise AssertionError("load_kube() called in shared mode (touched K8s!)")

    def _no_apply(*a, **k):
        k8s_calls["apply_yaml_docs"] += 1
        raise AssertionError("apply_yaml_docs() called in shared mode (touched K8s!)")

    monkeypatch.setattr(submit_mod, "load_kube", _no_kube)
    monkeypatch.setattr(submit_mod, "apply_yaml_docs", _no_apply)

    # --- fake PlatformClient (patched on the SOURCE module) ----------------
    # `_submit_shared` does `from ..platform_client import PlatformClient` at
    # call time, which binds the name from `raytrain.platform_client`. So we
    # patch it there, not as a submit-module attribute.
    SUBMISSION_ID = "alice-proj-exp-250101-000000"
    calls = {"upload_code": 0, "submit_job": 0}

    class FakePlatformClient:
        def __init__(self, base_url, token, *a, **k):
            # record construction args so we know real creds flowed through
            self.base_url = base_url
            self.token = token

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def upload_code(self, zip_path, sha256, job_name):
            calls["upload_code"] += 1
            return {"code_uri": "s3://raytrain-code/alice/job.zip",
                    "sha256": "a" * 64}

        def submit_job(self, **kwargs):
            calls["submit_job"] += 1
            return {
                "submission_id": SUBMISSION_ID,
                "cluster_address": "http://ray-shared-h20-head:8265",
            }

    monkeypatch.setattr(platform_mod, "PlatformClient", FakePlatformClient)

    # --- invoke the real Click command (NOT dry-run) -----------------------
    runner = CliRunner()
    result = runner.invoke(
        submit,
        ["--config", "configs/foo.py", "--gpus", "8", "--nodes", "1",
         "--gpu-type", "h20", "--cluster-mode", "shared", "--name", "exp"],
    )

    assert result.exit_code == 0, result.output

    # submit_job called exactly once and its submission_id surfaced.
    assert calls["submit_job"] == 1, (calls, result.output)
    assert calls["upload_code"] == 1, (calls, result.output)
    assert SUBMISSION_ID in result.output, result.output

    # staged progress: shared path prints `[n/total]` markers ending in `/4]`.
    assert "/4]" in result.output, result.output
    assert "submitted" in result.output, result.output

    # explicit no-K8s assertions (exit_code 0 already implies the guards never
    # raised, but be explicit about intent).
    assert k8s_calls["load_kube"] == 0, "shared mode must not load kubeconfig"
    assert k8s_calls["apply_yaml_docs"] == 0, "shared mode must not apply K8s YAML"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
