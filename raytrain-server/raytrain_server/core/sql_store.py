"""
SQL-backed implementations of the *Store classes.

Same method signatures as the in-memory versions in store.py, so the API
layer is agnostic. Selected at startup via configure_stores() when
RAYTRAIN_DATABASE_URL is set.
"""
from __future__ import annotations

import time
import uuid

from .db import Database, dumps, loads
from .store import DatasetRecord, DevSessionRecord, WorkspaceRecord


# --------------------------------------------------------------------------- #
# Workspace
# --------------------------------------------------------------------------- #


class SqlWorkspaceStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _to_rec(self, row: dict) -> WorkspaceRecord:
        return WorkspaceRecord(
            id=row["id"], user=row["user"], tenant=row["tenant"],
            name=row["name"], image=row["image"],
            cpu=row["cpu"], memory_gi=row["memory_gi"], pvc_gi=row["pvc_gi"],
            state=row["state"], pod_name=row["pod_name"] or "",
            pvc_name=row["pvc_name"] or "", service_name=row["service_name"] or "",
            ide_urls=loads(row["ide_urls"]),
            created_at=row["created_at"] or time.time(),
            last_active_at=row["last_active_at"] or time.time(),
        )

    def create(self, **kw) -> WorkspaceRecord:
        wid = kw.pop("id", None) or uuid.uuid4().hex[:10]
        rec = WorkspaceRecord(id=wid, **kw)
        self._db.execute(
            'INSERT INTO workspaces (id,"user",tenant,name,image,cpu,memory_gi,'
            "pvc_gi,state,pod_name,pvc_name,service_name,ide_urls,created_at,"
            "last_active_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.id, rec.user, rec.tenant, rec.name, rec.image, rec.cpu,
             rec.memory_gi, rec.pvc_gi, rec.state, rec.pod_name, rec.pvc_name,
             rec.service_name, dumps(rec.ide_urls), rec.created_at,
             rec.last_active_at),
        )
        return rec

    def get(self, wid: str):
        row = self._db.query_one("SELECT * FROM workspaces WHERE id=?", (wid,))
        return self._to_rec(row) if row else None

    def update(self, wid: str, **changes):
        rec = self.get(wid)
        if not rec:
            return None
        for k, v in changes.items():
            setattr(rec, k, v)
        self._db.execute(
            "UPDATE workspaces SET state=?,pod_name=?,pvc_name=?,service_name=?,"
            "ide_urls=?,last_active_at=? WHERE id=?",
            (rec.state, rec.pod_name, rec.pvc_name, rec.service_name,
             dumps(rec.ide_urls), rec.last_active_at, wid),
        )
        return rec

    def delete(self, wid: str) -> bool:
        existed = self.get(wid) is not None
        self._db.execute("DELETE FROM workspaces WHERE id=?", (wid,))
        return existed

    def list_for_user(self, user: str, is_admin: bool = False):
        if is_admin:
            rows = self._db.query_all("SELECT * FROM workspaces")
        else:
            rows = self._db.query_all(
                'SELECT * FROM workspaces WHERE "user"=?', (user,)
            )
        return [self._to_rec(r) for r in rows]

    def count_for_user(self, user: str) -> int:
        row = self._db.query_one(
            'SELECT COUNT(*) AS c FROM workspaces WHERE "user"=?', (user,)
        )
        return int(row["c"]) if row else 0


# --------------------------------------------------------------------------- #
# DevSession
# --------------------------------------------------------------------------- #


class SqlDevSessionStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _to_rec(self, row: dict) -> DevSessionRecord:
        return DevSessionRecord(
            id=row["id"], workspace_id=row["workspace_id"], user=row["user"],
            tenant=row["tenant"], image=row["image"], gpu_type=row["gpu_type"],
            gpu_count=row["gpu_count"], pvc_name=row["pvc_name"],
            state=row["state"], pod_name=row["pod_name"] or "",
            service_name=row["service_name"] or "", ide_urls=loads(row["ide_urls"]),
            created_at=row["created_at"] or time.time(),
            last_seen_at=row["last_seen_at"] or time.time(),
            idle_timeout_s=row["idle_timeout_s"], max_lifetime_s=row["max_lifetime_s"],
        )

    def create(self, **kw) -> DevSessionRecord:
        sid = kw.pop("id", None) or uuid.uuid4().hex[:10]
        rec = DevSessionRecord(id=sid, **kw)
        self._db.execute(
            'INSERT INTO dev_sessions (id,workspace_id,"user",tenant,image,'
            "gpu_type,gpu_count,pvc_name,state,pod_name,service_name,ide_urls,"
            "created_at,last_seen_at,idle_timeout_s,max_lifetime_s) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.id, rec.workspace_id, rec.user, rec.tenant, rec.image,
             rec.gpu_type, rec.gpu_count, rec.pvc_name, rec.state, rec.pod_name,
             rec.service_name, dumps(rec.ide_urls), rec.created_at,
             rec.last_seen_at, rec.idle_timeout_s, rec.max_lifetime_s),
        )
        return rec

    def get(self, sid: str):
        row = self._db.query_one("SELECT * FROM dev_sessions WHERE id=?", (sid,))
        return self._to_rec(row) if row else None

    def update(self, sid: str, **changes):
        rec = self.get(sid)
        if not rec:
            return None
        for k, v in changes.items():
            setattr(rec, k, v)
        self._db.execute(
            "UPDATE dev_sessions SET state=?,pod_name=?,service_name=?,"
            "ide_urls=?,last_seen_at=? WHERE id=?",
            (rec.state, rec.pod_name, rec.service_name, dumps(rec.ide_urls),
             rec.last_seen_at, sid),
        )
        return rec

    def delete(self, sid: str) -> bool:
        existed = self.get(sid) is not None
        self._db.execute("DELETE FROM dev_sessions WHERE id=?", (sid,))
        return existed

    def list_for_user(self, user: str, is_admin: bool = False):
        if is_admin:
            rows = self._db.query_all("SELECT * FROM dev_sessions")
        else:
            rows = self._db.query_all(
                'SELECT * FROM dev_sessions WHERE "user"=?', (user,)
            )
        return [self._to_rec(r) for r in rows]

    def gpu_in_use_by_user(self, user: str) -> int:
        rows = self._db.query_all(
            'SELECT gpu_count, state FROM dev_sessions WHERE "user"=?', (user,)
        )
        return sum(
            r["gpu_count"] for r in rows
            if r["state"] in ("creating", "running")
        )

    def expired(self, now: float | None = None):
        now = now or time.time()
        rows = self._db.query_all("SELECT * FROM dev_sessions")
        recs = [self._to_rec(r) for r in rows]
        return [r for r in recs if r.is_expired(now)]


# --------------------------------------------------------------------------- #
# Dataset
# --------------------------------------------------------------------------- #


class SqlDatasetStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def _to_rec(self, row: dict) -> DatasetRecord:
        return DatasetRecord(
            id=row["id"], name=row["name"], type=row["type"], uri=row["uri"],
            owner=row["owner"], tenant=row["tenant"], version=row["version"],
            visibility=row["visibility"], schema_json=loads(row["schema_json"]),
            rows=row["rows"], size_bytes=row["size_bytes"],
            tags=loads(row["tags"]) if row["tags"] else [],
            description=row["description"] or "",
            created_at=row["created_at"] or time.time(),
        )

    def create(self, **kw) -> DatasetRecord:
        did = kw.pop("id", None) or uuid.uuid4().hex[:10]
        rec = DatasetRecord(id=did, **kw)
        self._db.execute(
            "INSERT INTO datasets (id,name,type,uri,owner,tenant,version,"
            "visibility,schema_json,rows,size_bytes,tags,description,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.id, rec.name, rec.type, rec.uri, rec.owner, rec.tenant,
             rec.version, rec.visibility, dumps(rec.schema_json), rec.rows,
             rec.size_bytes, dumps(rec.tags), rec.description, rec.created_at),
        )
        return rec

    def get(self, did: str):
        row = self._db.query_one("SELECT * FROM datasets WHERE id=?", (did,))
        return self._to_rec(row) if row else None

    def update(self, did: str, **changes):
        rec = self.get(did)
        if not rec:
            return None
        for k, v in changes.items():
            setattr(rec, k, v)
        self._db.execute(
            "UPDATE datasets SET visibility=?,tags=?,description=?,"
            "schema_json=?,rows=?,size_bytes=? WHERE id=?",
            (rec.visibility, dumps(rec.tags), rec.description,
             dumps(rec.schema_json), rec.rows, rec.size_bytes, did),
        )
        return rec

    def delete(self, did: str) -> bool:
        existed = self.get(did) is not None
        self._db.execute("DELETE FROM datasets WHERE id=?", (did,))
        return existed

    def visible_to(self, user: str, tenant: str, is_admin: bool = False):
        rows = self._db.query_all("SELECT * FROM datasets")
        recs = [self._to_rec(r) for r in rows]
        if is_admin:
            return recs
        out = []
        for r in recs:
            if r.visibility == "public":
                out.append(r)
            elif r.visibility == "tenant" and r.tenant == tenant:
                out.append(r)
            elif r.owner == user:
                out.append(r)
        return out

    def can_access(self, rec, user, tenant, is_admin):
        if is_admin or rec.owner == user:
            return True
        if rec.visibility == "public":
            return True
        if rec.visibility == "tenant" and rec.tenant == tenant:
            return True
        return False


