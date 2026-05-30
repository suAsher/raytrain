"""
Auth endpoints.

v1 only exposes ``/v1/auth/me`` (introspect own token). Token issuance is
admin-only and goes through the ``raytrain-issue-token`` CLI script that
talks to the same JWT secret.

Why not a /login endpoint?
    M1's auth is "raytrain admin signs you a 1-year JWT and hands it to
    you". A login endpoint would need a password store — out of scope and
    we'd rather skip straight to OIDC in M5.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..core.jwt_auth import Identity, require_user

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class WhoAmIResponse(BaseModel):
    user: str
    tenant: str
    role: str
    issued_at: int
    expires_at: int


@router.get("/me", response_model=WhoAmIResponse)
def whoami(identity: Identity = Depends(require_user)) -> WhoAmIResponse:
    """Echo back the decoded token. Useful for clients to verify their
    config (~/.raytrain/config.yaml) is wired up correctly."""
    return WhoAmIResponse(
        user=identity.user,
        tenant=identity.tenant,
        role=identity.role,
        issued_at=identity.issued_at,
        expires_at=identity.expires_at,
    )
