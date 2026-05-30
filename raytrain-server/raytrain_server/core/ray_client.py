"""
Wrapper around Ray's :class:`JobSubmissionClient` for talking to a long-lived
RayCluster.

Why a wrapper:
    - The server exposes ``gpu_type`` (h20 / a100) as the user-facing knob;
      the actual cluster URL comes from ``settings.shared_clusters[gpu_type]``.
    - Mocking ``ray.job_submission`` directly in unit tests is awkward; this
      class accepts an injectable factory so tests can pass a stub.
    - Centralizes submission_id formatting and runtime_env construction so
      every endpoint uses identical conventions.

Used by:
    raytrain_server.api.jobs
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Protocol

from .settings import Settings, get_settings


class _JobClient(Protocol):
    """Subset of ``ray.job_submission.JobSubmissionClient`` we actually use.

    Defined as a Protocol so tests can pass any duck-typed stub.
    """

    def submit_job(
        self,
        *,
        entrypoint: str,
        submission_id: str | None = None,
        runtime_env: dict[str, Any] | None = None,
        metadata: dict[str, str] | None = None,
        entrypoint_num_cpus: float | None = None,
        entrypoint_num_gpus: float | None = None,
    ) -> str: ...

    def stop_job(self, submission_id: str) -> bool: ...

    def get_job_status(self, submission_id: str) -> Any: ...

    def get_job_info(self, submission_id: str) -> Any: ...

    def list_jobs(self) -> list[Any]: ...

    def tail_job_logs(self, submission_id: str) -> Iterator[str]: ...


JobClientFactory = Callable[[str], _JobClient]


def _default_factory(address: str) -> _JobClient:
    """Default factory: construct a real JobSubmissionClient. Imported lazily
    so the test environment doesn't need a running Ray installation."""
    from ray.job_submission import JobSubmissionClient  # type: ignore

    return JobSubmissionClient(address)


# ---------------------------------------------------------------------------- #
# Submission ID
# ---------------------------------------------------------------------------- #


_SUBMISSION_SAFE = re.compile(r"[^a-z0-9-]")


def make_submission_id(user: str, repo: str, exp_name: str) -> str:
    """Stable, human-readable submission id.

    Format: ``<user>-<repo>-<exp>-<utcstamp>``  (DNS-1123 safe).
    """
    stamp = datetime.now(timezone.utc).strftime("%y%m%d-%H%M%S")
    parts = [user or "anon", repo or "repo", exp_name or "run", stamp]
    safe = "-".join(_SUBMISSION_SAFE.sub("-", p.lower()) for p in parts)
    safe = re.sub(r"-+", "-", safe).strip("-")
    return safe[:63] or "raytrain-run"


# ---------------------------------------------------------------------------- #
# Submission spec
# ---------------------------------------------------------------------------- #


@dataclass
class JobSubmissionSpec:
    """Everything :class:`RayClusterClient.submit_job` needs to send to Ray."""

    user: str
    tenant: str
    gpu_type: str
    num_nodes: int
    gpus_per_node: int
    entrypoint: str
    code_uri: str | None = None
    code_hash: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    extra_pip: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------- #
# Cluster client
# ---------------------------------------------------------------------------- #


