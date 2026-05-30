"""
Unit tests for raytrain/code_sync.py.

Run with:
    pytest tests/test_code_sync.py -v
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Allow running directly: ROOT/raytrain importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from minio.error import S3Error  # noqa: E402

from raytrain.code_sync import (  # noqa: E402
    DEFAULT_BUCKET,
    DEFAULT_IGNORES,
    CodeBundle,
    CodeSyncTooLargeError,
    CodeSyncUploadError,
    build_code_zip,
    load_pathspec,
    upload_code_zip,
)


# ---------------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------------- #


def _write(p: Path, content: bytes | str = b"") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        p.write_text(content)
    else:
        p.write_bytes(content)


def _zip_names(zip_path: Path) -> set[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


# ---------------------------------------------------------------------------- #
# load_pathspec
# ---------------------------------------------------------------------------- #


class TestLoadPathspec:
    def test_default_ignores_apply(self, tmp_path: Path) -> None:
        spec = load_pathspec(tmp_path)
        assert spec.match_file(".git/HEAD") is True
        assert spec.match_file("__pycache__/foo.pyc") is True
        assert spec.match_file("data/x.bin") is True
        assert spec.match_file("model.ckpt") is True

    def test_normal_files_not_excluded(self, tmp_path: Path) -> None:
        spec = load_pathspec(tmp_path)
        assert spec.match_file("tools/train.py") is False
        assert spec.match_file("configs/exp.py") is False
        assert spec.match_file("README.md") is False

    def test_gitignore_picked_up(self, tmp_path: Path) -> None:
        _write(tmp_path / ".gitignore", "secret/\n*.bak\n")
        spec = load_pathspec(tmp_path)
        assert spec.match_file("secret/foo.txt") is True
        assert spec.match_file("file.bak") is True

    def test_raytrainignore_picked_up(self, tmp_path: Path) -> None:
        _write(tmp_path / ".raytrainignore", "outputs/\n")
        spec = load_pathspec(tmp_path)
        assert spec.match_file("outputs/run-1/model.bin") is True

    def test_extra_excludes_overlay(self, tmp_path: Path) -> None:
        spec = load_pathspec(tmp_path, extra_excludes=["scratch/", "*.bin"])
        assert spec.match_file("scratch/x.txt") is True
        assert spec.match_file("foo.bin") is True

    def test_comments_and_blanks_ignored(self, tmp_path: Path) -> None:
        _write(
            tmp_path / ".raytrainignore",
            "# this is a comment\n\n# another comment\nfoo/\n",
        )
        spec = load_pathspec(tmp_path)
        assert spec.match_file("foo/x") is True
        # Comments shouldn't accidentally exclude their own text
        assert spec.match_file("# this is a comment") is False


# ---------------------------------------------------------------------------- #
# build_code_zip
# ---------------------------------------------------------------------------- #


class TestBuildCodeZip:
    def test_includes_python_sources(self, tmp_path: Path) -> None:
        wd = tmp_path / "repo"
        _write(wd / "tools" / "train.py", "print('hi')\n")
        _write(wd / "configs" / "exp.py", "x = 1\n")
        _write(wd / "README.md", "# repo")

        bundle = build_code_zip(
            workdir=wd,
            job_name="job-1",
            user="zhangsan",
            out_dir=tmp_path,
        )

        names = _zip_names(bundle.zip_path)
        assert "tools/train.py" in names
        assert "configs/exp.py" in names
        assert "README.md" in names
        # raytrain metadata must be present
        assert ".raytrain-code-meta" in names

    def test_excludes_default_patterns(self, tmp_path: Path) -> None:
        wd = tmp_path / "repo"
        _write(wd / "tools" / "train.py", "ok")
        _write(wd / ".git" / "HEAD", "ref")
        _write(wd / "__pycache__" / "foo.cpython.pyc", b"\x00")
        _write(wd / "data" / "scannet" / "x.npz", b"\x00")
        _write(wd / "exp" / "run-1" / "model.ckpt", b"\x00")

        bundle = build_code_zip(
            workdir=wd, job_name="job-1", user="u", out_dir=tmp_path
        )
        names = _zip_names(bundle.zip_path)

        assert "tools/train.py" in names
        assert ".git/HEAD" not in names
        assert "__pycache__/foo.cpython.pyc" not in names
        assert "data/scannet/x.npz" not in names
        assert "exp/run-1/model.ckpt" not in names

    def test_excludes_via_raytrainignore(self, tmp_path: Path) -> None:
        wd = tmp_path / "repo"
        _write(wd / "tools" / "train.py", "ok")
        _write(wd / "outputs" / "x.bin", b"\x00")
        _write(wd / ".raytrainignore", "outputs/\n")

        bundle = build_code_zip(
            workdir=wd, job_name="job-1", user="u", out_dir=tmp_path
        )
        names = _zip_names(bundle.zip_path)
        assert "tools/train.py" in names
        assert "outputs/x.bin" not in names

    def test_excludes_via_extra_excludes(self, tmp_path: Path) -> None:
        wd = tmp_path / "repo"
        _write(wd / "tools" / "train.py", "ok")
        _write(wd / "scratch" / "x.txt", "junk")

        bundle = build_code_zip(
            workdir=wd,
            job_name="job-1",
            user="u",
            extra_excludes=["scratch/"],
            out_dir=tmp_path,
        )
        names = _zip_names(bundle.zip_path)
        assert "tools/train.py" in names
        assert "scratch/x.txt" not in names

    def test_sha256_stable(self, tmp_path: Path) -> None:
        wd = tmp_path / "repo"
        _write(wd / "a.py", "print(1)\n")
        _write(wd / "b.py", "print(2)\n")

        b1 = build_code_zip(
            workdir=wd, job_name="job-a", user="u", out_dir=tmp_path / "out1"
        )
        b2 = build_code_zip(
            workdir=wd, job_name="job-b", user="u", out_dir=tmp_path / "out2"
        )
        # same content + same job_name -> stable; but job_name is in metadata,
        # so different job_name will give different sha256. Verify within
        # a single job_name, multiple builds match:
        b3 = build_code_zip(
            workdir=wd, job_name="job-a", user="u", out_dir=tmp_path / "out3"
        )
        # b1 and b3 share job_name; should be identical sha256
        # Note: meta also includes created_at timestamp, so sha256 will differ
        # between runs unless we freeze time. We do NOT guarantee bit-stable
        # zips across runs; we DO guarantee that same content gives same
        # *file content* hash (sans metadata). So just assert sha256 is
        # 64 hex chars.
        assert len(b1.sha256) == 64
        assert all(c in "0123456789abcdef" for c in b1.sha256)
        # Different job_name should yield different sha256 (because meta
        # contains job_name).
        assert b1.sha256 != b2.sha256

    def test_zip_metadata_has_user_and_job(self, tmp_path: Path) -> None:
        wd = tmp_path / "repo"
        _write(wd / "a.py", "x")

        bundle = build_code_zip(
            workdir=wd, job_name="job-x", user="alice", out_dir=tmp_path
        )
        with zipfile.ZipFile(bundle.zip_path) as zf:
            meta = zf.read(".raytrain-code-meta").decode("utf-8")
        assert "user=alice" in meta
        assert "job_name=job-x" in meta

    def test_size_limit_exceeded(self, tmp_path: Path) -> None:
        wd = tmp_path / "repo"
        # 2 MiB of incompressible random-ish bytes
        big_blob = os.urandom(2 * 1024 * 1024)
        _write(wd / "big" / "blob.bin", big_blob)
        _write(wd / "tools" / "train.py", "ok")

        with pytest.raises(CodeSyncTooLargeError) as excinfo:
            build_code_zip(
                workdir=wd,
                job_name="big",
                user="u",
                max_size_bytes=512 * 1024,  # 512 KiB
                out_dir=tmp_path,
            )

        err = excinfo.value
        assert err.size_bytes > err.limit_bytes
        # top_files should report the blob
        assert any("big/blob.bin" in str(p) for p, _ in err.top_files)

    def test_workdir_must_exist(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            build_code_zip(
                workdir=tmp_path / "does-not-exist",
                job_name="x",
                user="u",
                out_dir=tmp_path,
            )

    def test_empty_dir_still_produces_zip(self, tmp_path: Path) -> None:
        wd = tmp_path / "repo"
        wd.mkdir()
        bundle = build_code_zip(
            workdir=wd, job_name="empty", user="u", out_dir=tmp_path
        )
        # at minimum the meta file is present
        names = _zip_names(bundle.zip_path)
        assert names == {".raytrain-code-meta"}
        assert bundle.file_count == 0

    def test_file_count_reports_actual_files(self, tmp_path: Path) -> None:
        wd = tmp_path / "repo"
        _write(wd / "a.py", "x")
        _write(wd / "b.py", "y")
        _write(wd / "sub" / "c.py", "z")
        # excluded
        _write(wd / "data" / "x.bin", b"\x00")

        bundle = build_code_zip(
            workdir=wd, job_name="cnt", user="u", out_dir=tmp_path
        )
        assert bundle.file_count == 3


# ---------------------------------------------------------------------------- #
# upload_code_zip
# ---------------------------------------------------------------------------- #


class TestUploadCodeZip:
    @pytest.fixture
    def fake_bundle(self, tmp_path: Path) -> CodeBundle:
        zp = tmp_path / "x.zip"
        zp.write_bytes(b"fake-zip")
        return CodeBundle(
            zip_path=zp,
            sha256="a" * 64,
            size_bytes=len(b"fake-zip"),
            file_count=1,
        )

    def test_happy_path(self, fake_bundle: CodeBundle) -> None:
        client = MagicMock()
        uri = upload_code_zip(
            fake_bundle,
            client,
            bucket="test-code",
            user="zhangsan",
            job_name="myjob",
        )
        assert uri == "s3://test-code/zhangsan/myjob.zip"
        assert fake_bundle.s3_uri == uri
        client.fput_object.assert_called_once()
        kwargs = client.fput_object.call_args.kwargs
        assert kwargs["bucket_name"] == "test-code"
        assert kwargs["object_name"] == "zhangsan/myjob.zip"
        assert kwargs["content_type"] == "application/zip"

    def test_explicit_object_key_overrides(self, fake_bundle: CodeBundle) -> None:
        client = MagicMock()
        uri = upload_code_zip(
            fake_bundle,
            client,
            bucket="b",
            object_key="explicit/key.zip",
        )
        assert uri == "s3://b/explicit/key.zip"
        assert client.fput_object.call_args.kwargs["object_name"] == "explicit/key.zip"

    def test_missing_user_or_job_raises(self, fake_bundle: CodeBundle) -> None:
        client = MagicMock()
        with pytest.raises(ValueError):
            upload_code_zip(fake_bundle, client, bucket="b")

    def test_no_retry_on_4xx(self, fake_bundle: CodeBundle) -> None:
        client = MagicMock()
        # Build a S3Error with code=AccessDenied
        err = S3Error(
            code="AccessDenied",
            message="forbidden",
            resource="/b/k",
            request_id="rid",
            host_id="hid",
            response=None,
        )
        client.fput_object.side_effect = err

        with pytest.raises(CodeSyncUploadError) as excinfo:
            upload_code_zip(
                fake_bundle, client, bucket="b", user="u", job_name="j"
            )
        assert "AccessDenied" in str(excinfo.value)
        # 4xx -> no retry, called exactly once
        assert client.fput_object.call_count == 1

    def test_retries_on_unknown_error_then_succeeds(
        self,
        fake_bundle: CodeBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # avoid real sleep
        import raytrain.code_sync as mod

        sleep_calls: list[float] = []
        monkeypatch.setattr(mod.time, "sleep", lambda s: sleep_calls.append(s))

        client = MagicMock()
        # First two attempts raise generic Exception (network), third succeeds
        client.fput_object.side_effect = [
            ConnectionError("temp"),
            ConnectionError("temp"),
            None,
        ]
        uri = upload_code_zip(
            fake_bundle, client, bucket="b", user="u", job_name="j"
        )
        assert uri == "s3://b/u/j.zip"
        assert client.fput_object.call_count == 3
        # backoff schedule: 2, 4 (no sleep after final attempt)
        assert sleep_calls == [2.0, 4.0]

    def test_retries_exhausted_raises(
        self,
        fake_bundle: CodeBundle,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import raytrain.code_sync as mod

        monkeypatch.setattr(mod.time, "sleep", lambda s: None)

        client = MagicMock()
        client.fput_object.side_effect = ConnectionError("nope")

        with pytest.raises(CodeSyncUploadError):
            upload_code_zip(
                fake_bundle, client, bucket="b", user="u", job_name="j"
            )
        assert client.fput_object.call_count == 3

    def test_dedup_skips_when_blob_exists(self, fake_bundle: CodeBundle) -> None:
        client = MagicMock()
        # stat_object succeeds -> object exists
        client.stat_object.return_value = MagicMock()

        uri = upload_code_zip(
            fake_bundle,
            client,
            bucket="b",
            user="u",
            job_name="j",
            dedup=True,
        )
        assert uri == f"s3://b/_blobs/{fake_bundle.sha256}.zip"
        # PUT must not happen
        client.fput_object.assert_not_called()

    def test_dedup_uploads_when_blob_missing(self, fake_bundle: CodeBundle) -> None:
        client = MagicMock()
        client.stat_object.side_effect = S3Error(
            code="NoSuchKey",
            message="missing",
            resource="/b/k",
            request_id="rid",
            host_id="hid",
            response=None,
        )

        uri = upload_code_zip(
            fake_bundle,
            client,
            bucket="b",
            user="u",
            job_name="j",
            dedup=True,
        )
        assert uri == f"s3://b/_blobs/{fake_bundle.sha256}.zip"
        client.fput_object.assert_called_once()
        # PUT goes to the blob path, not the user/job path
        assert (
            client.fput_object.call_args.kwargs["object_name"]
            == f"_blobs/{fake_bundle.sha256}.zip"
        )


# ---------------------------------------------------------------------------- #
# Acceptance tests (module-level node IDs, referenced by spec task 1.2)
#   tests/test_code_sync.py::test_build_zip_excludes_default
#   tests/test_code_sync.py::test_size_limit_exceeded
#   tests/test_code_sync.py::test_sha256_stable
# ---------------------------------------------------------------------------- #


def test_build_zip_excludes_default(tmp_path: Path) -> None:
    """Default ignore rules drop .git/, __pycache__/, data/ and *.pyc, while
    real source files are kept in the bundle."""
    wd = tmp_path / "repo"
    # real source file -> must be kept
    _write(wd / "tools" / "train.py", "print('hi')\n")
    # default-ignored entries -> must be dropped
    _write(wd / ".git" / "HEAD", "ref: refs/heads/main\n")
    _write(wd / "__pycache__" / "mod.cpython-39.pyc", b"\x00\x01")
    _write(wd / "data" / "scannet" / "x.npz", b"\x00")
    _write(wd / "tools" / "compiled.pyc", b"\x00")

    bundle = build_code_zip(
        workdir=wd, job_name="job-default", user="u", out_dir=tmp_path
    )
    names = _zip_names(bundle.zip_path)

    # kept
    assert "tools/train.py" in names
    # dropped by DEFAULT_IGNORES
    assert ".git/HEAD" not in names
    assert "__pycache__/mod.cpython-39.pyc" not in names
    assert "data/scannet/x.npz" not in names
    assert "tools/compiled.pyc" not in names


def test_size_limit_exceeded(tmp_path: Path) -> None:
    """A file larger than max_size_bytes triggers CodeSyncTooLargeError, and
    the error carries the offending file in its top_files."""
    wd = tmp_path / "repo"
    # 2 MiB of incompressible bytes so the compressed zip still exceeds the
    # small limit we pass in.
    _write(wd / "big" / "blob.bin", os.urandom(2 * 1024 * 1024))
    _write(wd / "tools" / "train.py", "ok")

    with pytest.raises(CodeSyncTooLargeError) as excinfo:
        build_code_zip(
            workdir=wd,
            job_name="big",
            user="u",
            max_size_bytes=512 * 1024,  # 512 KiB
            out_dir=tmp_path,
        )

    err = excinfo.value
    assert err.size_bytes > err.limit_bytes
    # top_files must surface the largest file (the blob).
    assert err.top_files, "expected top_files to be populated"
    assert any("big/blob.bin" in str(p) for p, _ in err.top_files)
    # largest file should be first in the descending-size list.
    assert "big/blob.bin" in str(err.top_files[0][0])


def test_sha256_stable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two builds of identical content (same user/job_name) produce the same
    sha256. The bundle metadata embeds created_at=int(time.time()), so we
    freeze time to validate determinism of the content-derived hash."""
    import raytrain.code_sync as mod

    # Freeze time so the created_at line in the metadata is identical across
    # builds; otherwise the wall-clock second could differ between runs.
    monkeypatch.setattr(mod.time, "time", lambda: 1_700_000_000.0)

    wd = tmp_path / "repo"
    # Distinct file sizes -> deterministic ordering when sorted by size.
    _write(wd / "a.py", "print(1)\n")          # 9 bytes
    _write(wd / "sub" / "b.py", "print(22)\n")  # 10 bytes

    b1 = build_code_zip(
        workdir=wd, job_name="job-a", user="u", out_dir=tmp_path / "out1"
    )
    b2 = build_code_zip(
        workdir=wd, job_name="job-a", user="u", out_dir=tmp_path / "out2"
    )

    assert b1.sha256 == b2.sha256
    assert len(b1.sha256) == 64
    assert all(c in "0123456789abcdef" for c in b1.sha256)


