"""Thin wrapper around Ray's :class:`ray.job_submission.JobSubmissionClient`.

The submission server talks to one long-lived ("shared") RayCluster per GPU
type. Each cluster exposes a Ray dashboard on port ``8265``; the dashboard URL
is what ``JobSubmissionClient`` connects to. This module maps a ``gpu_type``
(e.g. ``"h20"`` / ``"a100"``) to the matching dashboard URL and forwards
``submit`` / ``stop`` / ``tail logs`` calls to the right cluster.

Lazy ray import
---------------
Ray is a heavy, GPU-side dependency that is **not** installed where this server
package is merely imported (CLI hosts, CI, unit tests). Therefore::

    import raytrain.server.ray_client

must succeed even when ``ray`` is absent. To guarantee that, the real import
``from ray.job_submission import JobSubmissionClient`` lives inside
:func:`_make_submission_client` -- the single, overridable construction hook --
and never at module top level. Unit tests monkeypatch
:func:`_make_submission_client` to return a ``MagicMock``, so they run with no
ray and no network.

from_env contract
-----------------
:meth:`RayClusterClient.from_env` builds the ``gpu_type -> url`` mapping from the
environment using this precedence:

1. ``RAYTRAIN_SHARED_CLUSTERS`` -- a JSON object, e.g.
   ``{"h20": "http://ray-shared-h20-head.ray-shared.svc:8265", "a100": "..."}``.
   When set and non-empty it is the sole source of truth.
2. Otherwise, any ``RAYTRAIN_CLUSTER_URL_<GPU_TYPE>`` variables are collected,
   where the suffix after the prefix is lowercased to form the ``gpu_type``
   (``RAYTRAIN_CLUSTER_URL_H20`` -> ``h20``, ``RAYTRAIN_CLUSTER_URL_A100`` ->
   ``a100``).

If neither yields any entries, :class:`RayClientError` (code
``no_clusters_configured``) is raised.
"""
from __future__ import annotations

import json
import os
from typing import Dict, Iterator, Optional

# Prefix used by the per-GPU-type env-var fallback in :meth:`from_env`.
_CLUSTER_URL_ENV_PREFIX = "RAYTRAIN_CLUSTER_URL_"
# JSON mapping env var consulted first by :meth:`from_env`.
_SHARED_CLUSTERS_ENV = "RAYTRAIN_SHARED_CLUSTERS"


