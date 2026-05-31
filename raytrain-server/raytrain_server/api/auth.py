"""
Auth endpoints.

- ``POST /v1/auth/login``  — username + password → signed JWT (browser login).
- ``GET  /v1/auth/me``     — introspect the caller's token.

Password login
--------------
Users created with a password (admin sets it, or the user changes it) can log
in with username/password; the server verifies the PBKDF2 hash and signs a JWT,
which the SPA stores and sends as a Bearer token thereafter. Token issuance via
the ``raytrain-issue-token`` CLI still exists for bootstrap / automation. OIDC
SSO can later be added as a second login branch.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..core.jwt_auth import Identity, issue_token, require_user
from ..core.settings import Settings, get_settings
from ..core.users import get_user_store, verify_password

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class LoginResponse(BaseModel):
    token: str
    expires_at: int
    user: str
    tenant: str
    role: str


class WhoAmIResponse(BaseModel):
    user: str
    tenant: str
    role: str
    issued_at: int
    expires_at: int


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    settings: Settings = Depends(get_settings),
) -> LoginResponse:
    """Verify username/password and return a signed JWT.

    Returns 401 for unknown user, no-password user, wrong password, or disabled
    account — with a single generic message so we don't leak which case it was.
    """
    rec = get_user_store().get(body.username)
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="用户名或密码错误",
    )
    if rec is None or not rec.has_password:
        raise invalid
    if not rec.enabled:
        # account exists but disabled — still generic to avoid enumeration,
        # but a distinct log line for ops.
        log.warning("auth.login.disabled user=%s", body.username)
        raise invalid
    if not verify_password(body.password, rec.password_hash):
        log.warning("auth.login.badpw user=%s", body.username)
        raise invalid

    token, exp = issue_token(
        user=rec.user, tenant=rec.tenant, role=rec.role, settings=settings
    )
    log.info("auth.login.ok user=%s role=%s", rec.user, rec.role)
    return LoginResponse(
        token=token, expires_at=exp, user=rec.user, tenant=rec.tenant, role=rec.role
    )


@router.get("/me", response_model=WhoAmIResponse)
def whoami(identity: Identity = Depends(require_user)) -> WhoAmIResponse:
    """Echo back the decoded token, for clients to verify their session."""
    return WhoAmIResponse(
        user=identity.user,
        tenant=identity.tenant,
        role=identity.role,
        issued_at=identity.issued_at,
        expires_at=identity.expires_at,
    )
