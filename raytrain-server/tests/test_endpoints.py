"""End-to-end tests through the FastAPI TestClient.

We replace the RayClusterClient dependency with a fake so the test suite
doesn't need a running Ray cluster.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from raytrain_server.api.jobs import _ray_client
from raytrain_server.core.jwt_auth import issue_token
from raytrain_server.core.ray_client import RayClusterClient
from raytrain_server.core.settings import Settings, get_settings
from raytrain_server.main import create_app


# ---------------------------------------------------------------------------- #
# Fake Ray client
# ---------------------------------------------------------------------------- #


class _FakeRayClusterClient:
    def __init__(self, *args, **kwargs) -> None:
        self.submitted: list[dict] = []
        self.stopped: list[str] = []
        self.metadata: dict[str, dict[str, str]] = {}

    def address_for(self, gpu_type: str) -> str:
        return f"http://ray-shared-{gpu_type}:8265"

    def build_runtime_env(self, spec):
        return {
            "working_dir": spec.code_uri,
            "env_vars": {"RAYTRAIN_USER": spec.user},
            "config": {"setup_timeout_seconds": 600},
        }

    def submit_job(self, spec, submission_id: str, repo: str) -> str:
        self.submitted.append(
            {
                "submission_id": submission_id,
                "user": spec.user,
                "tenant": spec.tenant,
                "code_uri": spec.code_uri,
                "repo": repo,
            }
        )
        self.metadata[submission_id] = {
            "raytrain.user": spec.user,
            "raytrain.tenant": spec.tenant,
            "raytrain.repo": repo,
        }
        return submission_id

    def stop(self, gpu_type: str, submission_id: str) -> bool:
        self.stopped.append(submission_id)
        return True

    def get_info(self, gpu_type: str, submission_id: str):
        meta = self.metadata.get(submission_id, {"raytrain.user": "someone-else"})

        class _I:
            status = "RUNNING"
            metadata = meta

        return _I()

    def list_jobs(self, gpu_type: str):
        out = []
        for sid, meta in self.metadata.items():
            class _J:
                submission_id = sid
                status = "RUNNING"
                metadata = meta

            # Capture by default value
            j = _J()
            j.submission_id = sid
            j.status = "RUNNING"
            j.metadata = dict(meta)
            out.append(j)
        return out

    def tail_logs(self, gpu_type: str, submission_id: str) -> Iterator[str]:
        yield f"line-1 for {submission_id}\n"
        yield "line-2\n"


# ---------------------------------------------------------------------------- #
# Fixtures
# ---------------------------------------------------------------------------- #


@pytest.fixture
def app(settings: Settings):
    app = create_app(settings=settings)
    fake = _FakeRayClusterClient()
    app.dependency_overrides[_ray_client] = lambda: fake
    app.state.fake_ray = fake  # type: ignore[attr-defined]
    yield app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def user_token(settings: Settings) -> str:
    token, _ = issue_token("zhangsan", tenant="occ", role="user", settings=settings)
    return token


@pytest.fixture
def admin_token(settings: Settings) -> str:
    token, _ = issue_token("root", tenant="default", role="admin", settings=settings)
    return token


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------- #
# /healthz, /readyz
# ---------------------------------------------------------------------------- #


class TestHealth:
    def test_healthz_no_auth(self, client: TestClient) -> None:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_readyz_no_auth(self, client: TestClient) -> None:
        r = client.get("/readyz")
        assert r.status_code == 200


# ---------------------------------------------------------------------------- #
# /v1/auth/me
# ---------------------------------------------------------------------------- #


class TestAuthMe:
    def test_requires_token(self, client: TestClient) -> None:
        r = client.get("/v1/auth/me")
        assert r.status_code == 401

    def test_returns_decoded_identity(
        self, client: TestClient, user_token: str
    ) -> None:
        r = client.get("/v1/auth/me", headers=_h(user_token))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"] == "zhangsan"
        assert body["tenant"] == "occ"
        assert body["role"] == "user"


# ---------------------------------------------------------------------------- #
# /v1/jobs
# ---------------------------------------------------------------------------- #


def _submit_payload(**overrides) -> dict:
    p = {
        "repo": "pointcept",
        "exp_name": "smoke",
        "gpu_type": "h20",
        "num_nodes": 1,
        "gpus_per_node": 1,
        "entrypoint": "python tools/train.py --config configs/x.py",
        "code_uri": "s3://raytrain-code/zhangsan/abc.zip",
        "code_hash": "abc123",
    }
    p.update(overrides)
    return p


class TestSubmitJob:
    def test_happy_path(self, client: TestClient, user_token: str, app) -> None:
        r = client.post("/v1/jobs", json=_submit_payload(), headers=_h(user_token))
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["submission_id"].startswith("zhangsan-pointcept-smoke-")
        assert body["code_uri"] == "s3://raytrain-code/zhangsan/abc.zip"
        assert body["cluster_address"] == "http://ray-shared-h20:8265"
        # Ensure forwarded to fake Ray
        assert len(app.state.fake_ray.submitted) == 1
        assert app.state.fake_ray.submitted[0]["user"] == "zhangsan"

    def test_no_token_blocks(self, client: TestClient) -> None:
        r = client.post("/v1/jobs", json=_submit_payload())
        assert r.status_code == 401

    def test_unknown_gpu_type_400(
        self, client: TestClient, user_token: str
    ) -> None:
        r = client.post(
            "/v1/jobs",
            json=_submit_payload(gpu_type="a100"),
            headers=_h(user_token),
        )
        assert r.status_code == 400, r.text

    def test_invalid_entrypoint_400(
        self, client: TestClient, user_token: str
    ) -> None:
        r = client.post(
            "/v1/jobs",
            json=_submit_payload(entrypoint="python a.py; rm -rf /"),
            headers=_h(user_token),
        )
        assert r.status_code == 422  # pydantic validation


class TestQueryJob:
    def test_owner_can_get(
        self, client: TestClient, user_token: str
    ) -> None:
        # First submit, then query
        sub = client.post(
            "/v1/jobs", json=_submit_payload(), headers=_h(user_token)
        ).json()
        sid = sub["submission_id"]
        r = client.get(
            f"/v1/jobs/{sid}",
            params={"gpu_type": "h20"},
            headers=_h(user_token),
        )
        assert r.status_code == 200
        assert r.json()["status"] == "RUNNING"

    def test_other_user_blocked(
        self,
        client: TestClient,
        user_token: str,
        settings: Settings,
    ) -> None:
        # zhangsan submits
        sub = client.post(
            "/v1/jobs", json=_submit_payload(), headers=_h(user_token)
        ).json()
        sid = sub["submission_id"]
        # lisi tries to read
        other_token, _ = issue_token("lisi", settings=settings)
        r = client.get(
            f"/v1/jobs/{sid}",
            params={"gpu_type": "h20"},
            headers=_h(other_token),
        )
        assert r.status_code == 403

    def test_admin_can_read_anything(
        self,
        client: TestClient,
        user_token: str,
        admin_token: str,
    ) -> None:
        sub = client.post(
            "/v1/jobs", json=_submit_payload(), headers=_h(user_token)
        ).json()
        sid = sub["submission_id"]
        r = client.get(
            f"/v1/jobs/{sid}",
            params={"gpu_type": "h20"},
            headers=_h(admin_token),
        )
        assert r.status_code == 200


class TestListJobs:
    def test_user_only_sees_own(
        self,
        client: TestClient,
        user_token: str,
        settings: Settings,
        app,
    ) -> None:
        # zhangsan submits 2
        client.post("/v1/jobs", json=_submit_payload(), headers=_h(user_token))
        client.post(
            "/v1/jobs",
            json=_submit_payload(exp_name="another"),
            headers=_h(user_token),
        )
        # lisi submits 1
        lisi_token, _ = issue_token("lisi", settings=settings)
        client.post("/v1/jobs", json=_submit_payload(), headers=_h(lisi_token))

        # zhangsan list
        r = client.get(
            "/v1/jobs", params={"gpu_type": "h20"}, headers=_h(user_token)
        )
        assert r.status_code == 200
        items = r.json()
        for it in items:
            assert it["metadata"]["raytrain.user"] == "zhangsan"
        assert len(items) == 2

    def test_admin_sees_all(
        self,
        client: TestClient,
        user_token: str,
        admin_token: str,
        settings: Settings,
    ) -> None:
        client.post("/v1/jobs", json=_submit_payload(), headers=_h(user_token))
        lisi_token, _ = issue_token("lisi", settings=settings)
        client.post("/v1/jobs", json=_submit_payload(), headers=_h(lisi_token))

        r = client.get(
            "/v1/jobs", params={"gpu_type": "h20"}, headers=_h(admin_token)
        )
        assert r.status_code == 200
        users = {it["metadata"]["raytrain.user"] for it in r.json()}
        assert users == {"zhangsan", "lisi"}


class TestStopJob:
    def test_owner_can_stop(self, client: TestClient, user_token: str, app) -> None:
        sub = client.post(
            "/v1/jobs", json=_submit_payload(), headers=_h(user_token)
        ).json()
        sid = sub["submission_id"]
        r = client.delete(
            f"/v1/jobs/{sid}",
            params={"gpu_type": "h20"},
            headers=_h(user_token),
        )
        assert r.status_code == 204
        assert sid in app.state.fake_ray.stopped

    def test_other_user_blocked(
        self, client: TestClient, user_token: str, settings: Settings
    ) -> None:
        sub = client.post(
            "/v1/jobs", json=_submit_payload(), headers=_h(user_token)
        ).json()
        sid = sub["submission_id"]
        other_token, _ = issue_token("lisi", settings=settings)
        r = client.delete(
            f"/v1/jobs/{sid}",
            params={"gpu_type": "h20"},
            headers=_h(other_token),
        )
        assert r.status_code == 403


class TestLogs:
    def test_streams_lines(self, client: TestClient, user_token: str) -> None:
        sub = client.post(
            "/v1/jobs", json=_submit_payload(), headers=_h(user_token)
        ).json()
        sid = sub["submission_id"]
        with client.stream(
            "GET",
            f"/v1/jobs/{sid}/logs",
            params={"gpu_type": "h20"},
            headers=_h(user_token),
        ) as r:
            assert r.status_code == 200
            content = b"".join(r.iter_bytes()).decode()
            assert f"line-1 for {sid}" in content
            assert "line-2" in content


# ---------------------------------------------------------------------------- #
# /v1/code (mock MinIO)
# ---------------------------------------------------------------------------- #


class TestCodeUpload:
    def test_happy_path(
        self, client: TestClient, user_token: str, settings: Settings
    ) -> None:
        body = b"PK\x03\x04 fake zip body for tests" * 100
        sha = hashlib.sha256(body).hexdigest()

        with patch("raytrain_server.api.code.make_minio_client") as mk:
            fake = MagicMock()
            fake.bucket_exists.return_value = True
            mk.return_value = fake

            r = client.put(
                "/v1/code",
                content=body,
                headers={
                    **_h(user_token),
                    "Content-Type": "application/zip",
                    "X-Code-Sha256": sha,
                    "X-Job-Name": "myjob-123",
                },
            )
        assert r.status_code == 201, r.text
        body_json = r.json()
        assert body_json["sha256"] == sha
        assert body_json["bucket"] == "raytrain-code"
        assert body_json["object_key"] == "zhangsan/myjob-123.zip"
        assert body_json["code_uri"] == "s3://raytrain-code/zhangsan/myjob-123.zip"
        fake.put_object.assert_called_once()

    def test_no_token_blocks(self, client: TestClient) -> None:
        r = client.put(
            "/v1/code",
            content=b"x",
            headers={
                "Content-Type": "application/zip",
                "X-Code-Sha256": "abc",
                "X-Job-Name": "j",
            },
        )
        assert r.status_code == 401

    def test_invalid_job_name_400(
        self, client: TestClient, user_token: str
    ) -> None:
        r = client.put(
            "/v1/code",
            content=b"x",
            headers={
                **_h(user_token),
                "X-Code-Sha256": "abc",
                "X-Job-Name": "Bad Name",  # uppercase + space
            },
        )
        assert r.status_code == 400

    def test_sha_mismatch_400(
        self, client: TestClient, user_token: str
    ) -> None:
        with patch("raytrain_server.api.code.make_minio_client") as mk:
            fake = MagicMock()
            fake.bucket_exists.return_value = True
            mk.return_value = fake
            r = client.put(
                "/v1/code",
                content=b"actual body",
                headers={
                    **_h(user_token),
                    "X-Code-Sha256": "0" * 64,
                    "X-Job-Name": "j",
                },
            )
        assert r.status_code == 400

    def test_empty_body_400(self, client: TestClient, user_token: str) -> None:
        r = client.put(
            "/v1/code",
            content=b"",
            headers={
                **_h(user_token),
                "X-Code-Sha256": hashlib.sha256(b"").hexdigest(),
                "X-Job-Name": "j",
            },
        )
        assert r.status_code == 400
