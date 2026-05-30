"""
Unit tests for `_upload_manifest_plan_artifacts` (spec task 11.2).

After a per_job `raytrain submit` finishes applying the K8s objects, it
attaches the exact serialized manifest/plan to the pre-created MLflow run
(artifact_path `raytrain/`) for audit. These tests exercise that helper
directly — no real cluster / MLflow needed.

How `log_artifact` is patched
-----------------------------
`raytrain/cli/submit.py` imports the symbol directly:

    from ..mlflow_util import create_run, log_artifact

and the helper calls the bare name `log_artifact(...)`. So we monkeypatch the
name **as bound in the submit module**: `raytrain.cli.submit.log_artifact`.

Follows the ROOT sys.path.insert + monkeypatch patterns from
tests/test_cli_submit.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import raytrain.cli.submit as submit_mod  # noqa: E402
from raytrain.cli.submit import _upload_manifest_plan_artifacts  # noqa: E402


MANIFEST_YAML = "apiVersion: raytrain/v1\nimage: foo:latest\n"
PLAN_YAML = "job_name: demo-job\nrun_id: abc123\n"
RUN_ID = "deadbeefcafebabe1234567890abcdef"
TRACKING_URI = "http://mlflow.example:5000"


def test_uploads_manifest_and_plan(monkeypatch):
    """Helper writes manifest.yaml + plan.yaml and calls log_artifact twice
    with artifact_path='raytrain', the run_id, and matching file contents."""
    calls = []

    def fake_log_artifact(run_id, path, tracking_uri, artifact_path=None):
        # Capture the args + the file contents (file must still exist while the
        # temp dir is open — log_artifact is invoked inside the context mgr).
        calls.append(
            {
                "run_id": run_id,
                "path": path,
                "tracking_uri": tracking_uri,
                "artifact_path": artifact_path,
                "basename": Path(path).name,
                "content": Path(path).read_text(),
            }
        )

    monkeypatch.setattr(submit_mod, "log_artifact", fake_log_artifact)

    _upload_manifest_plan_artifacts(RUN_ID, MANIFEST_YAML, PLAN_YAML,
                                    TRACKING_URI)

    # Two uploads: manifest + plan.
    assert len(calls) == 2

    by_name = {c["basename"]: c for c in calls}
    assert set(by_name) == {"manifest.yaml", "plan.yaml"}

    for c in calls:
        assert c["run_id"] == RUN_ID
        assert c["tracking_uri"] == TRACKING_URI
        assert c["artifact_path"] == "raytrain"

    # Contents written to the temp files match the inputs exactly.
    assert by_name["manifest.yaml"]["content"] == MANIFEST_YAML
    assert by_name["plan.yaml"]["content"] == PLAN_YAML


def test_best_effort_swallows_errors(monkeypatch):
    """If log_artifact raises, the helper swallows it (best-effort) and does
    NOT propagate — so a successful submit is never failed by artifact upload."""
    def boom(*a, **k):
        raise RuntimeError("mlflow unreachable")

    monkeypatch.setattr(submit_mod, "log_artifact", boom)

    # Must not raise.
    _upload_manifest_plan_artifacts(RUN_ID, MANIFEST_YAML, PLAN_YAML,
                                    TRACKING_URI)


def test_temp_dir_cleaned_up(monkeypatch):
    """The temp files exist during upload but the temp dir is removed after."""
    seen_paths = []

    def fake_log_artifact(run_id, path, tracking_uri, artifact_path=None):
        seen_paths.append(path)
        assert Path(path).is_file()  # exists during the upload

    monkeypatch.setattr(submit_mod, "log_artifact", fake_log_artifact)

    _upload_manifest_plan_artifacts(RUN_ID, MANIFEST_YAML, PLAN_YAML,
                                    TRACKING_URI)

    # After return, the temp dir (and its files) are gone.
    assert seen_paths
    for p in seen_paths:
        assert not Path(p).exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
