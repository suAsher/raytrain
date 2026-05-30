"""Liveness / readiness endpoints. No auth required."""
from __future__ import annotations

from fastapi import APIRouter

from .. import __version__

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Process-up probe. Returns 200 as long as we can serve HTTP."""
    return {"status": "ok", "version": __version__}


@router.get("/readyz")
def readyz() -> dict[str, str]:
    """Readiness probe. v1 returns OK unconditionally; v2 will check
    MinIO + at least one configured Ray cluster."""
    return {"status": "ready", "version": __version__}
