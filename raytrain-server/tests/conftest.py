"""Shared fixtures."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterator

import pytest

# Make raytrain_server importable when running ``pytest`` from repo root or
# raytrain-server/.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from raytrain_server.core.settings import Settings, get_settings  # noqa: E402


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Fresh Settings with deterministic dev values + clean cache."""
    monkeypatch.setenv("RAYTRAIN_JWT_SECRET", "test-secret-please-rotate-this-now-32b!")
    monkeypatch.setenv("RAYTRAIN_JWT_ISSUER", "raytrain-test")
    monkeypatch.setenv("RAYTRAIN_JWT_DEFAULT_TTL_DAYS", "1")
    monkeypatch.setenv(
        "RAYTRAIN_SHARED_CLUSTERS",
        '{"h20": "http://ray-shared-h20:8265"}',
    )
    monkeypatch.setenv("RAYTRAIN_MINIO_ENDPOINT", "http://minio:9000")
    monkeypatch.setenv("RAYTRAIN_MINIO_ACCESS_KEY", "test-ak")
    monkeypatch.setenv("RAYTRAIN_MINIO_SECRET_KEY", "test-sk")
    monkeypatch.setenv("RAYTRAIN_CODE_BUCKET", "raytrain-code")
    monkeypatch.setenv("RAYTRAIN_WORKSPACE_BASE_DOMAIN", "raytrain.example.com")
    monkeypatch.setenv("RAYTRAIN_IN_CLUSTER", "false")
    monkeypatch.setenv("RAYTRAIN_SEED_DEMO", "false")
    get_settings.cache_clear()
    s = get_settings()
    yield s
    get_settings.cache_clear()