# --------------------------------------------------------------------------- #
# Users (platform RBAC: per-user quota + grants)
# --------------------------------------------------------------------------- #


class SqlUserStore:
    """SQL-backed UserStore. Same interface as the in-memory users.UserStore."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def _to_rec(self, row: dict):
        from .users import UserQuota, UserRecord
        q = loads(row["quota"]) or {}
        return UserRecord(
            user=row["user"],
            tenant=row["tenant"],
            role=row["role"],
            quota=UserQuota(
                max_gpus=int(q.get("max_gpus", 0) or 0),
                max_jobs=int(q.get("max_jobs", 0) or 0),
                max_cpus=int(q.get("max_cpus", 0) or 0),
                max_memory_gi=int(q.get("max_memory_gi", 0) or 0),
            ),
            projects=loads(row["projects"]) or [],
            queues=loads(row["queues"]) or [],
            datasets=loads(row["datasets"]) or [],
            image_prefixes=loads(row["image_prefixes"]) or [],
            enabled=bool(row["enabled"]),
            password_hash=(row["password_hash"] if "password_hash" in row.keys() else "") or "",
            created_at=row["created_at"] or time.time(),
            updated_at=row["updated_at"] or time.time(),
        )

    def create(self, rec):
        if self.get(rec.user) is not None:
            raise ValueError(f"user {rec.user!r} already exists")
        self._db.execute(
            'INSERT INTO users ("user",tenant,role,quota,projects,queues,'
            "datasets,image_prefixes,enabled,password_hash,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.user, rec.tenant, rec.role, dumps(rec.quota.to_dict()),
             dumps(rec.projects), dumps(rec.queues), dumps(rec.datasets),
             dumps(rec.image_prefixes), 1 if rec.enabled else 0,
             rec.password_hash, rec.created_at, rec.updated_at),
        )
        return rec

    def get(self, user: str):
        row = self._db.query_one('SELECT * FROM users WHERE "user"=?', (user,))
        return self._to_rec(row) if row else None

    def update(self, user: str, **changes):
        rec = self.get(user)
        if not rec:
            return None
        for k, v in changes.items():
            setattr(rec, k, v)
        rec.updated_at = time.time()
        self._db.execute(
            'UPDATE users SET tenant=?,role=?,quota=?,projects=?,queues=?,'
            'datasets=?,image_prefixes=?,enabled=?,password_hash=?,updated_at=? '
            'WHERE "user"=?',
            (rec.tenant, rec.role, dumps(rec.quota.to_dict()),
             dumps(rec.projects), dumps(rec.queues), dumps(rec.datasets),
             dumps(rec.image_prefixes), 1 if rec.enabled else 0,
             rec.password_hash, rec.updated_at, user),
        )
        return rec

    def delete(self, user: str) -> bool:
        existed = self.get(user) is not None
        self._db.execute('DELETE FROM users WHERE "user"=?', (user,))
        return existed

    def list_all(self):
        rows = self._db.query_all("SELECT * FROM users")
        return [self._to_rec(r) for r in rows]


# --------------------------------------------------------------------------- #
# Jobs (platform-side training job records)
# --------------------------------------------------------------------------- #


class SqlJobStore:
    """SQL-backed JobStore. Same interface as the in-memory jobs_store.JobStore."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def _to_rec(self, row: dict):
        from .jobs_store import FailureInfo, JobMounts, JobResources, PlatformJob

        res = loads(row["resources"]) or {}
        mnt = loads(row["mounts"]) or {}
        fail_raw = loads(row["failure"]) if row["failure"] else None
        failure = None
        if fail_raw:
            failure = FailureInfo(
                category=fail_raw.get("category", ""),
                summary=fail_raw.get("summary", ""),
                detail=fail_raw.get("detail", ""),
                container=fail_raw.get("container", ""),
                log_anchor=int(fail_raw.get("log_anchor", 0) or 0),
            )
        return PlatformJob(
            id=row["id"], name=row["name"], user=row["user"], tenant=row["tenant"],
            project=row["project"], queue=row["queue"],
            quota_group=row["quota_group"] or "", priority=row["priority"] or "normal",
            status=row["status"] or "Queued", image=row["image"] or "",
            entrypoint=row["entrypoint"] or "", working_dir=row["working_dir"] or "",
            git_ref=row["git_ref"] or "", env=loads(row["env"]) or {},
            submission_id=row["submission_id"] or "", code_uri=row["code_uri"] or "",
            resources=JobResources(**res) if res else JobResources(),
            mounts=JobMounts(**mnt) if mnt else JobMounts(),
            failure=failure, description=row["description"] or "",
            created_at=row["created_at"] or time.time(),
            started_at=row["started_at"] or 0.0,
            finished_at=row["finished_at"] or 0.0,
            experiment=row["experiment"] or "",
        )

    def _failure_json(self, rec) -> str | None:
        return dumps(rec.failure.to_dict()) if rec.failure else None

    def create(self, rec):
        if not rec.id:
            rec.id = "job-" + uuid.uuid4().hex[:8]
        self._db.execute(
            'INSERT INTO jobs (id,name,"user",tenant,project,queue,quota_group,'
            "priority,status,image,entrypoint,working_dir,git_ref,env,"
            "submission_id,code_uri,resources,mounts,failure,description,"
            "experiment,created_at,started_at,finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.id, rec.name, rec.user, rec.tenant, rec.project, rec.queue,
             rec.quota_group, rec.priority, rec.status, rec.image, rec.entrypoint,
             rec.working_dir, rec.git_ref, dumps(rec.env), rec.submission_id,
             rec.code_uri, dumps(rec.resources.to_dict()), dumps(rec.mounts.to_dict()),
             self._failure_json(rec), rec.description, rec.experiment,
             rec.created_at, rec.started_at, rec.finished_at),
        )
        return rec

    def get(self, jid: str):
        row = self._db.query_one("SELECT * FROM jobs WHERE id=?", (jid,))
        return self._to_rec(row) if row else None

    def update(self, jid: str, **changes):
        rec = self.get(jid)
        if not rec:
            return None
        for k, v in changes.items():
            setattr(rec, k, v)
        self._db.execute(
            "UPDATE jobs SET name=?,status=?,image=?,entrypoint=?,working_dir=?,"
            "git_ref=?,env=?,submission_id=?,code_uri=?,resources=?,mounts=?,"
            "failure=?,description=?,experiment=?,queue=?,quota_group=?,priority=?,"
            "started_at=?,finished_at=? WHERE id=?",
            (rec.name, rec.status, rec.image, rec.entrypoint, rec.working_dir,
             rec.git_ref, dumps(rec.env), rec.submission_id, rec.code_uri,
             dumps(rec.resources.to_dict()), dumps(rec.mounts.to_dict()),
             self._failure_json(rec), rec.description, rec.experiment, rec.queue,
             rec.quota_group, rec.priority, rec.started_at, rec.finished_at, jid),
        )
        return rec

    def delete(self, jid: str) -> bool:
        existed = self.get(jid) is not None
        self._db.execute("DELETE FROM jobs WHERE id=?", (jid,))
        return existed

    def list_visible(self, user: str, tenant: str, is_admin: bool):
        rows = self._db.query_all("SELECT * FROM jobs ORDER BY created_at DESC")
        recs = [self._to_rec(r) for r in rows]
        if is_admin:
            return recs
        return [j for j in recs if j.user == user or j.tenant == tenant]

    def count_running_gpus(self, user: str) -> int:
        rows = self._db.query_all('SELECT * FROM jobs WHERE "user"=?', (user,))
        total = 0
        for r in rows:
            if r["status"] in ("Queued", "Starting", "Running"):
                res = loads(r["resources"]) or {}
                gt = (res.get("gpu_type") or "").upper()
                if gt != "CPU-ONLY":
                    total += int(res.get("nodes", 0) or 0) * int(res.get("gpus_per_node", 0) or 0)
        return total


