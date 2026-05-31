"""
Admin-managed platform resources: Projects, QuotaGroups, Runtime Images.

These are lightweight catalog records the platform owns (not K8s CRDs). Queues
have their own richer store (queues_store) because they carry live usage; the
three kinds here are flat name + spec records with admin CRUD.

One generic ``ResourceStore`` keyed by kind keeps it DRY; each kind seeds a few
sensible defaults so a fresh platform's Admin page isn't empty.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

PROJECT = "project"
QUOTA_GROUP = "quota_group"
RUNTIME_IMAGE = "runtime_image"

KINDS = (PROJECT, QUOTA_GROUP, RUNTIME_IMAGE)


@dataclass
class ResourceRecord:
    id: str
    kind: str
    name: str
    # free-form spec fields per kind, kept as a flat dict so the API/UI can
    # render them uniformly. Examples:
    #   project:       {owner, description}
    #   quota_group:   {gpu_type, max_gpus, max_cpus}
    #   runtime_image: {uri, cuda, framework}
    spec: dict = field(default_factory=dict)
    enabled: bool = True
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


class ResourceStore:
    def __init__(self, seed: bool = True) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, ResourceRecord] = {}
        if seed:
            self._seed()

    def _seed(self) -> None:
        defaults = [
            ResourceRecord("p-pointcept", PROJECT, "pointcept",
                           {"owner": "asher", "description": "Pointcept 3D 感知"}),
            ResourceRecord("p-sslod26", PROJECT, "sslod26",
                           {"owner": "asher", "description": "sslod26 自监督预训练"}),
            ResourceRecord("p-occ", PROJECT, "occ-world",
                           {"owner": "lisi", "description": "occupancy world model"}),
            ResourceRecord("qg-pointcept", QUOTA_GROUP, "pointcept-qg",
                           {"gpu_type": "H20", "max_gpus": 32, "max_cpus": 256}),
            ResourceRecord("qg-sslod26", QUOTA_GROUP, "sslod26-qg",
                           {"gpu_type": "H20", "max_gpus": 48, "max_cpus": 384}),
            ResourceRecord("img-pointcept", RUNTIME_IMAGE, "raytrain/pointcept:cu124-v3",
                           {"uri": "raytrain/pointcept:cu124-v3", "cuda": "12.4", "framework": "torch 2.4"}),
            ResourceRecord("img-sslod26", RUNTIME_IMAGE, "raytrain/sslod26:cu124-v3",
                           {"uri": "raytrain/sslod26:cu124-v3", "cuda": "12.4", "framework": "torch 2.4"}),
            ResourceRecord("img-occ", RUNTIME_IMAGE, "raytrain/occworld:cu121-v2",
                           {"uri": "raytrain/occworld:cu121-v2", "cuda": "12.1", "framework": "torch 2.3"}),
        ]
        for r in defaults:
            self._items[r.id] = r

    def list(self, kind: str) -> list[ResourceRecord]:
        with self._lock:
            return [r for r in self._items.values() if r.kind == kind]

    def get(self, rid: str) -> Optional[ResourceRecord]:
        with self._lock:
            return self._items.get(rid)

    def create(self, kind: str, name: str, spec: dict) -> ResourceRecord:
        with self._lock:
            rid = f"{kind[:3]}-{uuid.uuid4().hex[:8]}"
            rec = ResourceRecord(id=rid, kind=kind, name=name, spec=spec or {})
            self._items[rid] = rec
            return rec

    def update(self, rid: str, **changes) -> Optional[ResourceRecord]:
        with self._lock:
            rec = self._items.get(rid)
            if not rec:
                return None
            for k, v in changes.items():
                setattr(rec, k, v)
            rec.updated_at = time.time()
            return rec

    def delete(self, rid: str) -> bool:
        with self._lock:
            return self._items.pop(rid, None) is not None


_resource_store: Optional[ResourceStore] = None


def get_resource_store() -> ResourceStore:
    global _resource_store
    if _resource_store is None:
        _resource_store = ResourceStore()
    return _resource_store


def set_resource_store(s: ResourceStore) -> None:
    global _resource_store
    _resource_store = s
