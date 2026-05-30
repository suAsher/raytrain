"""
Parse a training repo's `.raytrain.yaml` manifest.

A manifest is a per-repo declarative description of:
  - which image to run in,
  - what command starts training (native_ddp / torchrun / accelerate / ray_train),
  - per-node resource requests,
  - which MinIO datasets to sync into the pods (old path: ``datasets``),
  - OR which Lance/Parquet data source to stream from (new path: ``data_source``),
  - which artifacts to upload on completion.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_API = {"raytrain/v1"}
SUPPORTED_LAUNCHERS = {"native_ddp", "torchrun", "accelerate", "custom", "ray_train"}
SUPPORTED_DATA_SOURCE_TYPES = {"lance", "parquet"}


@dataclass
class Launcher:
    type: str
    entrypoint: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Resources:
    gpus_per_node: int = 1
    cpus_per_node: int = 8
    memory_per_node: str = "64Gi"
    shm_size: str = "16Gi"
    object_store_memory: str = ""  # Ray Object Store size; empty = 30% of memory_per_node


@dataclass
class DatasetMount:
    name: str
    s3: str           # s3://bucket/prefix
    mount: str        # path relative to workdir where a symlink is placed
    read_only: bool = True


@dataclass
class DataSource:
    """New-style streaming data source backed by Ray Data + Lance/Parquet."""
    type: str              # "lance" | "parquet"
    uri: str               # s3://lance-datasets/nuscenes-v1/train.lance
    version: str = "latest"
    filter: str = ""       # optional filter expression
    columns: list[str] = field(default_factory=list)


@dataclass
class CodeSync:
    """
    Configure how the CLI bundles the working directory and ships it to
    Ray via ``runtime_env.working_dir``.

    Defaults to enabled. Set ``enabled: false`` in ``.raytrain.yaml`` to
    keep the legacy "code is baked into the image" behavior.
    """
    enabled: bool = True
    bucket: str = "raytrain-code"
    extra_excludes: list[str] = field(default_factory=list)
    max_size_mib: int = 200
    dedup: bool = False  # nice-to-have: HEAD _blobs/{sha}.zip before PUT


@dataclass
class Manifest:
    api_version: str
    image: str
    workdir: str
    launcher: Launcher
    resources: Resources
    datasets: list[DatasetMount] = field(default_factory=list)
    data_source: DataSource | None = None  # new streaming path
    artifacts: list[str] = field(default_factory=list)
    name: str | None = None           # default run/job name hint
    repo_name: str | None = None      # defaults to dir name
    cpu_workers: int = 0              # CPU-only workers for Ray Data transforms
    save_path: str | None = None      # default save_path template
    code_sync: CodeSync = field(default_factory=CodeSync)  # M0: workdir → MinIO → working_dir

    @staticmethod
    def load(path: str | os.PathLike) -> "Manifest":
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(
                f"manifest not found at {p}. Did you forget to create .raytrain.yaml?"
            )
        with p.open() as f:
            raw = yaml.safe_load(f) or {}

        api = raw.get("apiVersion")
        if api not in SUPPORTED_API:
            raise ValueError(
                f"unsupported apiVersion {api!r}; supported: {sorted(SUPPORTED_API)}"
            )

        launcher_raw = raw.get("launcher") or {}
        ltype = launcher_raw.get("type", "native_ddp")
        if ltype not in SUPPORTED_LAUNCHERS:
            raise ValueError(
                f"unsupported launcher.type {ltype!r}; supported: {sorted(SUPPORTED_LAUNCHERS)}"
            )
        launcher = Launcher(
            type=ltype,
            entrypoint=launcher_raw.get("entrypoint")
            or _required("launcher.entrypoint"),
            args=list(launcher_raw.get("args") or []),
            env=dict(launcher_raw.get("env") or {}),
        )

        res_raw = raw.get("resources") or {}
        resources = Resources(
            gpus_per_node=int(res_raw.get("gpus_per_node", 1)),
            cpus_per_node=int(res_raw.get("cpus_per_node", 8)),
            memory_per_node=str(res_raw.get("memory_per_node", "64Gi")),
            shm_size=str(res_raw.get("shm_size", "16Gi")),
            object_store_memory=str(res_raw.get("object_store_memory", "")),
        )

        datasets = [
            DatasetMount(
                name=d["name"],
                s3=d["s3"],
                mount=d["mount"],
                read_only=bool(d.get("read_only", True)),
            )
            for d in (raw.get("datasets") or [])
        ]

        # data_source (new streaming path) — alternative to datasets
        ds_raw = raw.get("data_source")
        data_source = None
        if ds_raw:
            if datasets:
                raise ValueError(
                    "datasets and data_source are mutually exclusive; "
                    "choose local dataset sync or Ray Data streaming."
                )
            ds_type = ds_raw.get("type", "lance")
            if ds_type not in SUPPORTED_DATA_SOURCE_TYPES:
                raise ValueError(
                    f"unsupported data_source.type {ds_type!r}; "
                    f"supported: {sorted(SUPPORTED_DATA_SOURCE_TYPES)}"
                )
            data_source = DataSource(
                type=ds_type,
                uri=ds_raw.get("uri") or _required("data_source.uri"),
                version=str(ds_raw.get("version", "latest")),
                filter=ds_raw.get("filter", ""),
                columns=list(ds_raw.get("columns") or []),
            )

        return Manifest(
            api_version=api,
            image=raw.get("image") or _required("image"),
            workdir=raw.get("workdir") or _required("workdir"),
            launcher=launcher,
            resources=resources,
            datasets=datasets,
            data_source=data_source,
            artifacts=list(raw.get("artifacts") or []),
            name=raw.get("name"),
            repo_name=raw.get("repo_name") or p.parent.name,
            cpu_workers=int(raw.get("cpu_workers", 0)),
            save_path=raw.get("save_path"),
            code_sync=_load_code_sync(raw.get("code_sync")),
        )


def _required(field_name: str) -> Any:
    raise ValueError(f"manifest missing required field: {field_name}")


def _load_code_sync(raw: Any) -> CodeSync:
    """
    Build a :class:`CodeSync` from the raw YAML block.

    Missing block / ``null`` -> defaults (``enabled=True``).
    Plain ``true`` / ``false`` -> shorthand for enabled / disabled.
    Mapping -> field-by-field overlay.
    """
    if raw is None:
        return CodeSync()
    if isinstance(raw, bool):
        return CodeSync(enabled=raw)
    if not isinstance(raw, dict):
        raise ValueError(
            "code_sync must be a mapping, a boolean, or omitted; "
            f"got {type(raw).__name__}"
        )
    return CodeSync(
        enabled=bool(raw.get("enabled", True)),
        bucket=str(raw.get("bucket", "raytrain-code")),
        extra_excludes=list(raw.get("extra_excludes") or []),
        max_size_mib=int(raw.get("max_size_mib", 200)),
        dedup=bool(raw.get("dedup", False)),
    )
