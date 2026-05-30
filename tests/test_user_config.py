"""
Unit tests for raytrain/user_config.py.

Run with:
    pytest tests/test_user_config.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

# Allow running directly: ROOT/raytrain importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from raytrain.user_config import UserConfig  # noqa: E402


# ---------------------------------------------------------------------------- #
# code_bucket field (spec task 1.5)
# ---------------------------------------------------------------------------- #


def test_code_bucket_default() -> None:
    """A freshly constructed UserConfig defaults code_bucket to raytrain-code."""
    cfg = UserConfig()
    assert cfg.code_bucket == "raytrain-code"


def test_code_bucket_round_trip(tmp_path: Path) -> None:
    """save then load preserves a customized code_bucket value."""
    cfg = UserConfig(user_name="zhangsan", code_bucket="custom-code-bucket")
    p = tmp_path / "config.yaml"
    cfg.save(p)

    loaded = UserConfig.load(p)
    assert loaded.code_bucket == "custom-code-bucket"
    # other fields survive the round trip too
    assert loaded.user_name == "zhangsan"


def test_code_bucket_persisted_to_yaml(tmp_path: Path) -> None:
    """The saved yaml actually contains the code_bucket key."""
    cfg = UserConfig(user_name="u", code_bucket="raytrain-code")
    p = tmp_path / "config.yaml"
    cfg.save(p)

    raw = yaml.safe_load(p.read_text())
    assert raw["code_bucket"] == "raytrain-code"


def test_load_old_yaml_missing_code_bucket_uses_default(tmp_path: Path) -> None:
    """An old config.yaml without code_bucket loads with the default value."""
    p = tmp_path / "config.yaml"
    # Simulate a pre-existing config written before code_bucket existed.
    p.write_text(
        yaml.safe_dump(
            {
                "user_name": "laoyonghu",
                "namespace": "ray-cluster-3",
                "exp_bucket": "u-{user}-exp",
            },
            sort_keys=False,
        )
    )

    loaded = UserConfig.load(p)
    assert loaded.code_bucket == "raytrain-code"
    assert loaded.user_name == "laoyonghu"


def test_load_missing_file_raises(tmp_path: Path) -> None:
    """Loading a non-existent config raises a helpful FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        UserConfig.load(tmp_path / "does-not-exist.yaml")


# ---------------------------------------------------------------------------- #
# shared_clusters field (spec task 8.1)
# ---------------------------------------------------------------------------- #


def test_shared_clusters_default() -> None:
    """A freshly constructed UserConfig defaults shared_clusters to {}."""
    cfg = UserConfig()
    assert cfg.shared_clusters == {}


def test_shared_clusters_round_trip(tmp_path: Path) -> None:
    """save then load preserves a non-empty shared_clusters mapping."""
    clusters = {
        "h20": "http://ray-shared-h20-head.ray-shared.svc:8265",
        "a100": "http://ray-shared-a100-head.ray-shared.svc:8265",
    }
    cfg = UserConfig(user_name="zhangsan", shared_clusters=clusters)
    p = tmp_path / "config.yaml"
    cfg.save(p)

    loaded = UserConfig.load(p)
    assert loaded.shared_clusters == clusters
    assert loaded.user_name == "zhangsan"


def test_load_old_yaml_missing_shared_clusters_uses_empty_dict(tmp_path: Path) -> None:
    """An old config.yaml without shared_clusters loads with an empty dict."""
    p = tmp_path / "config.yaml"
    # Simulate a pre-existing config written before shared_clusters existed.
    p.write_text(
        yaml.safe_dump(
            {
                "user_name": "laoyonghu",
                "namespace": "ray-cluster-3",
                "exp_bucket": "u-{user}-exp",
            },
            sort_keys=False,
        )
    )

    loaded = UserConfig.load(p)
    assert loaded.shared_clusters == {}
    assert loaded.user_name == "laoyonghu"


def test_load_null_shared_clusters_uses_empty_dict(tmp_path: Path) -> None:
    """A yaml with shared_clusters explicitly null loads as an empty dict."""
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {"user_name": "u", "shared_clusters": None},
            sort_keys=False,
        )
    )

    loaded = UserConfig.load(p)
    assert loaded.shared_clusters == {}
