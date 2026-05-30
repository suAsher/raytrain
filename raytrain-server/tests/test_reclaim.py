"""Tests for the DevSession reclaim loop."""
from __future__ import annotations

import time

from raytrain_server.core.reclaim import reclaim_once
from raytrain_server.core.store import DevSessionStore


class _FakeK8s:
    def __init__(self):
        self.deleted_pods = []
        self.deleted_svcs = []

    def delete_pod(self, name, namespace):
        self.deleted_pods.append(name)

    def delete_service(self, name, namespace):
        self.deleted_svcs.append(name)


def _add(store, sid, *, idle, life, last_seen_off, created_off, now):
    return store.create(
        id=sid,
        workspace_id="w",
        user="u",
        tenant="t",
        image="i",
        gpu_type="h20",
        gpu_count=1,
        pvc_name="p",
        pod_name=f"dev-{sid}",
        service_name=f"dev-{sid}",
        idle_timeout_s=idle,
        max_lifetime_s=life,
        last_seen_at=now - last_seen_off,
        created_at=now - created_off,
    )


def test_reclaims_idle_expired():
    now = time.time()
    store = DevSessionStore()
    _add(store, "old", idle=100, life=10000, last_seen_off=200, created_off=200, now=now)
    _add(store, "fresh", idle=100, life=10000, last_seen_off=10, created_off=10, now=now)

    k8s = _FakeK8s()
    reclaimed = reclaim_once(store, k8s, "raytrain-dev", now=now)

    assert reclaimed == ["old"]
    assert k8s.deleted_pods == ["dev-old"]
    assert store.get("old") is None
    assert store.get("fresh") is not None


def test_reclaims_lifetime_expired_even_if_active():
    now = time.time()
    store = DevSessionStore()
    # active heartbeat but exceeded max lifetime
    _add(store, "long", idle=100000, life=1000, last_seen_off=1, created_off=2000, now=now)

    k8s = _FakeK8s()
    reclaimed = reclaim_once(store, k8s, "raytrain-dev", now=now)
    assert reclaimed == ["long"]


def test_nothing_to_reclaim():
    now = time.time()
    store = DevSessionStore()
    _add(store, "ok", idle=10000, life=100000, last_seen_off=1, created_off=1, now=now)
    k8s = _FakeK8s()
    assert reclaim_once(store, k8s, "raytrain-dev", now=now) == []
    assert k8s.deleted_pods == []
