"""
ArtifactStore — list a job's REAL output artifacts from object storage (Req:
no synthesized data). A job's outputs live under its checkpoint URI
(``s3://bucket/prefix``); we list objects there and classify each by name into
checkpoint / model / log / eval.

Injectable (Protocol + MinIO impl + Fake) so tests need no real bucket. Listing
failures raise ArtifactsUnavailable → the API turns it into a FriendlyError; we
never fabricate artifacts. When the job has no checkpoint URI (or it isn't an
``s3://`` URI), we return an empty list flagged 'unavailable'.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

log = logging.getLogger(__name__)


class ArtifactsUnavailable(Exception):
    """Raised when object storage can't be listed (network / auth / config)."""


@dataclass
class Artifact:
    name: str
    kind: str          # checkpoint | model | log | eval
    size: str          # human-readable
    path: str          # s3://bucket/key
    created_at: str     # ISO

    def to_dict(self) -> dict:
        return {
            "name": self.name, "kind": self.kind, "size": self.size,
            "path": self.path, "created_at": self.created_at,
        }


@dataclass
class ArtifactPage:
    artifacts: list[Artifact] = field(default_factory=list)
    source: str = "minio"      # or "unavailable" when no checkpoint uri

    def to_dict(self) -> dict:
        return {"artifacts": [a.to_dict() for a in self.artifacts], "source": self.source}


def _human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.1f} {units[i]}"


def classify(name: str) -> str:
    """Classify an object by its name/extension into an artifact kind."""
    low = name.lower()
    if low.endswith((".log", ".txt", ".out")) or "log" in low:
        return "log"
    if low.endswith(".json") and ("eval" in low or "result" in low or "metric" in low):
        return "eval"
    if "final" in low or low.endswith((".onnx", ".safetensors")) or "model" in low:
        return "model"
    if low.endswith((".pth", ".pt", ".ckpt", ".bin")) or "checkpoint" in low or "epoch" in low:
        return "checkpoint"
    return "checkpoint"


def split_s3_uri(uri: str) -> tuple[str, str] | None:
    """Parse ``s3://bucket/prefix`` → (bucket, prefix). Returns None for non-s3."""
    if not uri:
        return None
    u = urlparse(uri)
    if u.scheme not in ("s3", "minio"):
        return None
    bucket = u.netloc
    prefix = u.path.lstrip("/")
    if not bucket:
        return None
    return bucket, prefix


class ArtifactStore(Protocol):
    def list_for_uri(self, checkpoint_uri: str) -> ArtifactPage: ...


class MinioArtifactStore:
    """Lists objects under a job's checkpoint prefix from MinIO/S3."""

    def __init__(self, client, max_items: int = 200):
        self._client = client
        self._max = max_items

    def list_for_uri(self, checkpoint_uri: str) -> ArtifactPage:
        parsed = split_s3_uri(checkpoint_uri)
        if parsed is None:
            return ArtifactPage(artifacts=[], source="unavailable")
        bucket, prefix = parsed
        try:
            objs = self._client.list_objects(bucket, prefix=prefix, recursive=True)
            arts: list[Artifact] = []
            for o in objs:
                key = getattr(o, "object_name", "") or ""
                if key.endswith("/"):
                    continue  # skip "directories"
                name = key.rsplit("/", 1)[-1]
                size = int(getattr(o, "size", 0) or 0)
                lm = getattr(o, "last_modified", None)
                created = lm.strftime("%Y-%m-%dT%H:%M:%SZ") if lm else ""
                arts.append(Artifact(
                    name=name, kind=classify(name), size=_human_size(size),
                    path=f"s3://{bucket}/{key}", created_at=created,
                ))
                if len(arts) >= self._max:
                    break
            return ArtifactPage(artifacts=arts, source="minio")
        except Exception as exc:  # noqa: BLE001
            raise ArtifactsUnavailable(f"list artifacts failed: {exc!r}") from exc


class FakeArtifactStore:
    """Test double: serves preset artifacts, or raises if fail=True. With no
    preset, returns an empty 'unavailable' page (mirrors no-checkpoint case)."""

    def __init__(self, artifacts: list[Artifact] | None = None, fail: bool = False):
        self._artifacts = artifacts
        self._fail = fail

    def list_for_uri(self, checkpoint_uri: str) -> ArtifactPage:
        if self._fail:
            raise ArtifactsUnavailable("fake artifact failure")
        if split_s3_uri(checkpoint_uri) is None:
            return ArtifactPage(artifacts=[], source="unavailable")
        return ArtifactPage(artifacts=list(self._artifacts or []), source="minio")


_artifact_store: ArtifactStore | None = None


def get_artifact_store() -> ArtifactStore | None:
    """Return the configured ArtifactStore, or None when MinIO isn't configured."""
    global _artifact_store
    if _artifact_store is None:
        from .settings import get_settings

        s = get_settings()
        if not s.minio_endpoint or not s.minio_access_key:
            return None
        from .minio_client import make_minio_client

        _artifact_store = MinioArtifactStore(make_minio_client(s))
    return _artifact_store


def set_artifact_store(store: ArtifactStore | None) -> None:
    global _artifact_store
    _artifact_store = store