class RayClusterClient:
    """Talks to a long-lived RayCluster's Job Submission API.

    Pick the right cluster by ``gpu_type`` lookup in ``settings.shared_clusters``.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        client_factory: JobClientFactory | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._factory = client_factory or _default_factory
        # Cache one client per gpu_type so we don't re-handshake every call.
        self._cache: dict[str, _JobClient] = {}

    # -- selection ------------------------------------------------------------

    def address_for(self, gpu_type: str) -> str:
        try:
            return self._settings.shared_clusters[gpu_type]
        except KeyError as exc:
            raise ValueError(
                f"unknown gpu_type {gpu_type!r}; configured: "
                f"{sorted(self._settings.shared_clusters)}"
            ) from exc

    def get_client(self, gpu_type: str) -> _JobClient:
        if gpu_type not in self._cache:
            self._cache[gpu_type] = self._factory(self.address_for(gpu_type))
        return self._cache[gpu_type]

    # -- runtime_env builder --------------------------------------------------

    def build_runtime_env(self, spec: JobSubmissionSpec) -> dict[str, Any]:
        """
        Build the ``runtime_env`` dict for ``submit_job``.

        - When ``code_uri`` is set we point ``working_dir`` at it; Ray fetches
          and unpacks the zip on every worker.
        - We always inject env vars that the training subprocess relies on
          (RAYTRAIN_USER, RAYTRAIN_RUN_ID etc.) plus the user's manifest envs.
        """
        env_vars: dict[str, str] = {
            "RAYTRAIN_USER": spec.user,
            "RAYTRAIN_TENANT": spec.tenant,
            "RAYTRAIN_GPU_TYPE": spec.gpu_type,
            "RAYTRAIN_NUM_NODES": str(spec.num_nodes),
            "RAYTRAIN_GPUS_PER_NODE": str(spec.gpus_per_node),
            "PYTHONUNBUFFERED": "1",
        }
        if spec.code_uri:
            env_vars["RAYTRAIN_CODE_URI"] = spec.code_uri
        if spec.code_hash:
            env_vars["RAYTRAIN_CODE_HASH"] = spec.code_hash
        if self._settings.mlflow_tracking_uri:
            env_vars["MLFLOW_TRACKING_URI"] = self._settings.mlflow_tracking_uri
        if self._settings.minio_endpoint:
            env_vars["AWS_ENDPOINT_URL"] = self._settings.minio_endpoint
            env_vars["S3_ENDPOINT_URL"] = self._settings.minio_endpoint
        # User-supplied env wins (manifest.launcher.env)
        env_vars.update({str(k): str(v) for k, v in spec.extra_env.items()})

        runtime_env: dict[str, Any] = {
            "env_vars": env_vars,
            # Larger setup window: code zip can be tens of MiB; cold-start
            # workers may also need to pip-install transient deps.
            "config": {"setup_timeout_seconds": 600},
        }
        if spec.code_uri:
            runtime_env["working_dir"] = spec.code_uri
        if spec.extra_pip:
            runtime_env["pip"] = list(spec.extra_pip)
        return runtime_env

    # -- submit ---------------------------------------------------------------

    def submit_job(
        self,
        spec: JobSubmissionSpec,
        submission_id: str,
        repo: str,
    ) -> str:
        """Send the spec to the right RayCluster. Returns the submission_id."""
        client = self.get_client(spec.gpu_type)
        runtime_env = self.build_runtime_env(spec)
        metadata = {
            "raytrain.user": spec.user,
            "raytrain.tenant": spec.tenant,
            "raytrain.gpu_type": spec.gpu_type,
            "raytrain.repo": repo,
            **spec.metadata,
        }
        return client.submit_job(
            entrypoint=spec.entrypoint,
            submission_id=submission_id,
            runtime_env=runtime_env,
            metadata=metadata,
        )

    # -- query / control ------------------------------------------------------

    def stop(self, gpu_type: str, submission_id: str) -> bool:
        return self.get_client(gpu_type).stop_job(submission_id)

    def get_status(self, gpu_type: str, submission_id: str) -> str:
        status = self.get_client(gpu_type).get_job_status(submission_id)
        return str(status)

    def get_info(self, gpu_type: str, submission_id: str) -> Any:
        return self.get_client(gpu_type).get_job_info(submission_id)

    def list_jobs(self, gpu_type: str) -> Iterable[Any]:
        return self.get_client(gpu_type).list_jobs()

    def tail_logs(self, gpu_type: str, submission_id: str) -> Iterator[str]:
        return self.get_client(gpu_type).tail_job_logs(submission_id)
