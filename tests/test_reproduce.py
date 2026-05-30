"""
Acceptance tests for `raytrain reproduce <mlflow_run_id>` (spec task 11.1).

These run hermetically (no real MLflow / MinIO) by monkeypatching the two
module-level seams in `raytrain.cli.reproduce`:

  - `_get_run_tags`        -> returns canned tags (code_uri / code_hash)
  - `_make_minio_client`   -> returns a fake client whose `fget_object` writes
                              a real little zip to the destination path
  - `UserConfig.load`      -> a dummy in-memory config

Tests use `--dest tmp_path` so nothing is written to the real `/tmp` (the
default base stays `/tmp` per the spec).

Covers:
  - test_reproduce_happy_path     : tags present, zip downloaded + unzipped
  - test_reproduce_missing_code_uri: no code_uri tag -> friendly ClickException
  - test_reproduce_object_expired : fget_object raises NoSuchKey -> expired msg
                                     + no leftover partial file
  - test_reproduce_no_unzip       : --no-unzip leaves only the .zip
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from minio.error import S3Error  # noqa: E402

import raytrain.cli.reproduce as repro_mod  # noqa: E402
from raytrain.cli.reproduce import reproduce  # noqa: E402


CODE_HASH = "a" * 64
CODE_URI = "s3://raytrain-code/u/job.zip"


def _dummy_cfg():
    """A minimal UserConfig-shaped object good enough for the seams."""
    return SimpleNamespace(
        mlflow=SimpleNamespace(
            tracking_uri="http://mlflow.local:5000",
            username="",
            password="",
        ),
        minio=SimpleNamespace(
            endpoint="http://minio.local:9000",
            access_key="ak",
            secret_key="sk",
            secure=False,
            region="us-east-1",
        ),
    )


def _make_fake_zip_client(files: dict[str, str]):
    """Return a fake minio client whose `fget_object` writes a real zip.

    `files` maps arcname -> file content. The zip is written to the `dst`
    path that the command computed, so extraction produces real files.
    """
    class _FakeClient:
        def __init__(self):
            self.calls = []

        def fget_object(self, bucket, key, dst):
            self.calls.append((bucket, key, dst))
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
                for arcname, content in files.items():
                    zf.writestr(arcname, content)

    return _FakeClient()


@pytest.fixture
def patched(monkeypatch):
    """Patch UserConfig.load + the two reproduce seams. Returns a control dict.

    Defaults to a happy-path tag set + a fake client that writes a 2-file zip.
    Individual tests override `state["tags"]` or `state["client"]`.
    """
    state = {
        "tags": {
            repro_mod.TAG_CODE_URI: CODE_URI,
            repro_mod.TAG_CODE_HASH: CODE_HASH,
            repro_mod.TAG_CODE_SIZE: "123",
        },
        "client": _make_fake_zip_client(
            {"train.py": "print('hi')\n", "configs/foo.py": "x = 1\n"}
        ),
    }

    monkeypatch.setattr(repro_mod.UserConfig, "load",
                        staticmethod(lambda *a, **k: _dummy_cfg()))
    monkeypatch.setattr(repro_mod, "_get_run_tags",
                        lambda run_id, user_cfg: state["tags"])
    monkeypatch.setattr(repro_mod, "_make_minio_client",
                        lambda user_cfg: state["client"])
    return state


def test_reproduce_happy_path(patched, tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        reproduce, ["run-123", "--dest", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output

    dest = tmp_path / f"raytrain-reproduce-{CODE_HASH[:12]}"
    assert dest.is_dir(), result.output

    zip_path = dest / "job.zip"
    assert zip_path.is_file(), result.output

    # default unzips: extracted files present
    assert (dest / "train.py").is_file(), result.output
    assert (dest / "configs" / "foo.py").is_file(), result.output
    assert (dest / "train.py").read_text() == "print('hi')\n"

    # summary mentions the key facts
    assert "run-123" in result.output
    assert CODE_URI in result.output
    assert CODE_HASH in result.output


def test_reproduce_missing_code_uri(patched, tmp_path):
    patched["tags"] = {}  # no code_uri tag

    runner = CliRunner()
    result = runner.invoke(
        reproduce, ["run-456", "--dest", str(tmp_path)]
    )

    assert result.exit_code != 0
    assert repro_mod.TAG_CODE_URI in result.output
    assert "git" in result.output.lower()


def test_reproduce_object_expired(patched, tmp_path):
    class _ExpiredClient:
        def fget_object(self, bucket, key, dst):
            # simulate a partial write before failing...
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_bytes(b"partial")
            raise S3Error(
                code="NoSuchKey",
                message="The specified key does not exist.",
                resource=f"/{bucket}/{key}",
                request_id="rid",
                host_id="hid",
                response=None,
            )

    patched["client"] = _ExpiredClient()

    runner = CliRunner()
    result = runner.invoke(
        reproduce, ["run-789", "--dest", str(tmp_path)]
    )

    assert result.exit_code != 0
    out = result.output.lower()
    assert "expired" in out
    assert "7-day" in out or "lifecycle" in out

    # no leftover partial file
    dest = tmp_path / f"raytrain-reproduce-{CODE_HASH[:12]}"
    zip_path = dest / "job.zip"
    assert not zip_path.exists(), "partial file must be cleaned up"


def test_reproduce_no_unzip(patched, tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        reproduce, ["run-123", "--dest", str(tmp_path), "--no-unzip"]
    )

    assert result.exit_code == 0, result.output

    dest = tmp_path / f"raytrain-reproduce-{CODE_HASH[:12]}"
    zip_path = dest / "job.zip"
    assert zip_path.is_file(), result.output

    # NOT extracted
    assert not (dest / "train.py").exists(), result.output
    assert not (dest / "configs").exists(), result.output


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
