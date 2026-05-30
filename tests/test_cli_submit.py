"""
Acceptance tests for `raytrain submit` (per_job dry-run) code-sync staging.

Covers task 2.3 of the long-term-evolution spec:
  - `--no-code-sync` → 4-stage output, rendered RayJob has NO `working_dir`.
  - default (code-sync on) → 5-stage output, rendered RayJob HAS `working_dir`
    and the fabricated dry-run `s3://` code_uri.

These run hermetically (no real cluster / MinIO / MLflow) by monkeypatching
`Manifest.load`, `UserConfig.load`, `create_run`, and `build_code_zip`.

Validates: Requirements 1.10, 2.1, 2.4
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
from raytrain.cli.submit import submit  # noqa: E402
from raytrain.code_sync import CodeBundle  # noqa: E402

from test_render import build_dummy  # noqa: E402


@pytest.fixture
def patched_submit(monkeypatch):
    """Make `submit` runnable without a real cluster / MinIO / MLflow.

    Returns the dummy (manifest, user_cfg) so tests can tweak them if needed.
    """
    manifest, user_cfg, _plan = build_dummy()
    # per_job dry-run path: ensure code_sync defaults to enabled.
    assert manifest.code_sync.enabled is True
    # ensure shared mode is not taken
    user_cfg.default_cluster_mode = "per_job"

    monkeypatch.setattr(submit_mod.Manifest, "load",
                        staticmethod(lambda *a, **k: manifest))
    monkeypatch.setattr(submit_mod.UserConfig, "load",
                        staticmethod(lambda *a, **k: user_cfg))
    monkeypatch.setattr(
        submit_mod, "create_run",
        lambda *a, **k: SimpleNamespace(
            run_id="deadbeefcafebabe1234567890abcdef",
            experiment_id="42",
        ),
    )

    # Fake packaging so we don't zip the whole repo and don't need a workdir.
    fake_bundle = CodeBundle(
        zip_path=Path("/tmp/raytrain-code-fake.zip"),
        sha256="a" * 64,
        size_bytes=12_345_678,
        file_count=7,
    )
    monkeypatch.setattr(submit_mod, "build_code_zip",
                        lambda *a, **k: fake_bundle)

    return manifest, user_cfg


def test_submit_dry_run_no_code_sync(patched_submit):
    """`--no-code-sync` → 4 stages, no `working_dir` in rendered RayJob."""
    runner = CliRunner()
    result = runner.invoke(
        submit,
        ["--dry-run", "--config", "configs/foo.py", "--no-code-sync"],
    )

    assert result.exit_code == 0, result.output
    # 4-stage staging: every stage tag is "/N]" with N=4, never "/5]".
    assert "/4]" in result.output, result.output
    assert "/5]" not in result.output, result.output
    # Legacy / image-baked path → no working_dir injected into the RayJob.
    assert "working_dir:" not in result.output, result.output


def test_submit_dry_run_with_code_sync(patched_submit):
    """Default (code-sync on) → 5 stages, `working_dir` + fabricated s3 URI."""
    _manifest, user_cfg = patched_submit
    runner = CliRunner()
    result = runner.invoke(
        submit,
        ["--dry-run", "--config", "configs/foo.py"],
    )

    assert result.exit_code == 0, result.output
    # 5-stage staging present.
    assert "/5]" in result.output, result.output
    # Code-sync path → working_dir injected into the rendered RayJob.
    assert "working_dir:" in result.output, result.output
    # Dry-run fabricates a stable s3:// code_uri using the code bucket.
    expected_prefix = f"s3://{_manifest.code_sync.bucket}/{user_cfg.user_name}/"
    assert expected_prefix in result.output, result.output


def test_dry_run_skips_artifact_upload(patched_submit, monkeypatch):
    """Dry-run returns before the apply step, so the manifest/plan artifact
    upload (task 11.2) must NOT run on a `--dry-run` submit."""
    calls = []
    monkeypatch.setattr(
        submit_mod, "_upload_manifest_plan_artifacts",
        lambda *a, **k: calls.append((a, k)),
    )

    runner = CliRunner()
    result = runner.invoke(submit, ["--dry-run", "--config", "configs/foo.py"])

    assert result.exit_code == 0, result.output
    assert calls == [], "dry-run must not upload manifest/plan artifacts"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
