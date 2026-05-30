"""Token verification for the raytrain submission server.

A single entry point :func:`verify_token` authenticates an incoming request and
returns an :class:`Identity`. Two token kinds are supported through one door:

* **raytrain self-signed JWT** -- symmetric ``HS256`` signature. The signing
  secret comes from the ``RAYTRAIN_JWT_SECRET`` environment variable. The
  subject (``sub`` claim) is the username; an optional ``tenant`` claim carries
  the tenant; ``iss`` is the issuer (e.g. ``"raytrain"``).

* **OIDC ID Token** -- asymmetric signature (``RS256`` / ``ES256`` / ...) issued
  by a company SSO. The issuer and JWKS URI come from the environment
  (``RAYTRAIN_OIDC_ISSUER`` / ``RAYTRAIN_OIDC_JWKS_URI`` and the optional
  ``RAYTRAIN_OIDC_AUDIENCE``). Signatures are verified against the public keys
  published at the JWKS URI.

Path-selection rule
-------------------
The decision is driven by the token's *unverified* JWT header ``alg`` field
(cheap to read, no signature trust required):

* ``alg == "HS256"`` -> raytrain self-signed path.
* any other ``alg`` (``RS256``, ``ES256``, ...) -> OIDC path.

This is a clear, testable rule: a symmetric secret can only validate ``HS256``,
and OIDC providers sign with asymmetric keys, so the algorithm family
unambiguously identifies the issuer category. A malformed/unreadable header is
an auth failure.

Error handling
--------------
Any failure raises :class:`AuthError`, which carries an HTTP ``status_code``
(401), a machine-readable ``code`` and a human ``message``. The API layer can
map this exception however it likes. For convenience as a FastAPI route
dependency, :func:`require_identity` wraps :func:`verify_token` and converts
:class:`AuthError` into ``fastapi.HTTPException(status_code=401, ...)``.

The OIDC public-key lookup is isolated behind :func:`_get_oidc_signing_key` so
unit tests can monkeypatch it and exercise the OIDC path without network access.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import jwt

# Algorithms accepted on the OIDC path. HS256 is intentionally excluded here so
# that a token claiming an asymmetric issuer can never be validated with the
# shared symmetric secret (algorithm-confusion defense).
_OIDC_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]

# Default issuer attributed to self-signed tokens that omit an ``iss`` claim.
_DEFAULT_SELF_ISSUER = "raytrain"


class AuthError(Exception):
    """Authentication failure.

    Carries enough structured detail for an API layer to render a response:

    * :attr:`status_code` -- HTTP status (always 401 for now).
    * :attr:`code` -- short machine-readable code (e.g. ``"token_expired"``).
    * :attr:`message` -- human-readable description.
    """

    def __init__(self, code: str, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AuthError(code={self.code!r}, message={self.message!r}, status_code={self.status_code})"


@dataclass
class Identity:
    """An authenticated principal.

    Attributes:
        user: The subject / username. For self-signed tokens this is ``sub``;
            for OIDC it is ``preferred_username`` then ``email`` then ``sub``.
        issuer: The token issuer (``iss`` claim).
        tenant: Optional tenant, from a ``tenant`` claim when present. Consumed
            by later tasks for multi-tenant isolation.
        email: Optional email (``email`` claim) when present.
        groups: Optional list of groups (``groups`` claim) when present.
        raw_claims: The full set of verified claims, for downstream use.
    """

    user: str
    issuer: str
    tenant: Optional[str] = None
    email: Optional[str] = None
    groups: Optional[List[str]] = None
    raw_claims: Dict[str, Any] = field(default_factory=dict)


def _extract_bearer_token(req: Any) -> str:
    """Pull the bearer token out of the ``Authorization`` header.

    Raises :class:`AuthError` if the header is missing or not a well-formed
    ``Bearer <token>`` value.
    """
    # Starlette/FastAPI Request exposes a case-insensitive ``.headers`` mapping.
    headers = getattr(req, "headers", None)
    if headers is None:
        raise AuthError("missing_authorization", "request has no headers")

    auth_header = headers.get("authorization") or headers.get("Authorization")
    if not auth_header:
        raise AuthError("missing_authorization", "missing Authorization header")

    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthError(
            "malformed_authorization",
            "Authorization header must be 'Bearer <token>'",
        )
    return parts[1].strip()


def _peek_alg(token: str) -> str:
    """Read the unverified JWT header and return its ``alg`` (uppercased).

    Raises :class:`AuthError` if the token is not a parseable JWT.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise AuthError("malformed_token", f"cannot parse token header: {exc}") from exc
    alg = header.get("alg")
    if not alg:
        raise AuthError("malformed_token", "token header has no 'alg'")
    return str(alg).upper()


