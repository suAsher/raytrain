"""
RayLanceDataset: a PyTorch IterableDataset powered by Ray Data + Lance.

The training subprocess connects to the existing Ray cluster (started by
KubeRay/RayJob) via ``ray.init(address="auto")``, then uses
``ray.data.read_lance()`` to stream data from MinIO.

Capabilities (mapping to RAYDATA_PROPOSAL sections):
  - Streaming read from MinIO Lance       (§5.1)
  - Plasma caching via materialize()      (§5.2)
  - Locality-aware scheduling             (§5.3)
  - ActorPool for CPU transforms          (§5.4)
  - Heterogeneous resources (CPU/GPU)     (§5.5)
  - Zero-copy Arrow → Tensor              (§5.7)
  - Pipelined prefetch                    (§5.8)
  - Block-level + window shuffle          (§5.9)
  - DDP sharding via streaming split      (§6.11)

Usage::

    from raytrain.data import RayLanceDataset
    from torch.utils.data import DataLoader

    ds = RayLanceDataset(
        uri="s3://lance-datasets/nuscenes-v1/train.lance",
        transform_fn=MyAugmentActor,   # class with __init__ + __call__
        transform_concurrency=(4, 16), # ActorPool min/max
        batch_size=6,
        prefetch_batches=4,
        local_shuffle_buffer_size=512,
        do_materialize=True,           # pin to Plasma for multi-epoch
    )
    # num_workers=0 because Ray Data handles its own parallelism
    loader = DataLoader(ds, batch_size=None, num_workers=0)

    for epoch in range(50):
        for batch in loader:
            loss = model(batch["coord"], batch["segment"])
            loss.backward()
"""
from __future__ import annotations

import os
import math
from typing import Any, Callable, Iterator

import torch
from torch.utils.data import IterableDataset