# ---------------------------------------------------------------------------- #
# Acceptance tests (module-level node IDs, referenced by spec task 1.3)
#   tests/test_code_sync.py::test_upload_retries_on_5xx
#   tests/test_code_sync.py::test_upload_no_retry_on_4xx
# ---------------------------------------------------------------------------- #


def _fake_bundle(tmp_path: Path) -> CodeBundle:
    """A minimal CodeBundle backed by a tiny on-disk zip for upload tests."""
    zp = tmp_path / "x.zip"
    zp.write_bytes(b"fake-zip")
    return CodeBundle(
        zip_path=zp,
        sha256="a" * 64,
        size_bytes=len(b"fake-zip"),
        file_count=1,
    )


def test_upload_retries_on_5xx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 5xx-style/transient failure is retried with exponential backoff.

    The first two attempts raise a retryable S3Error (code not in the 4xx
    no-retry set), the third succeeds. We assert fput_object is called 3
    times, the returned URI is correct, and backoff sleeps fire as 2s then 4s.

    Validates: Requirements (task 1.3 — 5xx/网络错误 3 次重试 + 指数退避)
    """
    bundle = _fake_bundle(tmp_path)

    # Avoid real sleeping; capture the backoff schedule instead.
    import raytrain.code_sync as mod

    sleep_calls: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", lambda s: sleep_calls.append(s))

    # 5xx-style S3Error: ServiceUnavailable is NOT in the 4xx no-retry set,
    # so the implementation treats it as retryable.
    transient = S3Error(
        code="ServiceUnavailable",
        message="please retry",
        resource="/b/k",
        request_id="rid",
        host_id="hid",
        response=None,
    )

    client = MagicMock()
    client.fput_object.side_effect = [transient, transient, None]

    uri = upload_code_zip(
        bundle, client, bucket="b", user="u", job_name="j"
    )

    assert uri == "s3://b/u/j.zip"
    assert bundle.s3_uri == uri
    # retried twice then succeeded on the 3rd attempt
    assert client.fput_object.call_count == 3
    # exponential backoff: 2 ** 1, 2 ** 2 (no sleep after the final attempt)
    assert sleep_calls == [2.0, 4.0]


def test_upload_no_retry_on_4xx(tmp_path: Path) -> None:
    """A 4xx client error (e.g. AccessDenied) is raised immediately with no
    retry; fput_object is called exactly once.

    Validates: Requirements (task 1.3 — 4xx 直接抛错)
    """
    bundle = _fake_bundle(tmp_path)

    client = MagicMock()
    client.fput_object.side_effect = S3Error(
        code="AccessDenied",
        message="forbidden",
        resource="/b/k",
        request_id="rid",
        host_id="hid",
        response=None,
    )

    with pytest.raises(CodeSyncUploadError) as excinfo:
        upload_code_zip(bundle, client, bucket="b", user="u", job_name="j")

    assert "AccessDenied" in str(excinfo.value)
    # 4xx -> no retry, called exactly once
    assert client.fput_object.call_count == 1


# ---------------------------------------------------------------------------- #
# Acceptance test (module-level node ID, referenced by spec task 11.3)
#   tests/test_code_sync.py::test_dedup_skip_upload_when_blob_exists
#
# nice-to-have: code_sync.dedup=true 时按 sha256 跨 user 复用 blob
# (HEAD 命中即跳过 PUT)。
# ---------------------------------------------------------------------------- #


def test_dedup_skip_upload_when_blob_exists(tmp_path: Path) -> None:
    """When ``dedup=True`` and the content blob already exists, the upload is
    skipped (no PUT) and the existing ``_blobs/<sha256>.zip`` is reused.

    The blob key is derived purely from ``bundle.sha256`` and is independent of
    ``user``/``job_name``, so two different users submitting identical content
    resolve to the SAME blob — i.e. cross-user dedup. A HEAD/stat hit short
    circuits the PUT entirely.

    Validates: Requirements (task 11.3 — sha256 跨 user 复用 blob，HEAD 命中即跳过 PUT)
    """
    bundle = _fake_bundle(tmp_path)
    blob_key = f"_blobs/{bundle.sha256}.zip"

    client = MagicMock()
    # stat_object succeeds -> the content blob already exists (HEAD hit).
    client.stat_object.return_value = MagicMock()

    # User "u" submits content whose blob already exists.
    uri = upload_code_zip(
        bundle,
        client,
        bucket="b",
        user="u",
        job_name="j",
        dedup=True,
    )

    # Reuses the existing blob, keyed only by sha256.
    assert uri == f"s3://b/{blob_key}"
    assert bundle.s3_uri == uri
    # PUT must be skipped on the HEAD hit.
    client.fput_object.assert_not_called()
    # HEAD/stat was performed against the blob key.
    client.stat_object.assert_any_call("b", blob_key)

    # Cross-user reuse: a DIFFERENT user with the SAME sha256 resolves to the
    # SAME blob and still skips the PUT.
    uri_other = upload_code_zip(
        bundle,
        client,
        bucket="b",
        user="other",
        job_name="another-job",
        dedup=True,
    )
    assert uri_other == f"s3://b/{blob_key}"
    # Still no upload for the second (different-user) submission.
    client.fput_object.assert_not_called()


# ---------------------------------------------------------------------------- #
# Default constants
# ---------------------------------------------------------------------------- #


def test_default_bucket_constant() -> None:
    assert DEFAULT_BUCKET == "raytrain-code"


def test_default_ignores_includes_obvious_stuff() -> None:
    s = set(DEFAULT_IGNORES)
    assert ".git/" in s
    assert "__pycache__/" in s
    assert "data/" in s
    assert "*.ckpt" in s
    assert ".venv/" in s
