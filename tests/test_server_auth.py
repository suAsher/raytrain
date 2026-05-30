"""
Unit tests for raytrain/server/auth.py.

Covers the acceptance cases: 合法 (valid) / 过期 (expired) / 错签 (bad
signature) / OIDC mock.

Run with:
    PYTHONPATH=. python3 -m pytest tests/test_server_auth.py -v
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import jwt
import pytest

# Allow running directly: ROOT/raytrain importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from starlette.requests import Request  # noqa: E402

from raytrain.server import auth  # noqa: E402
from raytrain.server.auth import AuthError, Identity, verify_token  # noqa: E402

try:  # OIDC test needs real RSA keys
    from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

    _HAS_CRYPTO = True
except Exception:  # pragma: no cover
    _HAS_CRYPTO = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def make_request(authorization: str | None) -> Request:
    """Build a minimal Starlette Request carrying an Authorization header."""
    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("utf-8")))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/jobs",
        "headers": headers,
    }
    return Request(scope)


def make_request_with_token(token: str) -> Request:
    return make_request(f"Bearer {token}")


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Self-signed HS256 path
# --------------------------------------------------------------------------- #
def test_valid_self_signed(monkeypatch):
    secret = "super-secret-key-for-tests"
    monkeypatch.setenv("RAYTRAIN_JWT_SECRET", secret)

    token = jwt.encode(
        {
            "sub": "alice",
            "tenant": "team-a",
            "iss": "raytrain",
            "email": "alice@example.com",
            "groups": ["admins", "users"],
            "exp": _now() + dt.timedelta(hours=1),
        },
        secret,
        algorithm="HS256",
    )

    identity = verify_token(make_request_with_token(token))

    assert isinstance(identity, Identity)
    assert identity.user == "alice"
    assert identity.tenant == "team-a"
    assert identity.issuer == "raytrain"
    assert identity.email == "alice@example.com"
    assert identity.groups == ["admins", "users"]
    assert identity.raw_claims["sub"] == "alice"


def test_valid_self_signed_no_tenant(monkeypatch):
    secret = "another-secret"
    monkeypatch.setenv("RAYTRAIN_JWT_SECRET", secret)

    token = jwt.encode(
        {"sub": "bob", "exp": _now() + dt.timedelta(hours=1)},
        secret,
        algorithm="HS256",
    )

    identity = verify_token(make_request_with_token(token))
    assert identity.user == "bob"
    assert identity.tenant is None
    # No iss claim -> defaults to "raytrain".
    assert identity.issuer == "raytrain"


def test_expired_token(monkeypatch):
    secret = "secret-for-expiry"
    monkeypatch.setenv("RAYTRAIN_JWT_SECRET", secret)

    token = jwt.encode(
        {"sub": "alice", "exp": _now() - dt.timedelta(minutes=5)},
        secret,
        algorithm="HS256",
    )

    with pytest.raises(AuthError) as excinfo:
        verify_token(make_request_with_token(token))
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "token_expired"


def test_bad_signature(monkeypatch):
    monkeypatch.setenv("RAYTRAIN_JWT_SECRET", "the-real-secret")

    # Signed with a *different* secret.
    token = jwt.encode(
        {"sub": "alice", "exp": _now() + dt.timedelta(hours=1)},
        "a-totally-different-secret",
        algorithm="HS256",
    )

    with pytest.raises(AuthError) as excinfo:
        verify_token(make_request_with_token(token))
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "invalid_token"


def test_missing_header(monkeypatch):
    monkeypatch.setenv("RAYTRAIN_JWT_SECRET", "secret")
    with pytest.raises(AuthError) as excinfo:
        verify_token(make_request(None))
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "missing_authorization"


def test_malformed_header(monkeypatch):
    monkeypatch.setenv("RAYTRAIN_JWT_SECRET", "secret")
    for bad in ["Bearer", "Token abc", "Bearer ", "justsometoken"]:
        with pytest.raises(AuthError) as excinfo:
            verify_token(make_request(bad))
        assert excinfo.value.status_code == 401
        assert excinfo.value.code in {"malformed_authorization", "missing_authorization"}


def test_missing_secret_is_server_misconfig(monkeypatch):
    monkeypatch.delenv("RAYTRAIN_JWT_SECRET", raising=False)
    token = jwt.encode(
        {"sub": "alice", "exp": _now() + dt.timedelta(hours=1)},
        "whatever",
        algorithm="HS256",
    )
    with pytest.raises(AuthError) as excinfo:
        verify_token(make_request_with_token(token))
    assert excinfo.value.code == "server_misconfigured"


# --------------------------------------------------------------------------- #
# OIDC path (mocked JWKS)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _HAS_CRYPTO, reason="cryptography not installed")
def test_oidc_mock(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    issuer = "https://sso.example.com"
    audience = "raytrain"
    monkeypatch.setenv("RAYTRAIN_OIDC_ISSUER", issuer)
    monkeypatch.setenv("RAYTRAIN_OIDC_JWKS_URI", "https://sso.example.com/jwks")
    monkeypatch.setenv("RAYTRAIN_OIDC_AUDIENCE", audience)

    token = jwt.encode(
        {
            "sub": "user-uuid-123",
            "preferred_username": "carol",
            "email": "carol@example.com",
            "iss": issuer,
            "aud": audience,
            "tenant": "team-b",
            "groups": ["dev"],
            "exp": _now() + dt.timedelta(hours=1),
        },
        private_key,
        algorithm="RS256",
    )

    # Isolate the key resolver: return the public key directly, no network.
    monkeypatch.setattr(auth, "_get_oidc_signing_key", lambda _token: public_key)

    identity = verify_token(make_request_with_token(token))

    # preferred_username wins over email/sub.
    assert identity.user == "carol"
    assert identity.issuer == issuer
    assert identity.tenant == "team-b"
    assert identity.email == "carol@example.com"
    assert identity.groups == ["dev"]


@pytest.mark.skipif(not _HAS_CRYPTO, reason="cryptography not installed")
def test_oidc_username_falls_back_to_email_then_sub(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    issuer = "https://sso.example.com"
    monkeypatch.setenv("RAYTRAIN_OIDC_ISSUER", issuer)
    monkeypatch.setenv("RAYTRAIN_OIDC_JWKS_URI", "https://sso.example.com/jwks")
    monkeypatch.delenv("RAYTRAIN_OIDC_AUDIENCE", raising=False)
    monkeypatch.setattr(auth, "_get_oidc_signing_key", lambda _token: public_key)

    # No preferred_username -> email.
    token_email = jwt.encode(
        {
            "sub": "uuid",
            "email": "dave@example.com",
            "iss": issuer,
            "exp": _now() + dt.timedelta(hours=1),
        },
        private_key,
        algorithm="RS256",
    )
    assert verify_token(make_request_with_token(token_email)).user == "dave@example.com"

    # Neither -> sub.
    token_sub = jwt.encode(
        {"sub": "uuid-only", "iss": issuer, "exp": _now() + dt.timedelta(hours=1)},
        private_key,
        algorithm="RS256",
    )
    assert verify_token(make_request_with_token(token_sub)).user == "uuid-only"


@pytest.mark.skipif(not _HAS_CRYPTO, reason="cryptography not installed")
def test_oidc_bad_issuer_rejected(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    monkeypatch.setenv("RAYTRAIN_OIDC_ISSUER", "https://sso.example.com")
    monkeypatch.setenv("RAYTRAIN_OIDC_JWKS_URI", "https://sso.example.com/jwks")
    monkeypatch.delenv("RAYTRAIN_OIDC_AUDIENCE", raising=False)
    monkeypatch.setattr(auth, "_get_oidc_signing_key", lambda _token: public_key)

    token = jwt.encode(
        {
            "sub": "carol",
            "iss": "https://evil.example.com",
            "exp": _now() + dt.timedelta(hours=1),
        },
        private_key,
        algorithm="RS256",
    )
    with pytest.raises(AuthError) as excinfo:
        verify_token(make_request_with_token(token))
    assert excinfo.value.code == "invalid_issuer"


@pytest.mark.skipif(not _HAS_CRYPTO, reason="cryptography not installed")
def test_oidc_bad_signature_rejected(monkeypatch):
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # A *different* keypair whose public key we hand to the verifier.
    other_public = rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    ).public_key()

    issuer = "https://sso.example.com"
    monkeypatch.setenv("RAYTRAIN_OIDC_ISSUER", issuer)
    monkeypatch.setenv("RAYTRAIN_OIDC_JWKS_URI", "https://sso.example.com/jwks")
    monkeypatch.delenv("RAYTRAIN_OIDC_AUDIENCE", raising=False)
    monkeypatch.setattr(auth, "_get_oidc_signing_key", lambda _token: other_public)

    token = jwt.encode(
        {"sub": "carol", "iss": issuer, "exp": _now() + dt.timedelta(hours=1)},
        signing_key,
        algorithm="RS256",
    )
    with pytest.raises(AuthError) as excinfo:
        verify_token(make_request_with_token(token))
    assert excinfo.value.code == "invalid_token"


# --------------------------------------------------------------------------- #
# require_identity dependency wrapper
# --------------------------------------------------------------------------- #
def test_require_identity_converts_to_http_exception(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setenv("RAYTRAIN_JWT_SECRET", "secret")
    with pytest.raises(HTTPException) as excinfo:
        auth.require_identity(make_request(None))
    assert excinfo.value.status_code == 401
    assert excinfo.value.detail["code"] == "missing_authorization"


def test_require_identity_passes_through_identity(monkeypatch):
    secret = "dep-secret"
    monkeypatch.setenv("RAYTRAIN_JWT_SECRET", secret)
    token = jwt.encode(
        {"sub": "erin", "exp": _now() + dt.timedelta(hours=1)},
        secret,
        algorithm="HS256",
    )
    identity = auth.require_identity(make_request_with_token(token))
    assert identity.user == "erin"
