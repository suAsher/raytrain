"""
FriendlyError — 面向用户的结构化错误契约（横切，贯穿全平台）。

为什么需要它
------------
集群接入（K8s / Kueue / Loki / Prometheus / Ray）随时可能不可达。我们要求每个面向用户的
失败路径都返回**可读、可本地化**的错误，而不是裸异常 repr 或 500。FriendlyError 把
(status, code, message, hint) 统一序列化为：

    { "error": { "code": "WORKSPACE_TERMINATING", "message": "...", "hint": "..." } }

- ``code``    机器可读，前端据此做 i18n 文案映射（见 console 的 errMsg）。
- ``message`` 默认中文人话，前端无对应 code 文案时回退展示它。
- ``hint``    可选的下一步建议（如"请稍后重试"）。

API 层在任何用户可见失败处抛 ``FriendlyError(...)``；``install_error_handlers(app)`` 注册
全局处理器统一序列化。沿用既有风格：不引第三方错误库，stdlib + FastAPI 即可。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


class FriendlyError(Exception):
    """A user-facing error with a stable code + readable message.

    Raise it anywhere in the request path; the global handler turns it into a
    structured JSON body the console can localize.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        hint: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.hint = hint

    def to_payload(self) -> dict[str, Any]:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            err["hint"] = self.hint
        return {"error": err}


# Common, reusable codes (extend freely; keep stable for i18n mapping).
class Codes:
    # generic
    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    CONFLICT = "CONFLICT"
    INTERNAL = "INTERNAL"
    # cluster connectivity
    NO_CLUSTER = "NO_CLUSTER"               # gpu_type has no shared cluster
    KUEUE_UNAVAILABLE = "KUEUE_UNAVAILABLE"
    LOKI_UNAVAILABLE = "LOKI_UNAVAILABLE"
    PROM_UNAVAILABLE = "PROM_UNAVAILABLE"
    ARTIFACTS_UNAVAILABLE = "ARTIFACTS_UNAVAILABLE"
    SUBMIT_ERROR = "SUBMIT_ERROR"
    # workspace lifecycle
    WORKSPACE_TERMINATING = "WORKSPACE_TERMINATING"
    INVALID_IMAGE = "INVALID_IMAGE"
    # access / authorization
    DOMAIN_NOT_CONFIGURED = "DOMAIN_NOT_CONFIGURED"
    QUEUE_NOT_FOUND = "QUEUE_NOT_FOUND"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    PROJECT_FORBIDDEN = "PROJECT_FORBIDDEN"
    QUEUE_FORBIDDEN = "QUEUE_FORBIDDEN"
    IMAGE_FORBIDDEN = "IMAGE_FORBIDDEN"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"


def install_error_handlers(app: FastAPI) -> None:
    """Register the global FriendlyError handler on the FastAPI app."""

    @app.exception_handler(FriendlyError)
    async def _handle_friendly(_: Request, exc: FriendlyError) -> JSONResponse:
        if exc.status_code >= 500:
            log.error("friendly_error code=%s msg=%s", exc.code, exc.message)
        else:
            log.info("friendly_error code=%s status=%s", exc.code, exc.status_code)
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())
