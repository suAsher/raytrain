"""
Server-wide configuration. All values come from env vars (12-factor) so the
same image runs in dev / staging / prod with no code changes.

The Pydantic ``BaseSettings`` does the parsing + validation; tests inject
custom values by passing kwargs directly.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """raytrain-server runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="RAYTRAIN_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- service ----------
    server_host: str = "0.0.0.0"
    server_port: int = 8080
    log_level: str = "INFO"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # ---------- auth (JWT, M1 self-issued; M5+ OIDC) ----------
    # The HMAC secret is mounted as a K8s Secret; do NOT bake it into images.
    jwt_secret: str = Field(default="dev-only-change-me", min_length=8)
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "raytrain-server"
    jwt_default_ttl_days: int = 365

    # ---------- MinIO (for code zip + artifact lookups) ----------
    minio_endpoint: str = "http://minio.waterpool.svc.cluster.local:9000"
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_secure: bool = False
    minio_region: str = "us-east-1"
    code_bucket: str = "raytrain-code"

    # ---------- Ray long-lived clusters ----------
    # Map of gpu_type -> Ray dashboard / Job Submission API base URL.
    # Example: {"h20": "http://ray-shared-h20-head.raytrain-shared.svc:8265"}
    shared_clusters: dict[str, str] = Field(default_factory=dict)

    # MLflow tracking URI (passed through to training subprocess via env vars)
    mlflow_tracking_uri: str = ""

    # ---------- legacy per-job submission (kept as fallback) ----------
    # If a job comes in with cluster_mode=per_job we fall back to creating a
    # RayJob in this namespace via K8s API. v1 only supports shared mode but
    # we keep the field for forward-compat.
    legacy_namespace: str = "ray-cluster-3"

    # ---------- Workspace (M2) ----------
    workspace_namespace: str = "raytrain-workspaces"
    workspace_image: str = "172.31.9.104:5050/raytrain/raytrain-workspace:cpu-base-v1"
    workspace_storage_class: str = "longhorn"
    workspace_default_pvc_gi: int = 100
    workspace_default_cpu: int = 4
    workspace_default_memory_gi: int = 8
    # Wildcard base domain for IDE URLs (e.g. raytrain.example.com). Empty →
    # IDE URLs are omitted and users reach pods via port-forward / NodePort.
    workspace_base_domain: str = ""

    # ---------- DevSession (M3) ----------
    devsession_namespace: str = "raytrain-dev"
    devsession_image: str = "172.31.9.104:5050/raytrain/raytrain-workspace:gpu-v1"
    devsession_default_cpu: int = 8
    devsession_default_memory_gi: int = 64
    devsession_idle_timeout_s: int = 4 * 3600
    devsession_max_lifetime_s: int = 24 * 3600

    # ---------- container ----------
    # Whether the server is running inside a K8s pod (load_incluster_config)
    # vs locally (load_kube_config). Auto-detected when ``None``.
    in_cluster: bool | None = None

    # ---------- persistence (M4) ----------
    # Empty → in-memory stores (dev / tests). Otherwise:
    #   sqlite:///var/lib/raytrain/state.db
    #   postgresql://user:pass@host:5432/raytrain
    database_url: str = ""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton; never call inside tests directly — use override."""
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: drop the cached Settings so the next call re-reads env."""
    get_settings.cache_clear()
