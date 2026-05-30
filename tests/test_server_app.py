"""
Unit tests for raytrain/server/app.py.

Run with:
    PYTHONPATH=. python3 -m pytest tests/test_server_app.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running directly: ROOT/raytrain importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from raytrain.server.app import app  # noqa: E402

client = TestClient(app)


def test_healthz_returns_ok():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readyz_returns_ready():
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
