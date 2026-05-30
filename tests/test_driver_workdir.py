"""
Unit tests for driver._resolve_workdir.

Run with:
    pytest tests/test_driver_workdir.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from raytrain.entrypoint.driver import _resolve_workdir  # noqa: E402


def test_prefers_ray_runtime_env_working_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """When Ray injected working_dir env var, use it."""
    monkeypatch.setenv("RAY_RUNTIME_ENV_WORKING_DIR", "/tmp/ray/session_x/runtime/wd")
    plan = {"workdir": "/legacy/path"}
    manifest = {"workdir": "/manifest/path"}
    assert _resolve_workdir(plan, manifest) == "/tmp/ray/session_x/runtime/wd"


def test_fallback_to_plan_workdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAY_RUNTIME_ENV_WORKING_DIR", raising=False)
    plan = {"workdir": "/from/plan"}
    manifest = {"workdir": "/from/manifest"}
    assert _resolve_workdir(plan, manifest) == "/from/plan"


def test_fallback_to_manifest_workdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAY_RUNTIME_ENV_WORKING_DIR", raising=False)
    plan = {}
    manifest = {"workdir": "/from/manifest"}
    assert _resolve_workdir(plan, manifest) == "/from/manifest"


def test_empty_string_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAY_RUNTIME_ENV_WORKING_DIR", "")
    plan = {"workdir": "/legacy"}
    manifest = {"workdir": ""}
    assert _resolve_workdir(plan, manifest) == "/legacy"


def test_whitespace_only_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAY_RUNTIME_ENV_WORKING_DIR", "   ")
    plan = {"workdir": "/legacy"}
    manifest = {"workdir": ""}
    assert _resolve_workdir(plan, manifest) == "/legacy"


def test_raises_when_nothing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAY_RUNTIME_ENV_WORKING_DIR", raising=False)
    with pytest.raises(RuntimeError, match="no workdir resolved"):
        _resolve_workdir({}, {})


def test_raises_when_all_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAY_RUNTIME_ENV_WORKING_DIR", "")
    with pytest.raises(RuntimeError):
        _resolve_workdir({"workdir": ""}, {"workdir": ""})


def test_plan_overrides_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both fallbacks set, plan wins."""
    monkeypatch.delenv("RAY_RUNTIME_ENV_WORKING_DIR", raising=False)
    plan = {"workdir": "/from/plan"}
    manifest = {"workdir": "/from/manifest"}
    assert _resolve_workdir(plan, manifest) == "/from/plan"
