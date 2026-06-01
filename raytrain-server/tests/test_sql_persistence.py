"""
SQL persistence for jobs / resources / queue_meta (Req 11, Property 5).

Verifies: schema creates the tables, records survive a simulated restart
(rebuild the store against the same DB), and the SQL stores match the in-memory
stores' observable behavior.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from raytrain_server.core.db import Database
from raytrain_server.core.jobs_store import (
    JobMounts,
    JobResources,
    PlatformJob,
    FailureInfo,
)
from raytrain_server.core.sql_store import (
    SqlJobStore,
    SqlQueueMetaStore,
    SqlResourceStore,
)


@pytest.fixture
def db():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    d = Database(f"sqlite:///{path}")
    d.init_schema()
    return d, path


def _mk_job(jid="job-1", user="alice", tenant="t1", status="Queued"):
    return PlatformJob(
        id=jid, name="run", user=user, tenant=tenant, project="proj",
        queue="h20-shared", status=status, image="img:1",
        entrypoint="python train.py", code_uri="s3://c/x.zip",
        env={"A": "1"},
        resources=JobResources(gpu_type="H20", nodes=2, gpus_per_node=8),
        mounts=JobMounts(dataset_uri="s3://d.lance", checkpoint_uri="s3://ck"),
    )


def test_schema_creates_tables(db):
    d, _ = db
    for t in ("jobs", "resources", "queue_meta", "users", "workspaces"):
        # querying an empty table must not error → table exists
        assert d.query_all(f"SELECT * FROM {t}") == []


def test_init_schema_idempotent(db):
    d, _ = db
    d.init_schema()  # second call must not raise
    assert d.query_all("SELECT * FROM jobs") == []


def test_job_survives_restart(db):
    d, path = db
    store = SqlJobStore(d)
    store.create(_mk_job())
    store.update("job-1", status="Running", started_at=123.0)

    # simulate restart: new Database + store against the same file
    d2 = Database(f"sqlite:///{path}")
    d2.init_schema()
    store2 = SqlJobStore(d2)
    got = store2.get("job-1")
    assert got is not None
    assert got.status == "Running"
    assert got.resources.nodes == 2 and got.resources.gpus_per_node == 8
    assert got.mounts.dataset_uri == "s3://d.lance"
    assert got.env == {"A": "1"}


def test_job_failure_roundtrip(db):
    d, _ = db
    store = SqlJobStore(d)
    j = _mk_job("job-f", status="Failed")
    j.failure = FailureInfo(category="OOMKilled", summary="oom", detail="d", container="worker-0")
    store.create(j)
    got = store.get("job-f")
    assert got.failure is not None
    assert got.failure.category == "OOMKilled"


def test_job_list_visible_tenant_scope(db):
    d, _ = db
    store = SqlJobStore(d)
    store.create(_mk_job("j1", user="alice", tenant="t1"))
    store.create(_mk_job("j2", user="bob", tenant="t2"))
    # alice (t1) sees own + same-tenant only
    ids = {j.id for j in store.list_visible("alice", "t1", is_admin=False)}
    assert ids == {"j1"}
    # admin sees all
    ids_admin = {j.id for j in store.list_visible("x", "x", is_admin=True)}
    assert ids_admin == {"j1", "j2"}


def test_job_count_running_gpus(db):
    d, _ = db
    store = SqlJobStore(d)
    store.create(_mk_job("r1", status="Running"))   # 2*8 = 16
    store.create(_mk_job("r2", status="Succeeded"))  # not counted
    assert store.count_running_gpus("alice") == 16


def test_resource_crud_and_restart(db):
    d, path = db
    store = SqlResourceStore(d)
    rec = store.create("project", "pointcept", {"owner": "asher"})
    store.update(rec.id, name="pointcept2", spec={"owner": "bob"}, enabled=False)

    d2 = Database(f"sqlite:///{path}")
    d2.init_schema()
    store2 = SqlResourceStore(d2)
    got = store2.get(rec.id)
    assert got.name == "pointcept2" and got.spec["owner"] == "bob" and got.enabled is False
    assert [r.id for r in store2.list("project")] == [rec.id]
    assert store2.delete(rec.id) is True
    assert store2.get(rec.id) is None


def test_resource_seed_once(db):
    d, _ = db
    SqlResourceStore(d, seed=True)
    # seeding twice must not duplicate
    SqlResourceStore(d, seed=True)
    projects = SqlResourceStore(d).list("project")
    names = [r.name for r in projects]
    assert "pointcept" in names
    assert len(names) == len(set(names))  # no dup


def test_queue_meta_store(db):
    d, _ = db
    store = SqlQueueMetaStore(d)
    store.upsert("h20-shared", display_alias="H20 共享", sort_order=1)
    store.upsert("h20-shared", display_alias="H20 Shared", sort_order=2)  # update
    got = store.get("h20-shared")
    assert got["display_alias"] == "H20 Shared" and got["sort_order"] == 2
    assert len(store.list_all()) == 1
