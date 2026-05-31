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
