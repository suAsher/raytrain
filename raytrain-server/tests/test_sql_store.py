"""Tests for SQL-backed stores (sqlite :memory:) + audit log + db helpers."""
from __future__ import annotations

import time

import pytest

from raytrain_server.core.audit import AuditLog
from raytrain_server.core.db import Database, dumps, loads
from raytrain_server.core.sql_store import (
    SqlDatasetStore,
    SqlDevSessionStore,
    SqlWorkspaceStore,
)


@pytest.fixture
def db() -> Database:
    d = Database("sqlite://")  # in-memory
    d.init_schema()
    return d


# --------------------------------------------------------------------------- #
# db helpers
# --------------------------------------------------------------------------- #


def test_dumps_loads_roundtrip() -> None:
    assert loads(dumps({"a": 1})) == {"a": 1}
    assert loads(dumps(["x", "y"])) == ["x", "y"]
    assert loads(None) == {}
    assert loads("") == {}
    assert loads("not-json") == {}


# --------------------------------------------------------------------------- #
# Workspace
# --------------------------------------------------------------------------- #


class TestSqlWorkspaceStore:
    def test_create_get(self, db: Database) -> None:
        s = SqlWorkspaceStore(db)
        rec = s.create(
            user="z", tenant="occ", name="proj", image="img",
            cpu=4, memory_gi=8, pvc_gi=100,
            ide_urls={"code": "http://x"},
        )
        got = s.get(rec.id)
        assert got is not None
        assert got.user == "z"
        assert got.ide_urls == {"code": "http://x"}

    def test_update_persists(self, db: Database) -> None:
        s = SqlWorkspaceStore(db)
        rec = s.create(user="z", tenant="t", name="n", image="i",
                       cpu=1, memory_gi=1, pvc_gi=10)
        s.update(rec.id, state="running", pod_name="ws-x")
        got = s.get(rec.id)
        assert got.state == "running"
        assert got.pod_name == "ws-x"

    def test_list_and_count(self, db: Database) -> None:
        s = SqlWorkspaceStore(db)
        s.create(user="z", tenant="t", name="a", image="i", cpu=1, memory_gi=1, pvc_gi=1)
        s.create(user="z", tenant="t", name="b", image="i", cpu=1, memory_gi=1, pvc_gi=1)
        s.create(user="other", tenant="t", name="c", image="i", cpu=1, memory_gi=1, pvc_gi=1)
        assert s.count_for_user("z") == 2
        assert len(s.list_for_user("z")) == 2
        assert len(s.list_for_user("z", is_admin=True)) == 3

    def test_delete(self, db: Database) -> None:
        s = SqlWorkspaceStore(db)
        rec = s.create(user="z", tenant="t", name="n", image="i", cpu=1, memory_gi=1, pvc_gi=1)
        assert s.delete(rec.id) is True
        assert s.get(rec.id) is None
        assert s.delete("nope") is False


# --------------------------------------------------------------------------- #
# DevSession
# --------------------------------------------------------------------------- #


class TestSqlDevSessionStore:
    def test_create_get_gpu_accounting(self, db: Database) -> None:
        s = SqlDevSessionStore(db)
        s.create(workspace_id="w", user="z", tenant="t", image="i",
                 gpu_type="h20", gpu_count=2, pvc_name="p", state="running")
        s.create(workspace_id="w", user="z", tenant="t", image="i",
                 gpu_type="h20", gpu_count=1, pvc_name="p", state="running")
        assert s.gpu_in_use_by_user("z") == 3

    def test_expired_detection(self, db: Database) -> None:
        s = SqlDevSessionStore(db)
        now = time.time()
        s.create(workspace_id="w", user="z", tenant="t", image="i",
                 gpu_type="h20", gpu_count=1, pvc_name="p", state="running",
                 idle_timeout_s=100, max_lifetime_s=10000,
                 last_seen_at=now - 500, created_at=now - 500)
        s.create(workspace_id="w", user="z", tenant="t", image="i",
                 gpu_type="h20", gpu_count=1, pvc_name="p", state="running",
                 idle_timeout_s=10000, max_lifetime_s=100000,
                 last_seen_at=now, created_at=now)
        expired = s.expired(now)
        assert len(expired) == 1


# --------------------------------------------------------------------------- #
# Dataset
# --------------------------------------------------------------------------- #


class TestSqlDatasetStore:
    def test_visibility_filtering(self, db: Database) -> None:
        s = SqlDatasetStore(db)
        s.create(name="mine", type="lance", uri="s3://a", owner="z",
                 tenant="occ", visibility="private")
        s.create(name="team", type="lance", uri="s3://b", owner="other",
                 tenant="occ", visibility="tenant")
        s.create(name="pub", type="lance", uri="s3://c", owner="other",
                 tenant="nlp", visibility="public")
        s.create(name="hidden", type="lance", uri="s3://d", owner="other",
                 tenant="nlp", visibility="private")

        vis = s.visible_to("z", "occ")
        names = {r.name for r in vis}
        assert names == {"mine", "team", "pub"}

    def test_update_and_delete(self, db: Database) -> None:
        s = SqlDatasetStore(db)
        rec = s.create(name="d", type="lance", uri="s3://x", owner="z",
                       tenant="t", visibility="private", tags=["a"])
        s.update(rec.id, visibility="public", tags=["a", "b"])
        got = s.get(rec.id)
        assert got.visibility == "public"
        assert got.tags == ["a", "b"]
        assert s.delete(rec.id) is True
        assert s.get(rec.id) is None


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


class TestAudit:
    def test_record_and_search(self, db: Database) -> None:
        a = AuditLog(db)
        a.record("z", "submit_job", "job-1", "ok", "detail")
        a.record("z", "create_workspace", "ws-1", "ok")
        a.record("lisi", "stop_job", "job-2", "ok")

        all_z = a.search(user="z")
        assert len(all_z) == 2
        assert all_z[0]["action"] in ("submit_job", "create_workspace")

        everything = a.search()
        assert len(everything) == 3

    def test_search_without_db_returns_empty(self) -> None:
        a = AuditLog(None)
        a.record("z", "x")  # no-op write, just stdout
        assert a.search() == []


# --------------------------------------------------------------------------- #
# Cross-store: persistence survives "reopening" the same sqlite file
# --------------------------------------------------------------------------- #


def test_persistence_survives_reconnect(tmp_path) -> None:
    dbfile = tmp_path / "state.db"
    url = f"sqlite:///{dbfile}"

    db1 = Database(url)
    db1.init_schema()
    s1 = SqlWorkspaceStore(db1)
    rec = s1.create(user="z", tenant="t", name="persist", image="i",
                    cpu=1, memory_gi=1, pvc_gi=1)

    # New Database object pointing at the same file
    db2 = Database(url)
    s2 = SqlWorkspaceStore(db2)
    got = s2.get(rec.id)
    assert got is not None
    assert got.name == "persist"
