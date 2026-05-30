"""
JWT issuance + verification.

We use HS256 with a secret stored in K8s Secret. Tokens carry minimal claims:

    sub:    user identity (e.g. "zhangsan")
    tenant: tenant id (e.g. "occ-team")
    role:   "user" | "admin"
    iat:    issued-at (unix seconds)
    exp:    expiration (unix seconds)
    iss:    issuer (always settings.jwt_issuer)

Claims are validated on every request through FastAPI's ``Depends(require_user)``.
M5 will add OIDC by extending :func:`verify_token` with a second branch keyed
on issuer.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import jwt
from fastapi import Depends, Header, HTTPException, status

from .settings import Settings, get_settings


@dataclass(frozen=True)
class Identity:
    """Decoded subject of an authenticated request."""

    user: str
    tenant: str
    role: Literal["user", "admin"]
    issued_at: int
    expires_at: int

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# ---------------------------------------------------------------------------- #
# Issuance
# ---------------------------------------------------------------------------- #


def issue_token(
    user: str,
    tenant: str = "default",
    role: Literal["user", "admin"] = "user",
    ttl_days: int | None = None,
    settings: Settings | None = None,
) -> tuple[str, int]:
    """
    Sign a fresh JWT for ``user``. Returns (token, expires_at_unix).

    Raises ``ValueError`` for empty / malformed user.
    """
    if not user or not user.replace("-", "").replace("_", "").isalnum():
        raise ValueError(
            "user must be a non-empty alphanumeric / dash / underscore string"
        )

    s = settings or get_settings()
    ttl = ttl_days if ttl_days is not None else s.jwt_default_ttl_days
    if ttl <= 0:
        raise ValueError("ttl_days must be > 0")

    now = int(time.time())
    exp = now + ttl * 86400
    payload = {
        "sub": user,
        "tenant": tenant or "default",
        "role": role,
        "iat": now,
        "exp": exp,
        "iss": s.jwt_issuer,
    }
    token = jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)
    return token, exp


# ---------------------------------------------------------------------------- #
# Verification
# ---------------------------------------------------------------------------- #


def verify_token(token: str, settings: Settings | None = None) -> Identity:
    """
    Decode & validate a JWT. Raises ``HTTPException 401`` on any failure.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    s = settings or get_settings()
    try:
        payload = jwt.decode(
            token,
            s.jwt_secret,
            algorithms=[s.jwt_algorithm],
            issuer=s.jwt_issuer,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.InvalidTokenError as exc:
        # InvalidIssuer, InvalidSignature, DecodeError, MissingRequiredClaim,
        # etc. all funnel through here.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    role = payload.get("role", "user")
    if role not in ("user", "admin"):
        # If somebody managed to mint a token with an unknown role, treat as
        # least privilege.
        role = "user"

    return Identity(
        user=str(payload["sub"]),
        tenant=str(payload.get("tenant", "default")),
        role=role,  # type: ignore[arg-type]
        issued_at=int(payload["iat"]),
        expires_at=int(payload["exp"]),
    )


# ---------------------------------------------------------------------------- #
# FastAPI dependencies
# ---------------------------------------------------------------------------- #


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid Authorization header (expected 'Bearer <token>')",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return parts[1].strip()


def require_user(
    authorization: str | None = Header(default=None),
) -> Identity:
    """Standard dependency for any authenticated endpoint."""
    token = _extract_bearer(authorization)
    return verify_token(token)


def require_admin(
    identity: Identity = Depends(require_user),
) -> Identity:
    if not identity.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )
    return identity
