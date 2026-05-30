"""
Acceptance tests for `raytrain configure` (spec task 9.2).

Task 9.2 requires that `raytrain configure`:
  * guides the user to fill `submission_server` + `token`, and
  * does NOT require a local kubeconfig (kube access only happens in
    cluster_mode=per_job, exercised by `raytrain submit`).

These tests drive the real Click command via CliRunner, passing every prompt
as a flag so the run is non-interactive. The written config is redirected to a
tmp path by monkeypatching `raytrain.user_config.DEFAULT_PATH` so the real
`~/.raytrain/config.yaml` is never touched. A fail-if-called guard is installed
on `load_kube` to prove configure never reaches into Kubernetes.

Validates: tests/test_configure.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import raytrain.kube as kube_mod  # noqa: E402
import raytrain.user_config as user_config_mod  # noqa: E402
from raytrain.cli.configure import configure  # noqa: E402
from raytrain.user_config import UserConfig  # noqa: E402


def _install_guards(monkeypatch, tmp_path):
    """Redirect config writes to tmp and make any kube access fail loudly.

    Returns the tmp config path the command will write to.
    """
    cfg_path = tmp_path / "config.yaml"
    # `UserConfig.save` resolves `DEFAULT_PATH` from the user_config module at
    # call time, so patching it there reroutes the write without touching $HOME.
    monkeypatch.setattr(user_config_mod, "DEFAULT_PATH", cfg_path)

    # configure must NEVER load kubeconfig. If it ever did, this trips.
    def _no_kube(*a, **k):
        raise AssertionError("load_kube() called during `raytrain configure`")

    monkeypatch.setattr(kube_mod, "load_kube", _no_kube)
    return cfg_path


# Common flags so each test only overrides what it cares about.
_BASE_FLAGS = [
    "--user", "alice",
    "--namespace", "ray-cluster-3",
    "--minio-endpoint", "http://minio:9000",
    "--minio-access-key", "AK",
    "--minio-secret-key", "SK",
    "--mlflow-uri", "http://mlflow:5000",
    "--mlflow-user", "",
    "--mlflow-password", "",
]


def test_configure_shared_mode_writes_server_and_token(monkeypatch, tmp_path):
    """Shared mode: server + token are persisted, and no kube access occurs."""
    cfg_path = _install_guards(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        configure,
        _BASE_FLAGS + [
            "--cluster-mode", "shared",
            "--submission-server", "http://platform:30810",
            "--token", "jwt-token-123",
        ],
    )

    assert result.exit_code == 0, result.output
    assert cfg_path.exists(), "configure should have written the config file"

    raw = yaml.safe_load(cfg_path.read_text())
    assert raw["submission_server"] == "http://platform:30810"
    assert raw["token"] == "jwt-token-123"
    assert raw["default_cluster_mode"] == "shared"
    assert raw["user_name"] == "alice"

    # round-trips through the real loader too
    loaded = UserConfig.load(cfg_path)
    assert loaded.submission_server == "http://platform:30810"
    assert loaded.token == "jwt-token-123"
    assert loaded.default_cluster_mode == "shared"

    # shared mode with creds present prints the "no kubeconfig required" note
    assert "no local kubeconfig" in result.output


def test_configure_does_not_require_kubeconfig_per_job(monkeypatch, tmp_path):
    """per_job mode still writes config without any kubeconfig access.

    The `load_kube` guard raises if touched; reaching exit_code 0 proves
    configure never loads kubeconfig regardless of cluster mode.
    """
    cfg_path = _install_guards(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        configure,
        _BASE_FLAGS + [
            "--cluster-mode", "per_job",
            "--submission-server", "",
            "--token", "",
        ],
    )

    assert result.exit_code == 0, result.output
    assert cfg_path.exists()

    raw = yaml.safe_load(cfg_path.read_text())
    assert raw["default_cluster_mode"] == "per_job"
    # per_job mode prints the kubeconfig-required note (informational only).
    assert "kubeconfig" in result.output


def test_configure_shared_mode_warns_when_creds_missing(monkeypatch, tmp_path):
    """Shared mode with empty server/token still saves but warns the user."""
    cfg_path = _install_guards(monkeypatch, tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        configure,
        _BASE_FLAGS + [
            "--cluster-mode", "shared",
            "--submission-server", "",
            "--token", "",
        ],
    )

    assert result.exit_code == 0, result.output
    assert cfg_path.exists()

    raw = yaml.safe_load(cfg_path.read_text())
    assert raw["default_cluster_mode"] == "shared"
    assert raw["submission_server"] == ""
    assert raw["token"] == ""

    # the UX reminder fires so the user knows submit will fail until set
    assert "warning" in result.output.lower()
    assert "submission_server" in result.output


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
