"""
KueueReader (Task 16 / Req 9): parse real ClusterQueue/LocalQueue CRs into
QueueInfo, link LocalQueue→ClusterQueue, derive gpu_type, sum nominal/used,
and surface KueueUnavailable on read failure.
"""
from __future__ import annotations

import pytest

from raytrain_server.core.kueue_reader import (
    FakeKueueReader,
    K8sKueueReader,
    KueueUnavailable,
    QueueInfo,
    _gpu_type_from_flavor,
    _sum_gpu_nominal,
    _sum_gpu_used,
)


def test_gpu_type_from_flavor():
    assert _gpu_type_from_flavor("h20-flavor") == "H20"
    assert _gpu_type_from_flavor("a100") == "A100"
    assert _gpu_type_from_flavor("cpu-flavor") == "CPU-only"
    assert _gpu_type_from_flavor("weird") == "weird"


_CQ = {
    "metadata": {"name": "cq-h20"},
    "spec": {
        "resourceGroups": [
            {"flavors": [
                {"name": "h20-flavor", "resources": [
                    {"name": "nvidia.com/gpu", "nominalQuota": "64"},
                ]},
            ]},
        ],
    },
    "status": {
        "flavorsReservation": [
            {"name": "h20-flavor", "resources": [
                {"name": "nvidia.com/gpu", "total": "16"},
            ]},
        ],
    },
}


def test_sum_gpu_nominal_and_used():
    nominal, gpu_type = _sum_gpu_nominal(_CQ)
    assert nominal == 64 and gpu_type == "H20"
    assert _sum_gpu_used(_CQ) == 16


def test_k8s_reader_parses_and_links(monkeypatch):
    reader = K8sKueueReader(in_cluster=False)

    def fake_list_cr(plural, namespace=None):
        if plural == "clusterqueues":
            return [_CQ]
        if plural == "localqueues":
            return [{
                "metadata": {"name": "h20-research", "namespace": "team-a"},
                "spec": {"clusterQueue": "cq-h20"},
                "status": {"admittedWorkloads": 3, "pendingWorkloads": 5},
            }]
        return []

    monkeypatch.setattr(reader, "_list_cr", fake_list_cr)
    queues = reader.list_queues()
    assert len(queues) == 1
    q = queues[0]
    assert q.name == "h20-research"
    assert q.namespace == "team-a"
    assert q.cluster_queue == "cq-h20"   # LocalQueue→ClusterQueue link
    assert q.gpu_type == "H20"
    assert q.nominal == 64
    assert q.used == 16
    assert q.admitted == 3 and q.pending == 5


def test_k8s_reader_raises_unavailable(monkeypatch):
    reader = K8sKueueReader(in_cluster=False)

    def boom(plural, namespace=None):
        raise KueueUnavailable("CRD not found")

    monkeypatch.setattr(reader, "_list_cr", boom)
    with pytest.raises(KueueUnavailable):
        reader.list_queues()


def test_fake_reader():
    qi = QueueInfo("q1", "ns", "cq", "H20", 64, 8, 1, 2)
    fake = FakeKueueReader([qi])
    assert fake.list_queues()[0].name == "q1"
    assert fake.get_queue("q1").used == 8
    assert fake.get_queue("nope") is None


def test_fake_reader_failure_mode():
    fake = FakeKueueReader(fail=True)
    with pytest.raises(KueueUnavailable):
        fake.list_queues()
