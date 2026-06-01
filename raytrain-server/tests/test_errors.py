"""
FriendlyError contract: raising it anywhere yields a structured, localizable
JSON body { "error": { code, message, hint? } } with the right status.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from raytrain_server.core.errors import Codes, FriendlyError, install_error_handlers


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom")
    def boom():
        raise FriendlyError(409, Codes.WORKSPACE_TERMINATING,
                            "上一个实例仍在终止", hint="请稍后重试")

    @app.get("/plain")
    def plain():
        raise FriendlyError(400, Codes.BAD_REQUEST, "参数错误")

    return TestClient(app, raise_server_exceptions=False)


def test_friendly_error_payload(client):
    r = client.get("/boom")
    assert r.status_code == 409
    body = r.json()
    assert body == {
        "error": {
            "code": "WORKSPACE_TERMINATING",
            "message": "上一个实例仍在终止",
            "hint": "请稍后重试",
        }
    }


def test_friendly_error_without_hint(client):
    r = client.get("/plain")
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "BAD_REQUEST"
    assert body["error"]["message"] == "参数错误"
    assert "hint" not in body["error"]


def test_to_payload_unit():
    e = FriendlyError(403, Codes.FORBIDDEN, "no")
    assert e.to_payload() == {"error": {"code": "FORBIDDEN", "message": "no"}}
    assert e.status_code == 403
