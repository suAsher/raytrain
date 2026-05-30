"""Tests for raytrain.platform_client."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from raytrain.platform_client import PlatformClient, PlatformError  # noqa: E402


def _client_with_handler(handler) -> PlatformClient:
    transport = httpx.MockTransport(handler)
    pc = PlatformClient(base_url="http://example", token="t")
    pc._client = httpx.Client(
        base_url="http://example",
        transport=transport,
        headers={"Authorization": "Bearer t"},
    )
    return pc


def test_init_validates_url() -> None:
    with pytest.raises(ValueError):
        PlatformClient(base_url="", token="t")


def test_init_validates_token() -> None:
    with pytest.raises(ValueError):
        PlatformClient(base_url="http://x", token="")


def test_whoami_round_trip() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/auth/me"
        assert req.headers["authorization"] == "Bearer t"
        return httpx.Response(200, json={"user": "z", "tenant": "occ", "role": "user",
                                          "issued_at": 1, "expires_at": 2})
    pc = _client_with_handler(h)
    body = pc.whoami()
    assert body["user"] == "z"


def test_error_propagates_status_and_detail() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "token expired"})
    pc = _client_with_handler(h)
    with pytest.raises(PlatformError) as exc:
        pc.whoami()
    assert exc.value.status_code == 401
    assert "token expired" in str(exc.value)


def test_submit_job_sends_full_body() -> None:
    captured: dict = {}

    def h(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(202, json={
            "submission_id": "abc-123",
            "code_uri": "s3://b/k.zip",
            "cluster_address": "http://ray:8265",
            "runtime_env": {"env_vars": {}},
        })

    pc = _client_with_handler(h)
    out = pc.submit_job(
        repo="pointcept",
        exp_name="smoke",
        gpu_type="h20",
        num_nodes=1,
        gpus_per_node=8,
        entrypoint="python tools/train.py --foo 1",
        code_uri="s3://b/k.zip",
        code_hash="abcdef",
    )
    assert out["submission_id"] == "abc-123"
    assert captured["body"]["entrypoint"] == "python tools/train.py --foo 1"
    assert captured["body"]["gpu_type"] == "h20"
    assert captured["body"]["code_uri"] == "s3://b/k.zip"


def test_list_jobs_passes_gpu_type_query() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v1/jobs"
        assert req.url.params.get("gpu_type") == "h20"
        return httpx.Response(200, json=[])

    pc = _client_with_handler(h)
    assert pc.list_jobs("h20") == []


def test_stop_job_sends_delete() -> None:
    seen: dict = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        return httpx.Response(204)

    pc = _client_with_handler(h)
    pc.stop_job("abc", "h20")
    assert seen == {"method": "DELETE", "path": "/v1/jobs/abc"}


def test_stream_logs_yields_chunks() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        # MockTransport doesn't truly stream, but iter_text will see this body
        return httpx.Response(200, text="line-1\nline-2\n")

    pc = _client_with_handler(h)
    out = "".join(pc.stream_logs("abc", "h20"))
    assert "line-1" in out and "line-2" in out


def test_upload_code_sends_headers(tmp_path: Path) -> None:
    zp = tmp_path / "code.zip"
    zp.write_bytes(b"PK\x03\x04zip")
    captured: dict = {}

    def h(req: httpx.Request) -> httpx.Response:
        captured["sha"] = req.headers.get("x-code-sha256")
        captured["job"] = req.headers.get("x-job-name")
        captured["ct"] = req.headers.get("content-type")
        return httpx.Response(201, json={
            "code_uri": "s3://b/u/j.zip",
            "sha256": "abc",
            "size_bytes": 5,
            "bucket": "b",
            "object_key": "u/j.zip",
        })

    pc = _client_with_handler(h)
    out = pc.upload_code(str(zp), sha256="abc", job_name="myjob")
    assert out["code_uri"] == "s3://b/u/j.zip"
    assert captured["sha"] == "abc"
    assert captured["job"] == "myjob"
    assert captured["ct"] == "application/zip"
