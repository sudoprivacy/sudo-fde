"""错误码（规格 §3.8）。"""

from __future__ import annotations

from typing import Any

_STATUS = {
    "UNAUTHENTICATED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 403,              # 不区分"不存在"与"无权"（§3.1）
    "INPUT_SENSITIVE": 422,
    "SCHEMA_VIOLATION": 422,
    "CATALOG_INVALID": 422,
    "CONTEXT_TOO_LARGE": 413,
    "IDEMPOTENCY_CONFLICT": 409,
    "IDEMPOTENCY_IN_PROGRESS": 409,
    "STATE_VERSION_CONFLICT": 409,
    "MODEL_UNAVAILABLE": 503,
}


class ServiceError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = "FORBIDDEN" if code == "NOT_FOUND" else code
        self.status = _STATUS[code]
        self.message = message
        self.details = details

    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}
