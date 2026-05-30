"""
Machine-readable error codes + human messages.

Per spec: "所有错误信息必须返回机器可读 code 和人类可读 message". Every
validation / submission failure raises a PlatformError carrying a stable
``code`` (for the frontend to switch on) and a friendly ``message``.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlatformError(Exception):
    code: str
    message: str
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


# --- error code constants (stable, frontend switches on these) ---
ERR_RESERVED_LABEL = "RESERVED_LABEL"
ERR_PVC_RWO_MULTINODE = "PVC_RWO_MULTINODE"
ERR_CHECKPOINT_REQUIRED_MULTINODE = "CHECKPOINT_REQUIRED_MULTINODE"
ERR_QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
ERR_INVALID_RESOURCE = "INVALID_RESOURCE"
ERR_IMAGE_NOT_ALLOWED = "IMAGE_NOT_ALLOWED"
ERR_PROJECT_FORBIDDEN = "PROJECT_FORBIDDEN"
ERR_QUEUE_FORBIDDEN = "QUEUE_FORBIDDEN"
ERR_DATASET_FORBIDDEN = "DATASET_FORBIDDEN"
ERR_MISSING_FIELD = "MISSING_FIELD"