# --------------------------------------------------------------------------- #
# Resources (admin catalog: project / quota_group / runtime_image)
# --------------------------------------------------------------------------- #


class SqlResourceStore:
    """SQL-backed ResourceStore. Same interface as resources_store.ResourceStore."""

    def __init__(self, db: Database, seed: bool = False) -> None:
        self._db = db
        if seed and not self._db.query_all("SELECT id FROM resources LIMIT 1"):
            self._seed()

    def _seed(self) -> None:
        from .resources_store import ResourceStore

        mem = ResourceStore(seed=True)
        for kind in ("project", "quota_group", "runtime_image"):
            for r in mem.list(kind):
                self._db.execute(
                    "INSERT INTO resources (id,kind,name,spec,enabled,created_at,"
                    "updated_at) VALUES (?,?,?,?,?,?,?)",
                    (r.id, r.kind, r.name, dumps(r.spec), 1 if r.enabled else 0,
                     r.created_at, r.updated_at),
                )

    def _to_rec(self, row: dict):
        from .resources_store import ResourceRecord

        return ResourceRecord(
            id=row["id"], kind=row["kind"], name=row["name"],
            spec=loads(row["spec"]) or {}, enabled=bool(row["enabled"]),
            created_at=row["created_at"] or time.time(),
            updated_at=row["updated_at"] or time.time(),
        )

    def list(self, kind: str):
        rows = self._db.query_all("SELECT * FROM resources WHERE kind=?", (kind,))
        return [self._to_rec(r) for r in rows]

    def get(self, rid: str):
        row = self._db.query_one("SELECT * FROM resources WHERE id=?", (rid,))
        return self._to_rec(row) if row else None

    def create(self, kind: str, name: str, spec: dict):
        from .resources_store import ResourceRecord

        rid = f"{kind[:3]}-{uuid.uuid4().hex[:8]}"
        rec = ResourceRecord(id=rid, kind=kind, name=name, spec=spec or {})
        self._db.execute(
            "INSERT INTO resources (id,kind,name,spec,enabled,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            (rec.id, rec.kind, rec.name, dumps(rec.spec), 1 if rec.enabled else 0,
             rec.created_at, rec.updated_at),
        )
        return rec

    def update(self, rid: str, **changes):
        rec = self.get(rid)
        if not rec:
            return None
        for k, v in changes.items():
            setattr(rec, k, v)
        rec.updated_at = time.time()
        self._db.execute(
            "UPDATE resources SET name=?,spec=?,enabled=?,updated_at=? WHERE id=?",
            (rec.name, dumps(rec.spec), 1 if rec.enabled else 0, rec.updated_at, rid),
        )
        return rec

    def delete(self, rid: str) -> bool:
        existed = self.get(rid) is not None
        self._db.execute("DELETE FROM resources WHERE id=?", (rid,))
        return existed


# --------------------------------------------------------------------------- #
# Queue display metadata (platform-owned only; usage comes live from Kueue)
# --------------------------------------------------------------------------- #


class SqlQueueMetaStore:
    """Persists only display alias/sort for queues. Usage is never stored here."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, name: str) -> dict | None:
        return self._db.query_one("SELECT * FROM queue_meta WHERE name=?", (name,))

    def list_all(self) -> list[dict]:
        return self._db.query_all("SELECT * FROM queue_meta")

    def upsert(self, name: str, display_alias: str = "", sort_order: int = 0) -> None:
        if self.get(name):
            self._db.execute(
                "UPDATE queue_meta SET display_alias=?,sort_order=? WHERE name=?",
                (display_alias, sort_order, name),
            )
        else:
            self._db.execute(
                "INSERT INTO queue_meta (name,display_alias,sort_order,created_at) "
                "VALUES (?,?,?,?)",
                (name, display_alias, sort_order, time.time()),
            )

    def delete(self, name: str) -> bool:
        existed = self.get(name) is not None
        self._db.execute("DELETE FROM queue_meta WHERE name=?", (name,))
        return existed
