"""
HTTP client for the raytrain Platform Control Plane.

This is the "shared cluster" path (``cluster_mode=shared``): instead of the
CLI directly creating K8s objects, it POSTs to the server which forwards to
a long-lived RayCluster.

Methods are intentionally thin so the CLI can format errors itself.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterator

try:
    import httpx
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "raytrain.platform_client requires `httpx`. Install with `pip install httpx`."
    ) from exc


DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0)


@dataclass
class PlatformError(RuntimeError):
    """Generic Platform API error. Carries enough detail to surface to user."""

    status_code: int
    detail: str
    request_id: str | None = None

    def __str__(self) -> str:
        rid = f" (request-id: {self.request_id})" if self.request_id else ""
        return f"[{self.status_code}] {self.detail}{rid}"


def _check(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    request_id = resp.headers.get("x-request-id")
    detail: str
    try:
        body = resp.json()
        detail = body.get("detail") or json.dumps(body)
    except Exception:
        detail = resp.text or resp.reason_phrase
    raise PlatformError(
        status_code=resp.status_code, detail=detail, request_id=request_id
    )


class PlatformClient:
    """Talk to /v1/* endpoints with a single bearer token."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        if not base_url:
            raise ValueError("submission_server URL is empty")
        if not token:
            raise ValueError("raytrain token is empty")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
        )

    # context-mgr support
    def __enter__(self) -> "PlatformClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # --------------------------- auth ----------------------------------------

    def whoami(self) -> dict:
        r = self._client.get("/v1/auth/me")
        _check(r)
        return r.json()

    # --------------------------- code ----------------------------------------

    def upload_code(
        self,
        zip_path: str,
        sha256: str,
        job_name: str,
    ) -> dict:
        """PUT /v1/code with the zip body. Returns the {code_uri,...} dict."""
        with open(zip_path, "rb") as fh:
            r = self._client.put(
                "/v1/code",
                content=fh,
                headers={
                    "Content-Type": "application/zip",
                    "X-Code-Sha256": sha256,
                    "X-Job-Name": job_name,
                },
            )
        _check(r)
        return r.json()

    # --------------------------- jobs ----------------------------------------

    def submit_job(
        self,
        *,
        repo: str,
        exp_name: str,
        gpu_type: str,
        num_nodes: int,
        gpus_per_node: int,
        entrypoint: str,
        code_uri: str | None = None,
        code_hash: str | None = None,
        extra_env: dict | None = None,
        extra_pip: list | None = None,
        metadata: dict | None = None,
    ) -> dict:
        body = {
            "repo": repo,
            "exp_name": exp_name,
            "gpu_type": gpu_type,
            "num_nodes": num_nodes,
            "gpus_per_node": gpus_per_node,
            "entrypoint": entrypoint,
            "code_uri": code_uri,
            "code_hash": code_hash,
            "extra_env": extra_env or {},
            "extra_pip": extra_pip or [],
            "metadata": metadata or {},
        }
        r = self._client.post("/v1/jobs", json=body)
        _check(r)
        return r.json()

    def get_job(self, submission_id: str, gpu_type: str) -> dict:
        r = self._client.get(
            f"/v1/jobs/{submission_id}", params={"gpu_type": gpu_type}
        )
        _check(r)
        return r.json()

    def stop_job(self, submission_id: str, gpu_type: str) -> None:
        r = self._client.delete(
            f"/v1/jobs/{submission_id}", params={"gpu_type": gpu_type}
        )
        _check(r)

    def list_jobs(self, gpu_type: str) -> list[dict]:
        r = self._client.get("/v1/jobs", params={"gpu_type": gpu_type})
        _check(r)
        return r.json()

    def stream_logs(
        self, submission_id: str, gpu_type: str
    ) -> Iterator[str]:
        """Yield log chunks. Caller is responsible for printing / aggregating."""
        with self._client.stream(
            "GET",
            f"/v1/jobs/{submission_id}/logs",
            params={"gpu_type": gpu_type},
        ) as r:
            _check(r)
            for chunk in r.iter_text():
                if chunk:
                    yield chunk
