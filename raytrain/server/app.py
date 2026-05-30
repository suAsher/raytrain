"""Minimal FastAPI application for the raytrain submission server.

Run locally with::

    uvicorn raytrain.server.app:app

Exposes two operational probes:

* ``GET /healthz`` -- liveness. Returns 200 as long as the process is up.
* ``GET /readyz``  -- readiness. Returns 200 when all readiness checks pass,
  otherwise 503. For now there are no real checks (the server has no external
  dependencies yet); future phases (auth, Ray client, job store) register
  checks in :func:`_readiness_checks`.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse

try:  # pragma: no cover - trivial fallback
    from raytrain import __version__ as _VERSION
except Exception:  # pragma: no cover
    _VERSION = "0.1.0"

app = FastAPI(title="raytrain submission server", version=_VERSION)

# Job submission / lifecycle routes (POST /v1/jobs, ...). Kept in a separate
# module/router for clean layering; included here so the app exposes them.
from raytrain.server.jobs import router as jobs_router  # noqa: E402

app.include_router(jobs_router)


def _readiness_checks() -> dict[str, bool]:
    """Return readiness checks as ``{name: passed}``.

    Empty for now -- the server has no external dependencies yet. Future tasks
    add entries here (e.g. ``"ray"``, ``"auth"``); :func:`readyz` returns 503
    if any value is ``False``.
    """
    return {}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe. Always 200 if the process is serving requests."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    """Readiness probe. 200 when all checks pass, 503 otherwise."""
    checks = _readiness_checks()
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "failed": failed},
        )
    return JSONResponse(status_code=200, content={"status": "ready"})