class RayLanceDataset(IterableDataset):
    """
    PyTorch IterableDataset backed by Ray Data + Lance on S3/MinIO.

    The dataset connects to the existing Ray cluster and uses Ray Data's
    full feature set: streaming, Plasma caching, ActorPool transforms,
    zero-copy Arrow-to-Tensor, pipelined prefetch, and block shuffle.

    Parameters
    ----------
    uri : str
        Lance dataset URI on MinIO, e.g. ``s3://lance-datasets/nuscenes.lance``.
        Falls back to ``RAYTRAIN_DATA_SOURCE_URI`` env var if not provided.
    columns : list[str], optional
        Column pruning — only read these columns from Lance.
    filter_expr : str, optional
        Lance filter expression, e.g. ``"split == 'train'"``.
    version : str, optional
        Lance version to checkout (integer as string, or "latest").
    transform_fn : callable or class, optional
        A callable or class for ``map_batches()``. If a class, it is used
        as an ActorPool actor (§5.4): ``__init__`` runs once, ``__call__``
        is invoked per batch. This runs on CPU workers, not GPU workers.
    transform_concurrency : int or tuple[int, int]
        ActorPool size. A single int means fixed size; a tuple means
        ``(min_size, max_size)`` for autoscaling.
    transform_num_cpus : int
        CPU cores per transform actor.
    batch_size : int
        Mini-batch size for ``iter_torch_batches()``.
    prefetch_batches : int
        Pipeline depth — how many batches to prefetch (§5.8).
    local_shuffle_buffer_size : int
        Window size for local shuffle within each shard (§5.9).
    do_materialize : bool
        If True, call ``ds.materialize()`` to pin data in Plasma for
        multi-epoch reuse (§5.2). Only effective when data fits in
        Plasma + spill capacity.
    override_num_blocks : int, optional
        Explicit block count for Ray Data. Rule of thumb: GPU_count × 4.
    storage_options : dict, optional
        S3/MinIO connection options. Auto-configured from env vars if
        not provided.
    """

    def __init__(
        self,
        uri: str | None = None,
        columns: list[str] | None = None,
        filter_expr: str | None = None,
        version: str | None = None,
        transform_fn: Callable | type | None = None,
        transform_concurrency: int | tuple[int, int] = (2, 8),
        transform_num_cpus: int = 2,
        batch_size: int = 1,
        prefetch_batches: int = 4,
        local_shuffle_buffer_size: int = 512,
        do_materialize: bool = False,
        override_num_blocks: int | None = None,
        storage_options: dict[str, str] | None = None,
    ):
        # Auto-config from env vars set by raytrain driver
        self.uri = uri or os.environ.get("RAYTRAIN_DATA_SOURCE_URI", "")
        self.filter_expr = (filter_expr
                            or os.environ.get("RAYTRAIN_DATA_SOURCE_FILTER")
                            or None)
        self.version = (version
                        or os.environ.get("RAYTRAIN_DATA_SOURCE_VERSION")
                        or None)
        cols_env = os.environ.get("RAYTRAIN_DATA_SOURCE_COLUMNS", "")
        self.columns = columns or (cols_env.split(",") if cols_env else None)

        self.transform_fn = transform_fn
        self.transform_concurrency = transform_concurrency
        self.transform_num_cpus = transform_num_cpus
        self.batch_size = batch_size
        self.prefetch_batches = prefetch_batches
        self.local_shuffle_buffer_size = local_shuffle_buffer_size
        self.do_materialize = do_materialize
        self.override_num_blocks = override_num_blocks
        self.storage_options = storage_options or self._default_storage_options()

        if not self.uri:
            raise ValueError(
                "Lance URI not set. Pass uri= or set RAYTRAIN_DATA_SOURCE_URI."
            )

        # Connect to existing Ray cluster (started by KubeRay RayJob).
        # ignore_reinit_error=True allows calling from multiple workers.
        import ray
        ray.init(address="auto", ignore_reinit_error=True)

        # Build the Ray Data pipeline (lazy — no IO happens here)
        self._ds = self._build_pipeline()
        self._count: int | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _default_storage_options() -> dict[str, str]:
        """Build S3/MinIO storage options from env vars injected by driver."""
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        endpoint = (
            os.environ.get("S3_ENDPOINT_URL")
            or os.environ.get("AWS_ENDPOINT")
            or os.environ.get("AWS_ENDPOINT_URL")
            or ""
        )
        access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        options = {
            "aws_endpoint": endpoint,
            "endpoint": endpoint,
            "aws_access_key_id": access_key,
            "access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "secret_access_key": secret_key,
            "region": region,
            "aws_region": region,
            "allow_http": "true",
            "aws_virtual_hosted_style_request": "false",
            "virtual_hosted_style_request": "false",
        }
        return {key: value for key, value in options.items() if value}

    @staticmethod
    def _render_version(version: str | int | None) -> int | str | None:
        if version in (None, "", "latest"):
            return None
        if isinstance(version, int):
            return version
        version_str = str(version)
        return int(version_str) if version_str.isdigit() else version_str

    def _build_pipeline(self):
        """Construct the Ray Data pipeline with full feature set."""
        import ray.data

        # §5.1 — Streaming read from Lance on MinIO
        read_kwargs: dict[str, Any] = {}
        rendered_version = self._render_version(self.version)
        if rendered_version is not None:
            read_kwargs["version"] = rendered_version
        if self.columns:
            read_kwargs["columns"] = self.columns
        if self.filter_expr:
            read_kwargs["filter"] = self.filter_expr
        if self.override_num_blocks:
            read_kwargs["override_num_blocks"] = self.override_num_blocks

        ds = ray.data.read_lance(
            self.uri,
            storage_options=self.storage_options,
            **read_kwargs,
        )

        # §5.9 — Block-level shuffle (near-zero cost)
        ds = ds.randomize_block_order()

        # §5.4 + §5.5 — CPU-offloaded transforms via ActorPool
        if self.transform_fn:
            if isinstance(self.transform_concurrency, tuple):
                min_size, max_size = self.transform_concurrency
            else:
                min_size = max_size = self.transform_concurrency

            ds = ds.map_batches(
                self.transform_fn,
                batch_size=self.batch_size,
                compute=ray.data.ActorPoolStrategy(
                    min_size=min_size,
                    max_size=max_size,
                ),
                num_cpus=self.transform_num_cpus,
                num_gpus=0,  # transforms run on CPU workers only
            )

        # §5.2 — Plasma caching for multi-epoch reuse
        if self.do_materialize:
            ds = ds.materialize()

        return ds

    def _get_shard(self):
        """
        §6.11 — DDP sharding: split dataset by world_size, take this
        rank's shard. Each GPU worker gets a unique slice of the data.
        """
        world_size, global_rank = self._rank_info()

        if world_size > 1:
            shards = self._ds.split(world_size)
            return shards[min(global_rank, len(shards) - 1)]
        return self._ds

    @staticmethod
    def _rank_info() -> tuple[int, int]:
        try:
            import torch.distributed as dist

            if dist.is_available() and dist.is_initialized():
                return dist.get_world_size(), dist.get_rank()
        except Exception:
            pass

        world_size = int(os.environ.get("WORLD_SIZE", "1"))
        rank_env = os.environ.get("RANK")
        if rank_env is not None:
            return world_size, int(rank_env)

        local_rank = int(os.environ.get("LOCAL_RANK", "0"))
        gpus_per_node = int(os.environ.get("RAYTRAIN_GPUS_PER_NODE", "1"))
        node_rank = int(
            os.environ.get("RAYTRAIN_NODE_RANK")
            or os.environ.get("NODE_RANK")
            or "0"
        )
        return world_size, node_rank * gpus_per_node + local_rank

    # ------------------------------------------------------------------
    # PyTorch IterableDataset interface
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[dict[str, Any]]:
        shard = self._get_shard()

        # §5.7 + §5.8 — Zero-copy Arrow→Tensor + pipelined prefetch
        for batch in shard.iter_torch_batches(
            batch_size=self.batch_size,
            prefetch_batches=self.prefetch_batches,
            local_shuffle_buffer_size=self.local_shuffle_buffer_size,
        ):
            yield batch

    def __len__(self) -> int:
        if self._count is None:
            world_size, _ = self._rank_info()
            shard_rows = math.ceil(self._ds.count() / max(world_size, 1))
            self._count = math.ceil(shard_rows / max(self.batch_size, 1))
        return self._count

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def schema(self):
        """Return the Arrow schema of the underlying Lance dataset."""
        return self._ds.schema()
