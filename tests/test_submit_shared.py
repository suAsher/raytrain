"""Tests for the shared-cluster submission helpers in raytrain.cli.submit.

These exercise the pure helpers (_substitute_args, _build_entrypoint) without
needing a running Platform server.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from raytrain.cli.submit import _build_entrypoint, _substitute_args  # noqa: E402
from raytrain.manifest import Launcher, Manifest, Resources  # noqa: E402


def _manifest(launcher: Launcher) -> Manifest:
    return Manifest(
        api_version="raytrain/v1",
        image="img:latest",
        workdir="/workspace/proj",
        launcher=launcher,
        resources=Resources(),
        repo_name="proj",
    )


class TestSubstituteArgs:
    def test_substitutes_known(self) -> None:
        out = _substitute_args(
            ["--config={config}", "--nodes={num_nodes}"],
            {"config": "c.py", "num_nodes": "2"},
        )
        assert out == ["--config=c.py", "--nodes=2"]

    def test_leaves_unknown_intact(self) -> None:
        out = _substitute_args(
            ["--addr={master_addr}"],
            {"config": "c.py"},
        )
        assert out == ["--addr={master_addr}"]

    def test_handles_no_placeholders(self) -> None:
        out = _substitute_args(["--flag", "value"], {"config": "x"})
        assert out == ["--flag", "value"]


class TestBuildEntrypoint:
    def test_native_ddp_python_entry(self) -> None:
        m = _manifest(
            Launcher(
                type="native_ddp",
                entrypoint="tools/train.py",
                args=["--config-file={config}", "--num-gpus={num_gpus_per_node}"],
            )
        )
        ep = _build_entrypoint(
            m, config_path="configs/x.py", save_path="/save",
            num_nodes=1, gpus_per_node=8, config_override=[],
        )
        assert ep == "python tools/train.py --config-file=configs/x.py --num-gpus=8"

    def test_ray_train_entry(self) -> None:
        m = _manifest(
            Launcher(
                type="ray_train",
                entrypoint="tools/train_ray.py",
                args=["--config", "{config}", "--num-workers", "{world_size}"],
            )
        )
        ep = _build_entrypoint(
            m, config_path="configs/x.py", save_path="/save",
            num_nodes=2, gpus_per_node=8, config_override=[],
        )
        assert "python tools/train_ray.py" in ep
        assert "--config configs/x.py" in ep
        assert "--num-workers 16" in ep

    def test_config_override_appended(self) -> None:
        m = _manifest(
            Launcher(type="native_ddp", entrypoint="t.py", args=[])
        )
        ep = _build_entrypoint(
            m, config_path="c.py", save_path="/s",
            num_nodes=1, gpus_per_node=1, config_override=["foo=bar", "baz=1"],
        )
        assert ep.endswith("foo=bar baz=1")

    def test_multi_word_entrypoint_kept(self) -> None:
        m = _manifest(
            Launcher(type="custom", entrypoint="accelerate launch", args=["x.py"])
        )
        ep = _build_entrypoint(
            m, config_path="c.py", save_path="/s",
            num_nodes=1, gpus_per_node=1, config_override=[],
        )
        assert ep.startswith("accelerate launch x.py")
