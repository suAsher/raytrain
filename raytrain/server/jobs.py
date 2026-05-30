"""``POST /v1/jobs`` -- submit a training job to a shared RayCluster.

This is the API layer that sits on top of the already-built pieces:

* :mod:`raytrain.server.auth` -- :func:`require_identity` authenticates the
  caller (bad/missing token -> 401) and hands us an :class:`Identity`.
* :mod:`raytrain.server.ray_client` -- :class:`RayClusterClient` routes the
  submission to the right shared cluster for the requested ``gpu_type``.

The route does four things:

1. Validate the request body (:class:`SubmitJobRequest`, pydantic v2).
2. Assemble the Ray ``runtime_env`` (working_dir + env_vars + config) and the
   submission ``metadata`` (creator / tenant / gpu_type).
3. Call ``RayClusterClient.submit_job`` with an *upstream retry* (up to
   :data:`_MAX_SUBMIT_ATTEMPTS` attempts) on transient failures.
4. Write a structured audit log entry (logger ``raytrain.audit``) and return
   ``{"submission_id", "gpu_type", "cluster"}``.

Request-shape reconciliation
----------------------------
The CLI shared-mode submit path
(:meth:`raytrain.platform_client.PlatformClient.submit_job`) POSTs this JSON::

    {repo, exp_name, gpu_type, num_nodes, gpus_per_node, entrypoint,
     code_uri, code_hash, extra_env, extra_pip, metadata}

:class:`SubmitJobRequest` mirrors exactly those field names so client and server
agree on the wire format. The design doc (Component 2.2) sketched a slightly
different shape (``manifest_yaml`` / ``plan_yaml`` / ``mlflow_run_id`` /
``runtime_env_extras``); we follow the *client that actually exists*
(``platform_client.py``) as the source of truth. ``extra_pip`` is accepted (so
the client's payload validates) and threaded into ``runtime_env.pip``.

Dependency injection / testability
-----------------------------------
* :func:`get_ray_client` builds (and caches) a :class:`RayClusterClient` from
  the environment. Tests override it via
  ``app.dependency_overrides[get_ray_client] = lambda: fake_client``.
* :func:`_get_request_identity` adapts :func:`require_identity` (which takes a
  raw ``request``) into a properly ``Request``-typed FastAPI dependency so a
  missing/invalid token surfaces as a real ``401`` (not a ``422`` query-param
  error).
* The retry backoff goes through :func:`time.sleep`; tests monkeypatch
  ``jobs.time.sleep`` (or set the tiny backoff constant to ``0``) to avoid real
  delays.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Iterator, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .auth import Identity, require_identity
from .ray_client import RayClientError, RayClusterClient

# Entry point Ray runs on the head node after unpacking ``working_dir``. The
# in-cluster driver reconstructs manifest/plan from env vars (see design 2.2).
_DEFAULT_ENTRYPOINT = "python -m raytrain.entrypoint.driver --from-env"

# Ray's runtime_env setup must finish within this many seconds (code download +
# unpack). Matches the Phase 1 template value.
_SETUP_TIMEOUT_SECONDS = 600

# Upstream-retry policy. ``submit_failed`` is how :mod:`ray_client` normalises a
# transient dashboard / cluster failure; those are retried. Non-transient codes
# (e.g. ``unknown_gpu_type``) are never retried.
_MAX_SUBMIT_ATTEMPTS = 3
_RETRYABLE_CODES = frozenset({"submit_failed"})
# Small backoff between attempts. Kept tiny and monkeypatchable so tests don't
# sleep for real; patch ``jobs.time.sleep`` or set this to ``0``.
_RETRY_BACKOFF_SECONDS = 0.2

# Structured audit log. Emitted at INFO on logger ``raytrain.audit``; tests
# assert via ``caplog``. The event dict is JSON-serialised into the message so
# the submission_id / owner are present in ``caplog.text``.
_AUDIT_LOGGER = logging.getLogger("raytrain.audit")

router = APIRouter()


# --------------------------------------------------------------------------- #
# Request / response models (pydantic v2)
# --------------------------------------------------------------------------- #
class SubmitJobRequest(BaseModel):
    """Body of ``POST /v1/jobs``.

    Field names mirror :meth:`raytrain.platform_client.PlatformClient.submit_job`
    so the CLI client and this server speak the same JSON.
    """

    gpu_type: str
    entrypoint: Optional[str] = None
    code_uri: Optional[str] = None
    code_hash: Optional[str] = None
    num_nodes: int = 1
    gpus_per_node: int = 1
    extra_env: Dict[str, str] = Field(default_factory=dict)
    extra_pip: List[str] = Field(default_factory=list)
    metadata: Dict[str, str] = Field(default_factory=dict)
    # Optional descriptors used only to build a friendly submission id.
    repo: Optional[str] = None
    exp_name: Optional[str] = None


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #
# Module-level cache for the shared-cluster client. Built lazily from env on
# first use; ``None`` until then. Tests bypass this via dependency_overrides.
_ray_client_singleton: Optional[RayClusterClient] = None


def get_ray_client() -> RayClusterClient:
    """FastAPI dependency: return a cached :class:`RayClusterClient`.

    Built once from the environment (``RAYTRAIN_SHARED_CLUSTERS`` /
    ``RAYTRAIN_CLUSTER_URL_<GPU>``). Tests override this provider::

        app.dependency_overrides[get_ray_client] = lambda: fake_client
    """
    global _ray_client_singleton
    if _ray_client_singleton is None:
        _ray_client_singleton = RayClusterClient.from_env()
    return _ray_client_singleton


def _get_request_identity(request: Request) -> Identity:
    """``Request``-typed wrapper around :func:`require_identity`.

    :func:`require_identity` is annotated ``request: Any``, which FastAPI would
    otherwise treat as a query parameter. Wrapping it in a dependency with an
    explicit ``Request`` annotation makes FastAPI inject the live request, so a
    missing/invalid token becomes a proper ``401`` (via the ``HTTPException``
    that ``require_identity`` raises).
    """
    return require_identity(request)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _build_runtime_env(
    req: SubmitJobRequest, identity: Optional[Identity] = None
) -> dict:
    """Assemble Ray's ``runtime_env`` from the request.

    ``working_dir`` is included only when ``code_uri`` is set. Numeric values
    are stringified because Ray env_vars must be strings.

    Multi-tenant injection (task 9.3): when the caller's ``identity.tenant`` is
    set, ``RAYTRAIN_TENANT`` is injected into the job's ``env_vars`` so the
    in-cluster driver knows which tenant it runs as (mirrors design 2.2's
    ``runtime_env``). When there is no tenant claim, the key is omitted so the
    env stays clean (and existing single-tenant behaviour is unchanged).
    """
    env_vars: Dict[str, str] = {
        "TRAIN_NODES": str(req.num_nodes),
        "TRAIN_GPUS_PER_NODE": str(req.gpus_per_node),
    }
    if req.code_uri:
        env_vars["RAYTRAIN_CODE_URI"] = req.code_uri
    if req.code_hash:
        env_vars["RAYTRAIN_CODE_HASH"] = req.code_hash
    # Caller-supplied env overrides / extends the base set.
    env_vars.update({str(k): str(v) for k, v in (req.extra_env or {}).items()})
    # Token-derived tenant is authoritative: inject AFTER extra_env so a caller
    # cannot spoof a different tenant via ``extra_env['RAYTRAIN_TENANT']``.
    if identity is not None and identity.tenant:
        env_vars["RAYTRAIN_TENANT"] = str(identity.tenant)

    runtime_env: Dict[str, object] = {
        "env_vars": env_vars,
        "config": {"setup_timeout_seconds": _SETUP_TIMEOUT_SECONDS},
    }
    if req.code_uri:
        runtime_env["working_dir"] = req.code_uri
    if req.extra_pip:
        runtime_env["pip"] = list(req.extra_pip)
    return runtime_env


def _build_metadata(req: SubmitJobRequest, identity: Identity) -> Dict[str, str]:
    """Build job metadata: caller-supplied plus creator / tenant / gpu_type."""
    metadata: Dict[str, str] = {str(k): str(v) for k, v in (req.metadata or {}).items()}
    metadata["creator"] = identity.user
    metadata["gpu_type"] = req.gpu_type
    if identity.tenant:
        metadata["tenant"] = identity.tenant
    return metadata


_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _make_submission_id(req: SubmitJobRequest, identity: Identity) -> Optional[str]:
    """Build a friendly ``{user}-{repo}-{exp_name}-{stamp}`` submission id.

    Returns ``None`` when ``repo``/``exp_name`` are absent so Ray assigns its
    own id. The returned value of ``submit_job`` is always what we report back,
    so this is best-effort naming only.
    """
    if not req.repo or not req.exp_name:
        return None
    stamp = datetime.now(tz=timezone.utc).strftime("%y%m%d-%H%M%S")
    raw = f"{identity.user}-{req.repo}-{req.exp_name}-{stamp}"
    return _SANITIZE_RE.sub("-", raw).strip("-") or None


def _cluster_for(ray: RayClusterClient, gpu_type: str) -> str:
    """Best-effort cluster identifier for the response.

    Returns the dashboard URL when the client exposes its mapping, else falls
    back to the ``gpu_type`` string (fakes in tests need not expose internals).
    """
    urls = getattr(ray, "_cluster_urls", None)
    if isinstance(urls, dict):
        url = urls.get(str(gpu_type).lower())
        if url:
            return url
    return gpu_type


def _is_retryable(exc: RayClientError) -> bool:
    """Whether ``exc`` represents a transient upstream failure worth retrying."""
    return getattr(exc, "code", None) in _RETRYABLE_CODES


def _submit_with_retry(ray: RayClusterClient, **kwargs) -> str:
    """Call ``ray.submit_job`` with up to :data:`_MAX_SUBMIT_ATTEMPTS` tries.

    Retries only on transient (:func:`_is_retryable`) :class:`RayClientError`.
    Non-transient errors propagate immediately. After the final attempt the
    last error is re-raised for the route to map to a 5xx.
    """
    last_exc: Optional[RayClientError] = None
    for attempt in range(1, _MAX_SUBMIT_ATTEMPTS + 1):
        try:
            return ray.submit_job(**kwargs)
        except RayClientError as exc:
            if not _is_retryable(exc):
                raise
            last_exc = exc
            if attempt < _MAX_SUBMIT_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
    # Exhausted all attempts on a retryable failure.
    assert last_exc is not None  # loop ran at least once
    raise last_exc


def _audit_log(event: dict) -> None:
    """Emit a structured audit record on the ``raytrain.audit`` logger.

    The event is JSON-serialised into the log message so test ``caplog``
    assertions (and log scrapers) can match fields like ``submission_id`` and
    ``owner`` in the text. Deliberately a no-DB, stdlib-logging implementation.
    """
    _AUDIT_LOGGER.info("job_submit %s", json.dumps(event, sort_keys=True, default=str))


# --------------------------------------------------------------------------- #
# Multi-tenant isolation (task 9.3)
# --------------------------------------------------------------------------- #
# Server setting read from env ``RAYTRAIN_TENANT_ISOLATION``:
#
#   * ``"strict"`` -- operations on an EXISTING job (logs / delete / list) are
#     allowed only when the caller's ``identity.tenant`` matches the job's
#     ``metadata['tenant']``; a mismatch is a 403 ``tenant_forbidden``.
#   * ``"off"`` (default, and anything other than "strict") -- the check is
#     skipped entirely; behaviour is exactly as before this task.
#
# Default is "off" for backward-compatibility: the existing server tests
# (test_server_submit.py / test_server_logs.py) submit/read jobs without setting
# up matching tenants, and turning isolation on by default would break them.
# Multi-tenant deployments opt in by setting RAYTRAIN_TENANT_ISOLATION=strict.
_TENANT_ISOLATION_ENV = "RAYTRAIN_TENANT_ISOLATION"


def _tenant_isolation_mode() -> str:
    """Return the active tenant-isolation mode: ``"strict"`` or ``"off"``.

    Read from env (monkeypatchable in tests); anything other than a
    case-insensitive ``"strict"`` resolves to ``"off"``.
    """
    raw = os.environ.get(_TENANT_ISOLATION_ENV, "").strip().lower()
    return "strict" if raw == "strict" else "off"


def _job_tenant(
    ray: RayClusterClient, gpu_type: Optional[str], submission_id: str
) -> Optional[str]:
    """Look up the owning tenant of an existing job from its Ray metadata.

    submit_job records the tenant under ``metadata['tenant']``. We discover it
    via ``RayClusterClient.get_job_info`` (preferred, single-job lookup) and
    fall back to scanning ``list_jobs`` when ``get_job_info`` is unavailable
    (e.g. a minimal fake). gpu_type resolution mirrors the other existing-job
    operations: an explicit value pins the cluster, otherwise each configured
    cluster is tried until one resolves the job. Returns ``None`` when the job
    (or its tenant) cannot be determined -- the caller decides how to treat an
    unknown tenant under strict mode.
    """
    candidates = [gpu_type] if gpu_type else _configured_gpu_types(ray)
    for gt in candidates:
        if gt is None:
            continue
        # Preferred path: a direct single-job info lookup.
        get_info = getattr(ray, "get_job_info", None)
        if callable(get_info):
            try:
                info = get_info(gt, submission_id)
            except RayClientError:
                info = None
            if isinstance(info, dict):
                meta = info.get("metadata")
                if isinstance(meta, dict) and meta.get("tenant") is not None:
                    return str(meta.get("tenant"))
        # Fallback: scan the cluster's job list for a matching submission_id.
        try:
            for job in ray.list_jobs(gt):
                if not isinstance(job, dict):
                    continue
                if job.get("submission_id") == submission_id:
                    meta = job.get("metadata")
                    if isinstance(meta, dict) and meta.get("tenant") is not None:
                        return str(meta.get("tenant"))
        except RayClientError:
            continue
    return None


def _enforce_tenant_access(
    ray: RayClusterClient,
    gpu_type: Optional[str],
    submission_id: str,
    identity: Identity,
) -> None:
    """Guard an existing-job operation under strict tenant isolation.

    No-op unless :func:`_tenant_isolation_mode` is ``"strict"``. Under strict
    mode, the job's tenant (from its metadata) must equal the caller's
    ``identity.tenant``; any mismatch raises ``HTTPException(403)`` with
    ``{code: "tenant_forbidden", message: ...}``. A job whose tenant cannot be
    determined, or a caller without a tenant claim, is likewise rejected so the
    guard fails closed.
    """
    if _tenant_isolation_mode() != "strict":
        return
    job_tenant = _job_tenant(ray, gpu_type, submission_id)
    caller_tenant = identity.tenant
    if caller_tenant is not None and job_tenant is not None and caller_tenant == job_tenant:
        return
    raise HTTPException(
        status_code=403,
        detail={
            "code": "tenant_forbidden",
            "message": (
                f"tenant {caller_tenant!r} may not access job "
                f"{submission_id!r} owned by tenant {job_tenant!r}"
            ),
        },
    )


# --------------------------------------------------------------------------- #
# gpu_type resolution for existing-submission operations (logs / delete / list)
# --------------------------------------------------------------------------- #
# A submission_id taken from the URL does NOT carry its gpu_type, but every
# RayClusterClient method is keyed by gpu_type (it routes to a per-gpu-type
# shared cluster). We resolve the gpu_type pragmatically:
#
#   * If the caller passes ``?gpu_type=`` we use exactly that cluster.
#   * Otherwise we try each *configured* gpu_type in turn until one call
#     succeeds (does not raise ``RayClientError``). This keeps the API usable
#     without forcing the client to remember which cluster a job lives on, and
#     stays trivially testable against a mocked RayClusterClient.
def _configured_gpu_types(ray: RayClusterClient) -> List[str]:
    """Best-effort list of gpu_types the client knows about.

    Prefers the public ``gpu_types`` property; falls back to the
    ``_cluster_urls`` mapping (which test fakes expose). Returns ``[]`` when
    neither is available.
    """
    gpu_types = getattr(ray, "gpu_types", None)
    if isinstance(gpu_types, (list, tuple)) and gpu_types:
        return [str(g) for g in gpu_types]
    urls = getattr(ray, "_cluster_urls", None)
    if isinstance(urls, dict) and urls:
        return [str(k) for k in urls.keys()]
    return []


def _raise_ray_http(exc: RayClientError) -> "HTTPException":
    """Map a :class:`RayClientError` to an ``HTTPException`` and raise it.

    ``unknown_gpu_type`` is a client error (400); every other code is treated
    as an upstream failure (502). Detail shape matches ``submit_job``:
    ``{code, message}``.
    """
    code = getattr(exc, "code", "ray_error")
    message = getattr(exc, "message", str(exc))
    status_code = 400 if code == "unknown_gpu_type" else 502
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _try_over_gpu_types(
    ray: RayClusterClient, gpu_type: Optional[str], op: Callable[[str], object]
) -> tuple:
    """Run ``op(gpu_type)`` against the resolved cluster(s).

    When ``gpu_type`` is supplied, ``op`` is invoked once for that type. When it
    is ``None``, each configured gpu_type is tried in order until ``op`` returns
    without raising :class:`RayClientError`. Returns ``(resolved_gpu_type,
    result)``. If every candidate raises, the last error is mapped to an
    ``HTTPException`` (400 / 502). Raises a 400 when no clusters are configured.
    """
    candidates = [gpu_type] if gpu_type else _configured_gpu_types(ray)
    if not candidates:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "no_clusters_configured",
                "message": "no shared clusters configured to resolve this job",
            },
        )
    last_exc: Optional[RayClientError] = None
    for gt in candidates:
        try:
            return gt, op(gt)
        except RayClientError as exc:
            last_exc = exc
            continue
    assert last_exc is not None  # candidates is non-empty
    raise _raise_ray_http(last_exc)


# --------------------------------------------------------------------------- #
# Route
# --------------------------------------------------------------------------- #
@router.post("/v1/jobs")
def submit_job(
    req: SubmitJobRequest,
    identity: Identity = Depends(_get_request_identity),
    ray: RayClusterClient = Depends(get_ray_client),
) -> dict:
    """Submit a job to the shared cluster for ``req.gpu_type``.

    * 200 + ``{submission_id, gpu_type, cluster}`` on success.
    * 400 ``{code, message}`` for an unknown gpu_type.
    * 401 for a missing/invalid token (raised by the auth dependency).
    * 502 ``{code, message}`` when the upstream submit fails after retries.
    """
    entrypoint = req.entrypoint or _DEFAULT_ENTRYPOINT
    runtime_env = _build_runtime_env(req, identity)
    metadata = _build_metadata(req, identity)
    submission_id_hint = _make_submission_id(req, identity)

    ts = datetime.now(tz=timezone.utc).isoformat()
    base_event = {
        "owner": identity.user,
        "tenant": identity.tenant,
        "gpu_type": req.gpu_type,
        "code_uri": req.code_uri,
        "code_hash": req.code_hash,
        "num_nodes": req.num_nodes,
        "gpus_per_node": req.gpus_per_node,
        "ts": ts,
    }

    try:
        submission_id = _submit_with_retry(
            ray,
            gpu_type=req.gpu_type,
            entrypoint=entrypoint,
            runtime_env=runtime_env,
            metadata=metadata,
            submission_id=submission_id_hint,
        )
    except RayClientError as exc:
        code = getattr(exc, "code", "submit_failed")
        message = getattr(exc, "message", str(exc))
        # Unknown gpu_type is a client error; everything else is upstream (5xx).
        status_code = 400 if code == "unknown_gpu_type" else 502
        _audit_log({**base_event, "outcome": "error", "code": code, "message": message})
        raise HTTPException(status_code=status_code, detail={"code": code, "message": message})

    _audit_log({**base_event, "outcome": "submitted", "submission_id": submission_id})

    return {
        "submission_id": submission_id,
        "gpu_type": req.gpu_type,
        "cluster": _cluster_for(ray, req.gpu_type),
        # CLI (`_submit_shared`) reads `cluster_address`; include for compat.
        "cluster_address": _cluster_for(ray, req.gpu_type),
    }


# --------------------------------------------------------------------------- #
# GET /v1/jobs/{submission_id}/logs -- SSE log stream
# --------------------------------------------------------------------------- #
@router.get("/v1/jobs/{submission_id}/logs")
def stream_logs(
    submission_id: str,
    request: Request,
    gpu_type: Optional[str] = Query(default=None),
    identity: Identity = Depends(_get_request_identity),
    ray: RayClusterClient = Depends(get_ray_client),
) -> StreamingResponse:
    """Stream a job's logs as Server-Sent Events (``text/event-stream``).

    Each log chunk from ``RayClusterClient.tail_logs`` is emitted as an SSE
    ``data:`` frame (``f"data: {chunk}\\n\\n"``). The upstream iterator is
    resolved eagerly (so ``unknown_gpu_type`` -> 400 and other ray errors -> 502
    surface as a proper HTTP status *before* the stream starts), then chunks are
    produced lazily. The generator terminates cleanly when the upstream iterator
    is exhausted or raises, so a closed/timed-out stream never hangs.

    gpu_type resolution: an explicit ``?gpu_type=`` selects the cluster; when
    omitted each configured gpu_type is tried until ``tail_logs`` resolves.

    * 200 ``text/event-stream`` on success.
    * 400 ``{code, message}`` for an unknown gpu_type.
    * 401 for a missing/invalid token (auth dependency).
    * 502 ``{code, message}`` for other upstream failures.
    """
    # Tenant isolation (task 9.3): under strict mode, reject cross-tenant log
    # access BEFORE touching the upstream cluster so a forbidden caller never
    # triggers tail_logs. No-op when isolation is off (the default).
    _enforce_tenant_access(ray, gpu_type, submission_id, identity)

    # Resolve the cluster + obtain the (lazy) upstream iterator eagerly so any
    # routing/connection error becomes an HTTP status before streaming begins.
    # ``tail_logs`` resolves the client eagerly but only iterates lazily, so the
    # unknown_gpu_type error is raised here, not mid-stream.
    _resolved_gpu_type, log_iter = _try_over_gpu_types(
        ray, gpu_type, lambda gt: ray.tail_logs(gt, submission_id)
    )

    def _event_stream() -> Iterator[str]:
        # Lazily forward each upstream chunk as an SSE data frame. Terminates
        # when the upstream iterator is exhausted; on an upstream error we stop
        # cleanly (the HTTP status is already 200, so emit a terminal comment
        # frame rather than crash the connection).
        try:
            for chunk in log_iter:
                yield f"data: {chunk}\n\n"
        except RayClientError:
            # Upstream failed mid-stream after headers were sent; end the SSE
            # stream gracefully instead of propagating (which would 500).
            yield "event: error\ndata: log stream interrupted\n\n"
            return

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# DELETE /v1/jobs/{submission_id} -- stop a running job
# --------------------------------------------------------------------------- #
@router.delete("/v1/jobs/{submission_id}")
def delete_job(
    submission_id: str,
    gpu_type: Optional[str] = Query(default=None),
    identity: Identity = Depends(_get_request_identity),
    ray: RayClusterClient = Depends(get_ray_client),
) -> dict:
    """Stop ``submission_id`` and report whether the stop was accepted.

    Calls ``RayClusterClient.stop_job(gpu_type, submission_id)`` and returns
    ``{"submission_id", "stopped": <bool>, "gpu_type"}``. Writes an audit log
    entry recording owner + submission_id + outcome.

    gpu_type resolution mirrors the logs endpoint (explicit ``?gpu_type=`` or
    try-each-configured).

    * 200 ``{submission_id, stopped, gpu_type}`` on success.
    * 400 ``{code, message}`` for an unknown gpu_type.
    * 401 for a missing/invalid token (auth dependency).
    * 502 ``{code, message}`` for other upstream failures.
    """
    ts = datetime.now(tz=timezone.utc).isoformat()
    base_event = {
        "owner": identity.user,
        "tenant": identity.tenant,
        "submission_id": submission_id,
        "action": "stop",
        "ts": ts,
    }
    try:
        # Tenant isolation (task 9.3): under strict mode, reject a cross-tenant
        # stop before calling stop_job. No-op when isolation is off (default).
        _enforce_tenant_access(ray, gpu_type, submission_id, identity)
        resolved_gpu_type, stopped = _try_over_gpu_types(
            ray, gpu_type, lambda gt: ray.stop_job(gt, submission_id)
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        _audit_log({**base_event, "outcome": "error", **detail})
        raise

    _audit_log(
        {
            **base_event,
            "gpu_type": resolved_gpu_type,
            "outcome": "stopped",
            "stopped": bool(stopped),
        }
    )
    return {
        "submission_id": submission_id,
        "stopped": bool(stopped),
        "gpu_type": resolved_gpu_type,
    }


# --------------------------------------------------------------------------- #
# GET /v1/jobs -- list jobs (optionally filtered by owner)
# --------------------------------------------------------------------------- #
def _job_owner(job: dict) -> Optional[str]:
    """Extract the owner of a job from its metadata.

    submit_job stores the submitter under metadata ``creator``; we also accept
    an ``owner`` key for forward compatibility.
    """
    metadata = job.get("metadata") if isinstance(job, dict) else None
    if isinstance(metadata, dict):
        return metadata.get("creator") or metadata.get("owner")
    return None


def _job_tenant_of(job: dict) -> Optional[str]:
    """Extract the owning tenant of a listed job from its metadata.

    submit_job stores the tenant under metadata ``tenant`` (see
    :func:`_build_metadata`). Returns ``None`` when absent.
    """
    metadata = job.get("metadata") if isinstance(job, dict) else None
    if isinstance(metadata, dict):
        tenant = metadata.get("tenant")
        return None if tenant is None else str(tenant)
    return None


@router.get("/v1/jobs")
def list_jobs(
    owner: Optional[str] = Query(default=None),
    identity: Identity = Depends(_get_request_identity),
    ray: RayClusterClient = Depends(get_ray_client),
) -> List[dict]:
    """List jobs aggregated across all configured shared clusters.

    Owner filtering (documented default): when ``?owner=`` is given, only jobs
    whose metadata ``creator`` equals that value are returned. When ``owner`` is
    omitted, the result is filtered to the *authenticated caller's own* jobs
    (``identity.user``) -- a safe default that avoids leaking other tenants'
    jobs by accident.

    Each item is a dict with at least ``submission_id``, ``status`` and
    ``metadata`` (plus ``gpu_type`` / ``job_id`` / ``entrypoint`` when known).

    * 200 JSON list on success.
    * 401 for a missing/invalid token (auth dependency).
    * 502 ``{code, message}`` if every configured cluster fails to list.
    """
    target_owner = owner if owner is not None else identity.user

    gpu_types = _configured_gpu_types(ray)
    aggregated: List[dict] = []
    errors: List[RayClientError] = []
    for gt in gpu_types:
        try:
            aggregated.extend(ray.list_jobs(gt))
        except RayClientError as exc:
            errors.append(exc)
            continue

    # Only surface an error if every cluster failed and none returned jobs.
    if gpu_types and not aggregated and errors and len(errors) == len(gpu_types):
        _raise_ray_http(errors[-1])

    owned = [job for job in aggregated if _job_owner(job) == target_owner]

    # Tenant isolation (task 9.3): under strict mode, additionally restrict the
    # listing to the caller's own tenant so jobs from other tenants are never
    # surfaced (even if an owner filter would otherwise match). No-op when
    # isolation is off (the default) -- preserves the existing behaviour.
    if _tenant_isolation_mode() == "strict":
        caller_tenant = identity.tenant
        owned = [job for job in owned if _job_tenant_of(job) == caller_tenant]

    return owned
