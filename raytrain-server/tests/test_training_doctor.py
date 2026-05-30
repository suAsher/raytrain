"""Tests for the doctor readiness checks."""
from __future__ import annotations

from raytrain_server.training.doctor import run_doctor


class _Probe:
    def __init__(self, **kw):
        self._crds = kw.get("crds", set())
        self._gpu = kw.get("gpu", 0)
        self._rdma = kw.get("rdma", 0)
        self._scs = kw.get("scs", [])
        self._ns = kw.get("namespaces", set())

    def crd_exists(self, name):
        return name in self._crds

    def count_gpu_nodes(self, gpu_type=None):
        return self._gpu

    def count_rdma_capacity(self):
        return self._rdma

    def storage_classes(self):
        return self._scs

    def namespace_exists(self, ns):
        return ns in self._ns


def test_all_healthy():
    p = _Probe(
        crds={"rayjobs.ray.io", "workloads.kueue.x-k8s.io"},
        gpu=4, rdma=8, scs=["longhorn"], namespaces={"raytrain-jobs"},
    )
    rep = run_doctor(p, namespace="raytrain-jobs", want_gpu_type="h20")
    assert rep.healthy is True
    names = {c.name for c in rep.results}
    assert names == {"kuberay", "kueue", "gpu_nodes", "rdma", "storage", "namespace"}


def test_missing_kuberay_is_unhealthy():
    p = _Probe(crds=set(), gpu=4, scs=["longhorn"], namespaces={"ns"})
    rep = run_doctor(p, namespace="ns")
    assert rep.healthy is False
    kuberay = [c for c in rep.results if c.name == "kuberay"][0]
    assert kuberay.ok is False


def test_missing_kueue_is_warn_only():
    p = _Probe(crds={"rayjobs.ray.io"}, gpu=4, scs=["longhorn"], namespaces={"ns"})
    rep = run_doctor(p, namespace="ns")
    # kueue missing is warn → still healthy
    assert rep.healthy is True
    kueue = [c for c in rep.results if c.name == "kueue"][0]
    assert kueue.ok is False
    assert kueue.severity == "warn"


def test_no_gpu_nodes_unhealthy():
    p = _Probe(crds={"rayjobs.ray.io"}, gpu=0, scs=["longhorn"], namespaces={"ns"})
    rep = run_doctor(p, namespace="ns")
    assert rep.healthy is False


def test_missing_namespace_unhealthy():
    p = _Probe(crds={"rayjobs.ray.io"}, gpu=4, scs=["longhorn"], namespaces=set())
    rep = run_doctor(p, namespace="missing")
    assert rep.healthy is False
