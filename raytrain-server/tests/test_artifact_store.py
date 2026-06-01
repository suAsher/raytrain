"""
ArtifactStore (real-data artifacts): URI parsing, name classification, MinIO
listing via a fake client, and the Fake double. No real bucket needed.
"""
from __future__ import annotations

import datetime as _dt
import types

from raytrain_server.core.artifact_store import (
    Artifact,
    ArtifactsUnavailable,
    FakeArtifactStore,
    MinioArtifactStore,
    classify,
    split_s3_uri,
)


def test_split_s3_uri():
    assert split_s3_uri("s3://bucket/runs/job-1") == ("bucket", "runs/job-1")
    assert split_s3_uri("minio://b/p") == ("b", "p")
    assert split_s3_uri("") is None
    assert split_s3_uri("pvc://claim/path") is None
    assert split_s3_uri("/checkpoints") is None


def test_classify():
    assert classify("epoch_3.pth") == "checkpoint"
    assert classify("model_final.safetensors") == "model"
    assert classify("train.log") == "log"
    assert classify("eval_results.json") == "eval"
    assert classify("something.bin") == "checkpoint"


class _FakeMinio:
    def __init__(self, objs):
        self._objs = objs

    def list_objects(self, bucket, prefix="", recursive=True):
        return list(self._objs)


def _obj(name, size, when):
    return types.SimpleNamespace(object_name=name, size=size, last_modified=when)


def test_minio_store_lists_and_classifies():
    when = _dt.datetime(2026, 1, 2, 3, 4, 5)
    client = _FakeMinio([
        _obj("runs/job-1/epoch_1.pth", 1_900_000_000, when),
        _obj("runs/job-1/train.log", 4_200_000, when),
        _obj("runs/job-1/", 0, when),            # "directory" → skipped
        _obj("runs/job-1/eval_results.json", 12_000, when),
    ])
    store = MinioArtifactStore(client)
    page = store.list_for_uri("s3://ckpt/runs/job-1")
    assert page.source == "minio"
    names = {a.name for a in page.artifacts}
    assert names == {"epoch_1.pth", "train.log", "eval_results.json"}
    kinds = {a.name: a.kind for a in page.artifacts}
    assert kinds["epoch_1.pth"] == "checkpoint"
    assert kinds["train.log"] == "log"
    assert kinds["eval_results.json"] == "eval"
    sizes = {a.name: a.size for a in page.artifacts}
    assert sizes["train.log"] == "4.0 MB"
    assert all(a.path.startswith("s3://ckpt/") for a in page.artifacts)


def test_minio_store_non_s3_uri_is_unavailable():
    store = MinioArtifactStore(_FakeMinio([]))
    page = store.list_for_uri("pvc://claim/checkpoints")
    assert page.artifacts == [] and page.source == "unavailable"


def test_minio_store_raises_on_list_error():
    class _Boom:
        def list_objects(self, *a, **k):
            raise RuntimeError("network down")

    store = MinioArtifactStore(_Boom())
    try:
        store.list_for_uri("s3://b/p")
        assert False, "expected ArtifactsUnavailable"
    except ArtifactsUnavailable:
        pass


def test_fake_store():
    arts = [Artifact("model_final.pth", "model", "1.8 GB", "s3://b/model_final.pth", "")]
    store = FakeArtifactStore(arts)
    page = store.list_for_uri("s3://b/p")
    assert [a.name for a in page.artifacts] == ["model_final.pth"]
    # non-s3 uri → unavailable regardless of preset
    assert store.list_for_uri("").source == "unavailable"
    # fail mode
    try:
        FakeArtifactStore(fail=True).list_for_uri("s3://b/p")
        assert False
    except ArtifactsUnavailable:
        pass
