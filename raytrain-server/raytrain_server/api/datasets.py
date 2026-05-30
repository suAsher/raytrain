"""
``/v1/datasets`` — the Lance-first dataset registry.

Datasets are platform-level indexes (name + URI + metadata + visibility).
They do NOT copy data — the URI points at wherever the data lives (public
bucket like occ-lance, or a user's personal bucket). Visibility (private /
tenant / public) controls who can discover & select them when submitting.

On registration we best-effort scan Lance metadata (schema / rows / version);
failures don't block registration.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..core.jwt_auth import Identity, require_user
from ..core.lance_meta import scan_lance
from ..core.settings import Settings, get_settings
from ..core.store import DatasetRecord, DatasetStore, get_dataset_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/datasets", tags=["datasets"])

_VALID_TYPES = {"lance", "parquet", "dir"}
_VALID_VIS = {"private", "tenant", "public"}


class RegisterDatasetRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    type: str = Field(default="lance")
    uri: str = Field(..., min_length=1)
    version: str = "latest"
    visibility: str = Field(default="private")
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    scan_metadata: bool = True


class PatchDatasetRequest(BaseModel):
    visibility: str | None = None
    tags: list[str] | None = None
    description: str | None = None


class DatasetResponse(BaseModel):
    id: str
    name: str
    type: str
    uri: str
    version: str
    visibility: str
    owner: str
    tenant: str
    rows: int
    size_bytes: int
    arrow_schema: dict = Field(default_factory=dict)
    tags: list[str]
    description: str

    @classmethod
    def from_record(cls, r: DatasetRecord) -> "DatasetResponse":
        return cls(
            id=r.id, name=r.name, type=r.type, uri=r.uri, version=r.version,
            visibility=r.visibility, owner=r.owner, tenant=r.tenant,
            rows=r.rows, size_bytes=r.size_bytes, arrow_schema=r.schema_json,
            tags=r.tags, description=r.description,
        )


def _store() -> DatasetStore:
    return get_dataset_store()


def _storage_options(settings: Settings) -> dict:
    opts = {}
    if settings.minio_endpoint:
        opts["aws_endpoint"] = settings.minio_endpoint
        opts["endpoint"] = settings.minio_endpoint
        opts["allow_http"] = "true"
    if settings.minio_access_key:
        opts["aws_access_key_id"] = settings.minio_access_key
        opts["access_key_id"] = settings.minio_access_key
    if settings.minio_secret_key:
        opts["aws_secret_access_key"] = settings.minio_secret_key
        opts["secret_access_key"] = settings.minio_secret_key
    return opts


@router.post("", response_model=DatasetResponse, status_code=201)
def register_dataset(
    body: RegisterDatasetRequest,
    identity: Identity = Depends(require_user),
    store: DatasetStore = Depends(_store),
    settings: Settings = Depends(get_settings),
) -> DatasetResponse:
    if body.type not in _VALID_TYPES:
        raise HTTPException(400, detail=f"type must be one of {sorted(_VALID_TYPES)}")
    if body.visibility not in _VALID_VIS:
        raise HTTPException(400, detail=f"visibility must be one of {sorted(_VALID_VIS)}")

    rows, size_bytes, schema_json, version = 0, 0, {}, body.version
    if body.scan_metadata and body.type == "lance":
        meta = scan_lance(body.uri, storage_options=_storage_options(settings))
        if meta.ok:
            rows = meta.rows
            size_bytes = meta.size_bytes
            schema_json = meta.schema_json
            # keep user-specified version if given, else the scanned one
            if body.version in ("", "latest"):
                version = meta.version

    rec = store.create(
        name=body.name,
        type=body.type,
        uri=body.uri,
        owner=identity.user,
        tenant=identity.tenant,
        version=version,
        visibility=body.visibility,
        schema_json=schema_json,
        rows=rows,
        size_bytes=size_bytes,
        tags=body.tags,
        description=body.description,
    )
    log.info("dataset.register id=%s name=%s owner=%s vis=%s",
             rec.id, rec.name, identity.user, rec.visibility)
    return DatasetResponse.from_record(rec)


@router.get("", response_model=list[DatasetResponse])
def list_datasets(
    identity: Identity = Depends(require_user),
    store: DatasetStore = Depends(_store),
) -> list[DatasetResponse]:
    recs = store.visible_to(
        identity.user, identity.tenant, is_admin=identity.is_admin
    )
    return [DatasetResponse.from_record(r) for r in recs]


@router.get("/{did}", response_model=DatasetResponse)
def get_dataset(
    did: str,
    identity: Identity = Depends(require_user),
    store: DatasetStore = Depends(_store),
) -> DatasetResponse:
    rec = store.get(did)
    if not rec:
        raise HTTPException(404, detail="dataset not found")
    if not store.can_access(rec, identity.user, identity.tenant, identity.is_admin):
        raise HTTPException(403, detail="not visible to you")
    return DatasetResponse.from_record(rec)


@router.patch("/{did}", response_model=DatasetResponse)
def patch_dataset(
    did: str,
    body: PatchDatasetRequest,
    identity: Identity = Depends(require_user),
    store: DatasetStore = Depends(_store),
) -> DatasetResponse:
    rec = store.get(did)
    if not rec:
        raise HTTPException(404, detail="dataset not found")
    # Only owner / admin can modify
    if not identity.is_admin and rec.owner != identity.user:
        raise HTTPException(403, detail="only owner can modify")
    changes = {}
    if body.visibility is not None:
        if body.visibility not in _VALID_VIS:
            raise HTTPException(400, detail="invalid visibility")
        changes["visibility"] = body.visibility
    if body.tags is not None:
        changes["tags"] = body.tags
    if body.description is not None:
        changes["description"] = body.description
    rec = store.update(did, **changes)
    return DatasetResponse.from_record(rec)


@router.delete("/{did}", status_code=204)
def delete_dataset(
    did: str,
    identity: Identity = Depends(require_user),
    store: DatasetStore = Depends(_store),
) -> None:
    rec = store.get(did)
    if not rec:
        raise HTTPException(404, detail="dataset not found")
    if not identity.is_admin and rec.owner != identity.user:
        raise HTTPException(403, detail="only owner can delete")
    store.delete(did)


@router.post("/{did}/refresh", response_model=DatasetResponse)
def refresh_dataset(
    did: str,
    identity: Identity = Depends(require_user),
    store: DatasetStore = Depends(_store),
    settings: Settings = Depends(get_settings),
) -> DatasetResponse:
    """Re-scan Lance metadata for an existing dataset."""
    rec = store.get(did)
    if not rec:
        raise HTTPException(404, detail="dataset not found")
    if not identity.is_admin and rec.owner != identity.user:
        raise HTTPException(403, detail="only owner can refresh")
    if rec.type == "lance":
        meta = scan_lance(rec.uri, storage_options=_storage_options(settings))
        if meta.ok:
            rec = store.update(
                did,
                rows=meta.rows,
                size_bytes=meta.size_bytes,
                schema_json=meta.schema_json,
            )
    return DatasetResponse.from_record(rec)
