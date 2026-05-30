"""
Read Lance dataset metadata (schema / row count / size / versions) so the
Dataset registry can auto-fill those fields on registration.

Lazily imports `lance` so the server boots even if the lib isn't present
(tests inject a fake). All failures degrade gracefully: registration still
succeeds, metadata fields just stay empty.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class LanceMetadata:
    schema_json: dict = field(default_factory=dict)
    rows: int = 0
    size_bytes: int = 0
    version: str = "latest"
    ok: bool = False
    error: str = ""


def scan_lance(uri: str, storage_options: dict | None = None) -> LanceMetadata:
    """Open a Lance dataset and extract metadata. Never raises."""
    try:
        import lance  # type: ignore
    except ImportError:
        return LanceMetadata(ok=False, error="pylance not installed in server")

    try:
        ds = lance.dataset(uri, storage_options=storage_options or {})
        schema = {}
        try:
            arrow_schema = ds.schema
            schema = {f.name: str(f.type) for f in arrow_schema}
        except Exception:  # noqa: BLE001
            pass
        rows = 0
        try:
            rows = int(ds.count_rows())
        except Exception:  # noqa: BLE001
            pass
        version = "latest"
        try:
            version = str(ds.version)
        except Exception:  # noqa: BLE001
            pass
        return LanceMetadata(
            schema_json=schema,
            rows=rows,
            size_bytes=0,  # lance doesn't expose this cheaply; leave 0
            version=version,
            ok=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("scan_lance failed for %s: %r", uri, exc)
        return LanceMetadata(ok=False, error=repr(exc))
