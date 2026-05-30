"""Tests for raytrain_server.core.jwt_auth."""
from __future__ import annotations

import time

import jwt
import pytest
from fastapi import HTTPException

from raytrain_server.core.jwt_auth import (
    Identity,
    _extract_bearer,
    issue_token,
    require_admin,
    require_user,
    verify_token,
)
from raytrain_server.core.settings import Settings


# ---------------------------------------------------------------------------- #
# issue_token
# ---------------------------------------------------------------------------- #


class TestIssueToken:
    def test_happy_path_round_trip(self, settings: Settings) -> None:
        token, exp = issue_token(
            "zhangsan", tenant="occ", role="user", settings=settings
        )
        assert token.count(".") == 2  # JWT has 3 segments
        ident = verify_token(token, settings=settings)
        assert ident.user == "zhangsan"
        assert ident.tenant == "occ"
        assert ident.role == "user"
        assert ident.expires_at == exp
        assert ident.is_admin is False

    def test_admin_role_round_trip(self, settings: Settings) -> None:
        token, _ = issue_token("root", role="admin", settings=settings)
        ident = verify_token(token, settings=settings)
        assert ident.role == "admin"
        assert ident.is_admin is True

    def test_default_tenant(self, settings: Settings) -> None:
        token, _ = issue_token("u1", settings=settings)
        ident = verify_token(token, settings=settings)
        assert ident.tenant == "default"

    def test_rejects_blank_user(self, settings: Settings) -> None:
        with pytest.raises(ValueError):
            issue_token("", settings=settings)

    def test_rejects_special_chars_in_user(self, settings: Settings) -> None:
        with pytest.raises(ValueError):
            issue_token("foo@bar", settings=settings)

    def test_rejects_zero_or_negative_ttl(self, settings: Settings) -> None:
        with pytest.raises(ValueError):
            issue_token("u", ttl_days=0, settings=settings)
        with pytest.raises(ValueError):
            issue_token("u", ttl_days=-3, settings=settings)


# ---------------------------------------------------------------------------- #
# verify_token
# ---------------------------------------------------------------------------- #


class TestVerifyToken:
    def test_missing_token_raises_401(self, settings: Settings) -> None:
        with pytest.raises(HTTPException) as exc:
            verify_token("", settings=settings)
        assert exc.value.status_code == 401

    def test_garbage_token_raises_401(self, settings: Settings) -> None:
        with pytest.raises(HTTPException) as exc:
            verify_token("not.a.jwt", settings=settings)
        assert exc.value.status_code == 401

    def test_expired_token_raises_401(self, settings: Settings) -> None:
        # Encode a token that's already expired.
        now = int(time.time())
        payload = {
            "sub": "u1",
            "tenant": "default",
            "role": "user",
            "iat": now - 1000,
            "exp": now - 1,
            "iss": settings.jwt_issuer,
        }
        token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
        with pytest.raises(HTTPException) as exc:
            verify_token(token, settings=settings)
        assert exc.value.status_code == 401

    def test_wrong_secret_raises_401(self, settings: Settings) -> None:
        # Sign with a different secret → invalid signature
        now = int(time.time())
        payload = {
            "sub": "u1",
            "tenant": "default",
            "role": "user",
            "iat": now,
            "exp": now + 60,
            "iss": settings.jwt_issuer,
        }
        token = jwt.encode(payload, "different-secret", algorithm="HS256")
        with pytest.raises(HTTPException) as exc:
            verify_token(token, settings=settings)
        assert exc.value.status_code == 401

    def test_wrong_issuer_raises_401(self, settings: Settings) -> None:
        now = int(time.time())
        payload = {
            "sub": "u1",
            "tenant": "default",
            "role": "user",
            "iat": now,
            "exp": now + 60,
            "iss": "some-other-issuer",
        }
        token = jwt.encode(
            payload, settings.jwt_secret, algorithm=settings.jwt_algorithm
        )
        with pytest.raises(HTTPException) as exc:
            verify_token(token, settings=settings)
        assert exc.value.status_code == 401

    def test_unknown_role_downgraded_to_user(self, settings: Settings) -> None:
        now = int(time.time())
        payload = {
            "sub": "u1",
            "tenant": "default",
            "role": "superuser",  # not in our allowed set
            "iat": now,
            "exp": now + 60,
            "iss": settings.jwt_issuer,
        }
        token = jwt.encode(
            payload, settings.jwt_secret, algorithm=settings.jwt_algorithm
        )
        ident = verify_token(token, settings=settings)
        assert ident.role == "user"


# ---------------------------------------------------------------------------- #
# Bearer header parsing
# ---------------------------------------------------------------------------- #


class TestExtractBearer:
    def test_happy_path(self) -> None:
        assert _extract_bearer("Bearer abc.def.ghi") == "abc.def.ghi"

    def test_case_insensitive_scheme(self) -> None:
        assert _extract_bearer("bearer abc.def.ghi") == "abc.def.ghi"

    def test_missing_header_raises_401(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _extract_bearer(None)
        assert exc.value.status_code == 401

    def test_wrong_scheme_raises_401(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _extract_bearer("Basic foo")
        assert exc.value.status_code == 401

    def test_no_token_after_scheme_raises_401(self) -> None:
        with pytest.raises(HTTPException) as exc:
            _extract_bearer("Bearer")
        assert exc.value.status_code == 401


# ---------------------------------------------------------------------------- #
# require_user / require_admin (FastAPI deps)
# ---------------------------------------------------------------------------- #


class TestRequireUser:
    def test_returns_identity(self, settings: Settings) -> None:
        token, _ = issue_token("u", settings=settings)
        ident = require_user(authorization=f"Bearer {token}")
        assert isinstance(ident, Identity)
        assert ident.user == "u"

    def test_missing_authz_raises(self, settings: Settings) -> None:
        with pytest.raises(HTTPException) as exc:
            require_user(authorization=None)
        assert exc.value.status_code == 401


class TestRequireAdmin:
    def test_admin_passes(self, settings: Settings) -> None:
        admin = Identity(
            user="root",
            tenant="default",
            role="admin",
            issued_at=0,
            expires_at=2**31,
        )
        # Calling the dependency directly, bypass FastAPI plumbing.
        assert require_admin(identity=admin) is admin

    def test_user_blocked(self, settings: Settings) -> None:
        normal = Identity(
            user="u",
            tenant="default",
            role="user",
            issued_at=0,
            expires_at=2**31,
        )
        with pytest.raises(HTTPException) as exc:
            require_admin(identity=normal)
        assert exc.value.status_code == 403
