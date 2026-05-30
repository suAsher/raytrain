from __future__ import annotations

import importlib
import sys
import types


class _FakeIterableDataset:
    pass


def _install_fake_torch(
    *,
    dist_available: bool = False,
    dist_initialized: bool = False,
    world_size: int = 1,
    rank: int = 0,
) -> None:
    torch_mod = types.ModuleType("torch")
    utils_mod = types.ModuleType("torch.utils")
    data_mod = types.ModuleType("torch.utils.data")
    dist_mod = types.ModuleType("torch.distributed")

    data_mod.IterableDataset = _FakeIterableDataset
    utils_mod.data = data_mod
    torch_mod.utils = utils_mod

    dist_mod.is_available = lambda: dist_available
    dist_mod.is_initialized = lambda: dist_initialized
    dist_mod.get_world_size = lambda: world_size
    dist_mod.get_rank = lambda: rank
    torch_mod.distributed = dist_mod

    sys.modules["torch"] = torch_mod
    sys.modules["torch.utils"] = utils_mod
    sys.modules["torch.utils.data"] = data_mod
    sys.modules["torch.distributed"] = dist_mod


def _load_dataset_class():
    sys.modules.pop("raytrain.data.ray_lance_dataset", None)
    mod = importlib.import_module("raytrain.data.ray_lance_dataset")
    return mod.RayLanceDataset


def test_read_lance_receives_version_filter_columns_and_storage_options(monkeypatch):
    _install_fake_torch()
    RayLanceDataset = _load_dataset_class()

    calls = {}

    class FakeDataset:
        def randomize_block_order(self):
            calls["randomized"] = True
            return self

        def materialize(self):
            calls["materialized"] = True
            return self

    ray_mod = types.ModuleType("ray")
    ray_data_mod = types.ModuleType("ray.data")

    def init(**kwargs):
        calls["init"] = kwargs

    def read_lance(uri, **kwargs):
        calls["read_lance"] = {"uri": uri, "kwargs": kwargs}
        return FakeDataset()

    ray_mod.init = init
    ray_mod.data = ray_data_mod
    ray_data_mod.read_lance = read_lance
    sys.modules["ray"] = ray_mod
    sys.modules["ray.data"] = ray_data_mod

    RayLanceDataset(
        uri="s3://bucket/train.lance",
        version="7",
        filter_expr="split == 'train'",
        columns=["coord", "segment"],
        override_num_blocks=16,
        storage_options={"aws_endpoint": "http://minio"},
        do_materialize=True,
    )

    assert calls["init"] == {"address": "auto", "ignore_reinit_error": True}
    assert calls["read_lance"]["uri"] == "s3://bucket/train.lance"
    assert calls["read_lance"]["kwargs"] == {
        "version": 7,
        "filter": "split == 'train'",
        "columns": ["coord", "segment"],
        "override_num_blocks": 16,
        "storage_options": {"aws_endpoint": "http://minio"},
    }
    assert calls["randomized"] is True
    assert calls["materialized"] is True


def test_get_shard_prefers_initialized_torch_distributed_rank(monkeypatch):
    _install_fake_torch(
        dist_available=True,
        dist_initialized=True,
        world_size=4,
        rank=3,
    )
    RayLanceDataset = _load_dataset_class()

    monkeypatch.setenv("WORLD_SIZE", "1")
    monkeypatch.setenv("RAYTRAIN_NODE_RANK", "0")
    monkeypatch.setenv("RAYTRAIN_GPUS_PER_NODE", "1")

    class FakeSource:
        def __init__(self):
            self.split_count = None

        def split(self, n):
            self.split_count = n
            return [f"shard{i}" for i in range(n)]

    source = FakeSource()
    dataset = RayLanceDataset.__new__(RayLanceDataset)
    dataset._ds = source

    assert dataset._get_shard() == "shard3"
    assert source.split_count == 4