def _verify_self_signed(token: str) -> Identity:
    """Verify a raytrain self-signed HS256 token."""
    secret = os.environ.get("RAYTRAIN_JWT_SECRET")
    if not secret:
        raise AuthError(
            "server_misconfigured",
            "RAYTRAIN_JWT_SECRET is not configured",
        )
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token_expired", "token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("invalid_token", f"invalid self-signed token: {exc}") from exc

    sub = claims.get("sub")
    if not sub:
        raise AuthError("invalid_token", "token is missing 'sub' claim")

    groups = claims.get("groups")
    return Identity(
        user=str(sub),
        issuer=str(claims.get("iss") or _DEFAULT_SELF_ISSUER),
        tenant=claims.get("tenant"),
        email=claims.get("email"),
        groups=list(groups) if isinstance(groups, (list, tuple)) else None,
        raw_claims=claims,
    )


def _get_oidc_signing_key(token: str):
    """Resolve the OIDC signing key for ``token`` from the configured JWKS URI.

    Isolated so tests can monkeypatch it: a test generates an RSA keypair,
    signs a token with the private key, and patches this function to return the
    matching public key (no network access required).

    Returns the key object/bytes that :func:`jwt.decode` accepts as ``key``.
    Raises :class:`AuthError` on configuration or fetch failure.
    """
    jwks_uri = os.environ.get("RAYTRAIN_OIDC_JWKS_URI")
    if not jwks_uri:
        raise AuthError(
            "server_misconfigured",
            "RAYTRAIN_OIDC_JWKS_URI is not configured",
        )
    try:
        jwk_client = jwt.PyJWKClient(jwks_uri)
        signing_key = jwk_client.get_signing_key_from_jwt(token)
    except Exception as exc:  # noqa: BLE001 - any JWKS failure is an auth failure
        raise AuthError(
            "jwks_fetch_failed",
            f"failed to resolve OIDC signing key: {exc}",
        ) from exc
    # PyJWKClient returns a PyJWK whose ``.key`` is the public key object.
    return getattr(signing_key, "key", signing_key)


def _verify_oidc(token: str) -> Identity:
    """Verify an OIDC ID Token against the configured issuer / JWKS keys."""
    issuer = os.environ.get("RAYTRAIN_OIDC_ISSUER")
    if not issuer:
        raise AuthError(
            "server_misconfigured",
            "RAYTRAIN_OIDC_ISSUER is not configured",
        )
    audience = os.environ.get("RAYTRAIN_OIDC_AUDIENCE")

    key = _get_oidc_signing_key(token)

    options = {"require": ["exp"], "verify_aud": audience is not None}
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=_OIDC_ALGORITHMS,
            issuer=issuer,
            audience=audience if audience else None,
            options=options,
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token_expired", "token has expired") from exc
    except jwt.InvalidIssuerError as exc:
        raise AuthError("invalid_issuer", f"unknown issuer: {exc}") from exc
    except jwt.InvalidAudienceError as exc:
        raise AuthError("invalid_audience", f"bad audience: {exc}") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("invalid_token", f"invalid OIDC token: {exc}") from exc

    # Username preference: preferred_username -> email -> sub.
    user = (
        claims.get("preferred_username")
        or claims.get("email")
        or claims.get("sub")
    )
    if not user:
        raise AuthError(
            "invalid_token",
            "token has no preferred_username/email/sub claim",
        )

    groups = claims.get("groups")
    return Identity(
        user=str(user),
        issuer=str(claims.get("iss") or issuer),
        tenant=claims.get("tenant"),
        email=claims.get("email"),
        groups=list(groups) if isinstance(groups, (list, tuple)) else None,
        raw_claims=claims,
    )


def verify_token(req: Any) -> Identity:
    """Authenticate ``req`` and return the :class:`Identity`.

    ``req`` is a Starlette/FastAPI ``Request`` (anything exposing a
    case-insensitive ``.headers`` mapping works).

    The bearer token is extracted from ``Authorization: Bearer <token>``; the
    verification path is chosen from the unverified header ``alg`` (``HS256`` ->
    self-signed, otherwise OIDC). Any failure raises :class:`AuthError`.
    """
    token = _extract_bearer_token(req)
    alg = _peek_alg(token)
    if alg == "HS256":
        return _verify_self_signed(token)
    return _verify_oidc(token)


def require_identity(request: Any) -> Identity:
    """FastAPI dependency wrapper around :func:`verify_token`.

    Use as a route dependency::

        @app.post("/jobs")
        def create_job(identity: Identity = Depends(require_identity)):
            ...

    Converts :class:`AuthError` into ``fastapi.HTTPException(status_code=401)``
    with a structured ``detail`` so FastAPI renders a proper 401 response.
    """
    try:
        return verify_token(request)
    except AuthError as exc:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