class RayClientError(RuntimeError):
    """A failure talking to a shared RayCluster.

    Carries a short machine-readable :attr:`code` and a human :attr:`message`
    so the API layer (task 7.4 / 7.5) can render structured responses.

    Codes used here:

    * ``unknown_gpu_type``        -- no URL configured for the requested type.
    * ``no_clusters_configured``  -- :meth:`from_env` found no cluster URLs.
    * ``ray_not_installed``       -- ``ray`` could not be imported at call time.
    * ``submit_failed`` / ``stop_failed`` / ``tail_failed`` / ``list_failed``
      -- the underlying ``JobSubmissionClient`` raised.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"RayClientError(code={self.code!r}, message={self.message!r})"


def _make_submission_client(url: str):
    """Construct and return a ``JobSubmissionClient`` for ``url``.

    This is the single seam between the wrapper and Ray. The ``ray`` import is
    performed here (not at module top level) so importing this module never
    requires ray. Unit tests monkeypatch this function to return a mock,
    exercising the wrapper with no ray installed and no network access.
    """
    try:
        from ray.job_submission import JobSubmissionClient
    except ImportError as exc:  # pragma: no cover - exercised only without ray
        raise RayClientError(
            "ray_not_installed",
            "ray is not installed; install with `pip install raytrain[ray]` "
            "to talk to a shared RayCluster",
        ) from exc
    return JobSubmissionClient(url)


class RayClusterClient:
    """Route job operations to the shared RayCluster for a given ``gpu_type``.

    Construct with an explicit ``gpu_type -> dashboard URL`` mapping, or use
    :meth:`from_env`. Built ``JobSubmissionClient`` instances are cached per URL
    so repeated calls for the same cluster reuse one connection object.
    """

    def __init__(self, cluster_urls: Dict[str, str]) -> None:
        if not cluster_urls:
            raise RayClientError(
                "no_clusters_configured",
                "RayClusterClient requires a non-empty gpu_type -> url mapping",
            )
        # Normalise keys to lowercase so lookups are case-insensitive.
        self._cluster_urls: Dict[str, str] = {
            str(k).lower(): str(v) for k, v in cluster_urls.items()
        }
        # url -> JobSubmissionClient (lazily constructed, then cached).
        self._clients: Dict[str, object] = {}

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "RayClusterClient":
        """Build a client from environment variables.

        See the module docstring for the precedence rules. ``env`` defaults to
        ``os.environ`` and is injectable for testing.
        """
        environ = os.environ if env is None else env
        urls = _cluster_urls_from_env(environ)
        if not urls:
            raise RayClientError(
                "no_clusters_configured",
                f"no shared clusters configured; set {_SHARED_CLUSTERS_ENV} "
                f"(JSON) or {_CLUSTER_URL_ENV_PREFIX}<GPU_TYPE> env vars",
            )
        return cls(urls)

    @property
    def gpu_types(self) -> list:
        """The GPU types this client knows about (lowercased)."""
        return sorted(self._cluster_urls)

    def _client_for(self, gpu_type: str):
        """Return a (cached) ``JobSubmissionClient`` for ``gpu_type``.

        Raises :class:`RayClientError` (``unknown_gpu_type``) if the type has no
        configured URL.
        """
        key = str(gpu_type).lower()
        url = self._cluster_urls.get(key)
        if url is None:
            known = ", ".join(self.gpu_types) or "<none>"
            raise RayClientError(
                "unknown_gpu_type",
                f"no shared cluster configured for gpu_type={gpu_type!r}; "
                f"known types: {known}",
            )
        client = self._clients.get(url)
        if client is None:
            client = _make_submission_client(url)
            self._clients[url] = client
        return client

    def submit_job(
        self,
        gpu_type: str,
        entrypoint: str,
        runtime_env: Optional[dict] = None,
        metadata: Optional[dict] = None,
        submission_id: Optional[str] = None,
        **kwargs,
    ) -> str:
        """Submit a job to the cluster for ``gpu_type`` and return its id.

        ``entrypoint`` / ``runtime_env`` / ``metadata`` / ``submission_id`` are
        forwarded to ``JobSubmissionClient.submit_job``. Extra ``kwargs`` are
        passed through unchanged. Returns the submission id string Ray assigns
        (equal to ``submission_id`` when one is supplied).
        """
        client = self._client_for(gpu_type)
        try:
            return client.submit_job(
                entrypoint=entrypoint,
                runtime_env=runtime_env,
                metadata=metadata,
                submission_id=submission_id,
                **kwargs,
            )
        except RayClientError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise upstream failures
            raise RayClientError(
                "submit_failed",
                f"failed to submit job to {gpu_type!r} cluster: {exc}",
            ) from exc

    def stop_job(self, gpu_type: str, submission_id: str) -> bool:
        """Stop ``submission_id`` on the ``gpu_type`` cluster.

        Returns the boolean ``JobSubmissionClient.stop_job`` returns (``True``
        when the stop request was accepted).
        """
        client = self._client_for(gpu_type)
        try:
            return client.stop_job(submission_id)
        except RayClientError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise upstream failures
            raise RayClientError(
                "stop_failed",
                f"failed to stop job {submission_id!r} on {gpu_type!r} "
                f"cluster: {exc}",
            ) from exc

    def tail_logs(self, gpu_type: str, submission_id: str) -> Iterator[str]:
        """Yield log chunks for ``submission_id`` from the ``gpu_type`` cluster.

        Wraps ``JobSubmissionClient.tail_job_logs`` (which yields string chunks)
        as a generator so callers get a simple iterable of strings. The
        underlying client is resolved eagerly (so an ``unknown_gpu_type`` error
        surfaces immediately), while log chunks are produced lazily.
        """
        client = self._client_for(gpu_type)

        def _gen() -> Iterator[str]:
            try:
                for chunk in client.tail_job_logs(submission_id):
                    yield chunk
            except RayClientError:
                raise
            except Exception as exc:  # noqa: BLE001 - normalise upstream failures
                raise RayClientError(
                    "tail_failed",
                    f"failed to tail logs for job {submission_id!r} on "
                    f"{gpu_type!r} cluster: {exc}",
                ) from exc

        return _gen()

    def get_job_status(self, gpu_type: str, submission_id: str):
        """Return ``JobSubmissionClient.get_job_status`` for ``submission_id``.

        Convenience pass-through used by the status endpoint (task 7.5). The
        return value is Ray's ``JobStatus`` enum/string as-is.
        """
        client = self._client_for(gpu_type)
        try:
            return client.get_job_status(submission_id)
        except RayClientError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise upstream failures
            raise RayClientError(
                "status_failed",
                f"failed to get status for job {submission_id!r} on "
                f"{gpu_type!r} cluster: {exc}",
            ) from exc

    def get_job_info(self, gpu_type: str, submission_id: str) -> Dict[str, object]:
        """Return a single job's info (incl. ``metadata``) as a plain dict.

        Wraps ``JobSubmissionClient.get_job_info`` (which returns a single
        ``JobDetails``) and normalises it via :func:`_job_to_dict` so callers
        get at least ``submission_id`` / ``status`` / ``metadata``. Used by the
        tenant-isolation guard (task 9.3) to discover a job's owning tenant
        (stored under ``metadata['tenant']`` by ``submit_job``).

        The ``ray`` import stays lazy (it happens inside
        :func:`_make_submission_client` via :meth:`_client_for`), and upstream
        failures are normalised to :class:`RayClientError` with code
        ``info_failed`` -- consistent with the other methods. The
        ``unknown_gpu_type`` error from :meth:`_client_for` propagates unchanged
        (so a caller trying each configured cluster can fall through).
        """
        client = self._client_for(gpu_type)
        try:
            info = client.get_job_info(submission_id)
        except RayClientError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise upstream failures
            raise RayClientError(
                "info_failed",
                f"failed to get info for job {submission_id!r} on "
                f"{gpu_type!r} cluster: {exc}",
            ) from exc
        return _job_to_dict(info, gpu_type)

    def list_jobs(self, gpu_type: str) -> list:
        """Return the jobs known to the ``gpu_type`` cluster as plain dicts.

        Wraps ``JobSubmissionClient.list_jobs`` (which returns a list of
        ``JobDetails`` objects) and normalises each entry to a JSON-friendly
        dict via :func:`_job_to_dict` (at least ``submission_id`` / ``status`` /
        ``metadata``). The ``ray`` import stays lazy (it happens inside
        :func:`_make_submission_client` via :meth:`_client_for`), and upstream
        failures are normalised to :class:`RayClientError` with code
        ``list_failed`` -- consistent with the other methods. The ``unknown_gpu_type``
        error from :meth:`_client_for` propagates unchanged.
        """
        client = self._client_for(gpu_type)
        try:
            jobs = client.list_jobs()
        except RayClientError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise upstream failures
            raise RayClientError(
                "list_failed",
                f"failed to list jobs on {gpu_type!r} cluster: {exc}",
            ) from exc
        return [_job_to_dict(job, gpu_type) for job in (jobs or [])]


def _job_to_dict(job: object, gpu_type: str) -> Dict[str, object]:
    """Normalise a Ray ``JobDetails`` (or mock) into a JSON-friendly dict.

    Ray's ``JobSubmissionClient.list_jobs`` returns ``JobDetails`` objects whose
    relevant attributes are ``submission_id`` / ``job_id`` / ``status`` /
    ``metadata`` / ``entrypoint``. We read them defensively with ``getattr`` so
    a partial mock (or a plain dict) works too, and stringify ``status`` since
    it is a ``JobStatus`` enum upstream. ``gpu_type`` is attached so an
    aggregated listing keeps track of which cluster a job came from.
    """
    if isinstance(job, dict):
        get = job.get
    else:
        def get(name, default=None):
            return getattr(job, name, default)

    status = get("status")
    metadata = get("metadata") or {}
    return {
        "submission_id": get("submission_id") or get("job_id"),
        "job_id": get("job_id"),
        "status": None if status is None else str(status),
        "entrypoint": get("entrypoint"),
        "metadata": dict(metadata) if isinstance(metadata, dict) else {},
        "gpu_type": gpu_type,
    }


def _cluster_urls_from_env(environ: Dict[str, str]) -> Dict[str, str]:
    """Extract a ``gpu_type -> url`` mapping from ``environ``.

    Precedence: a non-empty JSON ``RAYTRAIN_SHARED_CLUSTERS`` wins; otherwise
    collect ``RAYTRAIN_CLUSTER_URL_<GPU_TYPE>`` variables. Returns an empty dict
    when nothing is configured (callers decide whether that is an error).
    """
    raw = environ.get(_SHARED_CLUSTERS_ENV)
    if raw and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RayClientError(
                "invalid_clusters_config",
                f"{_SHARED_CLUSTERS_ENV} is not valid JSON: {exc}",
            ) from exc
        if not isinstance(parsed, dict):
            raise RayClientError(
                "invalid_clusters_config",
                f"{_SHARED_CLUSTERS_ENV} must be a JSON object of "
                f"gpu_type -> url",
            )
        return {str(k).lower(): str(v) for k, v in parsed.items()}

    urls: Dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith(_CLUSTER_URL_ENV_PREFIX) and value:
            gpu_type = key[len(_CLUSTER_URL_ENV_PREFIX):].lower()
            if gpu_type:
                urls[gpu_type] = value
    return urls
