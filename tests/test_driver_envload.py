"""
Unit tests for env-var-based manifest/plan loading in the driver (task 8.5).

Covers:
  1. DriverConfig.from_env() base64-decodes + YAML-parses the
     RAYTRAIN_MANIFEST_B64 / RAYTRAIN_PLAN_B64 env vars (shared/Platform path).
  2. Clear errors when an env var is missing/empty, bad base64, or bad YAML.
  3. DriverConfig.load() from files still works (legacy per-job path intact).
  4. The small pure chooser _choose_driver_config(args, environ) picks env vs
     files correctly WITHOUT touching Ray.

Run with:
    PYTHONPATH=. python3 -m pytest tests/test_driver_envload.py -v
"""
from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from raytrain.entrypoint.driver import (  # noqa: E402
    DriverConfig,
    _choose_driver_config,
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

_MANIFEST = {"workdir": "/w", "launcher": {"type": "native_ddp", "cmd": ["x"]}}
_PLAN = {"run_id": "r", "num_nodes": 1, "gpus_per_node": 1, "cpus_per_node": 4}


def _b64_yaml(doc: dict) -> str:
    return base64.b64encode(yaml.safe_dump(doc).encode("utf-8")).decode("ascii")


def _args(manifest=None, plan=None, from_env=False) -> argparse.Namespace:
    return argparse.Namespace(manifest=manifest, plan=plan, from_env=from_env)


# --------------------------------------------------------------------------- #
# 1. from_env decodes base64 YAML
# --------------------------------------------------------------------------- #


def test_from_env_decodes_b64() -> None:
    env = {
        "RAYTRAIN_MANIFEST_B64": _b64_yaml(_MANIFEST),
        "RAYTRAIN_PLAN_B64": _b64_yaml(_PLAN),
    }
    dc = DriverConfig.from_env(env)
    assert dc.manifest == _MANIFEST
    assert dc.plan == _PLAN


def test_from_env_reads_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env() with no arg falls back to os.environ."""
    monkeypatch.setenv("RAYTRAIN_MANIFEST_B64", _b64_yaml(_MANIFEST))
    monkeypatch.setenv("RAYTRAIN_PLAN_B64", _b64_yaml(_PLAN))
    dc = DriverConfig.from_env()
    assert dc.manifest == _MANIFEST
    assert dc.plan == _PLAN


def test_from_env_tolerates_whitespace_padding() -> None:
    """Leading/trailing whitespace around the b64 blob is stripped."""
    env = {
        "RAYTRAIN_MANIFEST_B64": "  " + _b64_yaml(_MANIFEST) + "\n",
        "RAYTRAIN_PLAN_B64": _b64_yaml(_PLAN),
    }
    dc = DriverConfig.from_env(env)
    assert dc.manifest == _MANIFEST


# --------------------------------------------------------------------------- #
# 2. error cases
# --------------------------------------------------------------------------- #


def test_from_env_missing_raises() -> None:
    env = {"RAYTRAIN_MANIFEST_B64": _b64_yaml(_MANIFEST)}  # plan missing
    with pytest.raises(ValueError, match="RAYTRAIN_PLAN_B64"):
        DriverConfig.from_env(env)


def test_from_env_empty_raises() -> None:
    env = {
        "RAYTRAIN_MANIFEST_B64": "",
        "RAYTRAIN_PLAN_B64": _b64_yaml(_PLAN),
    }
    with pytest.raises(ValueError, match="missing or empty"):
        DriverConfig.from_env(env)


def test_from_env_bad_base64_raises() -> None:
    env = {
        "RAYTRAIN_MANIFEST_B64": "!!!not-base64!!!",
        "RAYTRAIN_PLAN_B64": _b64_yaml(_PLAN),
    }
    with pytest.raises(ValueError, match="not valid base64"):
        DriverConfig.from_env(env)


def test_from_env_bad_yaml_raises() -> None:
    # Valid base64, but the decoded bytes are not valid YAML.
    bad = base64.b64encode(b"key: : : [unbalanced").decode("ascii")
    env = {
        "RAYTRAIN_MANIFEST_B64": bad,
        "RAYTRAIN_PLAN_B64": _b64_yaml(_PLAN),
    }
    with pytest.raises(ValueError, match="valid YAML"):
        DriverConfig.from_env(env)


def test_from_env_non_mapping_raises() -> None:
    # Valid base64 + valid YAML, but a scalar/list, not a mapping.
    scalar = base64.b64encode(b"just-a-string").decode("ascii")
    env = {
        "RAYTRAIN_MANIFEST_B64": scalar,
        "RAYTRAIN_PLAN_B64": _b64_yaml(_PLAN),
    }
    with pytest.raises(ValueError, match="YAML mapping"):
        DriverConfig.from_env(env)


# --------------------------------------------------------------------------- #
# 3. legacy file path still works
# --------------------------------------------------------------------------- #


def test_load_from_files_still_works(tmp_path: Path) -> None:
    mpath = tmp_path / "manifest.yaml"
    ppath = tmp_path / "plan.yaml"
    mpath.write_text(yaml.safe_dump(_MANIFEST))
    ppath.write_text(yaml.safe_dump(_PLAN))

    dc = DriverConfig.load(str(mpath), str(ppath))
    assert dc.manifest == _MANIFEST
    assert dc.plan == _PLAN


# --------------------------------------------------------------------------- #
# 4. chooser logic (pure, no Ray)
# --------------------------------------------------------------------------- #


def test_resolver_prefers_env_when_flag() -> None:
    env = {
        "RAYTRAIN_MANIFEST_B64": _b64_yaml(_MANIFEST),
        "RAYTRAIN_PLAN_B64": _b64_yaml(_PLAN),
    }
    dc = _choose_driver_config(_args(from_env=True), env)
    assert dc.manifest == _MANIFEST
    assert dc.plan == _PLAN


def test_resolver_uses_env_when_present_and_no_paths() -> None:
    """No --from-env flag, but both env vars present and no file paths."""
    env = {
        "RAYTRAIN_MANIFEST_B64": _b64_yaml(_MANIFEST),
        "RAYTRAIN_PLAN_B64": _b64_yaml(_PLAN),
    }
    dc = _choose_driver_config(_args(), env)
    assert dc.manifest == _MANIFEST


def test_resolver_uses_files_otherwise(tmp_path: Path) -> None:
    """Legacy invocation: file paths given, no env vars -> read files."""
    mpath = tmp_path / "manifest.yaml"
    ppath = tmp_path / "plan.yaml"
    mpath.write_text(yaml.safe_dump(_MANIFEST))
    ppath.write_text(yaml.safe_dump(_PLAN))

    dc = _choose_driver_config(_args(manifest=str(mpath), plan=str(ppath)), {})
    assert dc.manifest == _MANIFEST
    assert dc.plan == _PLAN


def test_resolver_files_win_when_paths_given_even_if_env_present(
    tmp_path: Path,
) -> None:
    """
    If file paths are explicitly given AND env vars are present (no --from-env),
    the explicit paths win — they were passed on purpose.
    """
    other = {"workdir": "/from-file", "launcher": {"type": "native_ddp"}}
    mpath = tmp_path / "manifest.yaml"
    ppath = tmp_path / "plan.yaml"
    mpath.write_text(yaml.safe_dump(other))
    ppath.write_text(yaml.safe_dump(_PLAN))

    env = {
        "RAYTRAIN_MANIFEST_B64": _b64_yaml(_MANIFEST),
        "RAYTRAIN_PLAN_B64": _b64_yaml(_PLAN),
    }
    dc = _choose_driver_config(
        _args(manifest=str(mpath), plan=str(ppath)), env
    )
    assert dc.manifest == other  # files won


def test_resolver_from_env_flag_wins_over_paths(tmp_path: Path) -> None:
    """Explicit --from-env beats file paths."""
    mpath = tmp_path / "manifest.yaml"
    ppath = tmp_path / "plan.yaml"
    mpath.write_text(yaml.safe_dump({"workdir": "/file-only"}))
    ppath.write_text(yaml.safe_dump(_PLAN))

    env = {
        "RAYTRAIN_MANIFEST_B64": _b64_yaml(_MANIFEST),
        "RAYTRAIN_PLAN_B64": _b64_yaml(_PLAN),
    }
    dc = _choose_driver_config(
        _args(manifest=str(mpath), plan=str(ppath), from_env=True), env
    )
    assert dc.manifest == _MANIFEST  # env won because of explicit flag


def test_resolver_no_source_raises() -> None:
    with pytest.raises(ValueError, match="no manifest/plan source"):
        _choose_driver_config(_args(), {})


def test_resolver_partial_env_no_paths_raises() -> None:
    """Only one env var present and no paths -> not enough to load."""
    env = {"RAYTRAIN_MANIFEST_B64": _b64_yaml(_MANIFEST)}
    with pytest.raises(ValueError, match="no manifest/plan source"):
        _choose_driver_config(_args(), env)
