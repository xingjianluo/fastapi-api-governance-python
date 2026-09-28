"""Error code registry and JSON error responses.

Every rejection carries a stable machine-readable ``code`` so callers can
distinguish *why* a request was refused (unknown version vs. retired version,
bad signature vs. expired timestamp vs. replay, etc.).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi.responses import JSONResponse

logger = logging.getLogger("governance")


@dataclass(frozen=True)
class ErrorCode:
    code: str
    status_code: int
    message: str


# ---- version negotiation ------------------------------------------------
VERSION_UNKNOWN = ErrorCode(
    "version.unknown", 400,
    "The requested API version is not a recognised version identifier.",
)
VERSION_UNSUPPORTED = ErrorCode(
    "version.unsupported", 400,
    "The API version is recognised but not supported by this endpoint.",
)
VERSION_RETIRED = ErrorCode(
    "version.retired", 410,
    "The API version has passed its sunset date and is permanently retired.",
)

# ---- signature / replay -------------------------------------------------
SIG_MISSING_CREDENTIALS = ErrorCode(
    "signature.missing_credentials", 401,
    "Required signature headers (X-Caller-Id/X-Timestamp/X-Signature) are missing.",
)
SIG_UNKNOWN_CALLER = ErrorCode(
    "signature.unknown_caller", 401,
    "The caller id is not registered.",
)
SIG_EXPIRED = ErrorCode(
    "signature.timestamp_expired", 401,
    "The request timestamp is outside the accepted time window.",
)
SIG_INVALID = ErrorCode(
    "signature.invalid", 401,
    "The HMAC signature does not match the canonical request string.",
)
SIG_REPLAY = ErrorCode(
    "signature.replay_detected", 401,
    "A request with the same nonce has already been processed.",
)

# ---- idempotency --------------------------------------------------------
IDEMPOTENCY_MISSING_KEY = ErrorCode(
    "idempotency.missing_key", 400,
    "Write requests must carry an Idempotency-Key header.",
)
IDEMPOTENCY_CONFLICT = ErrorCode(
    "idempotency.key_conflict", 409,
    "The idempotency key was already used with a different request payload.",
)

# ---- rate limiting / quota ---------------------------------------------
RATE_LIMITED = ErrorCode(
    "rate_limit.exceeded", 429,
    "Per-second request rate exceeded; retry after the indicated delay.",
)
QUOTA_EXHAUSTED = ErrorCode(
    "quota.exhausted", 429,
    "Daily request quota exhausted; retry after the quota reset time.",
)

# ---- generic ------------------------------------------------------------
INTERNAL_ERROR = ErrorCode(
    "internal.error", 500,
    "An unexpected internal error occurred.",
)


def error_response(
    error: ErrorCode,
    *,
    detail: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: object,
) -> JSONResponse:
    """Build a JSON response for a governance rejection and log the decision."""
    body: dict[str, object] = {
        "error": {
            "code": error.code,
            "message": error.message,
        }
    }
    if detail is not None:
        body["error"]["detail"] = detail
    if extra:
        body["error"]["context"] = extra
    logger.info(
        "governance rejection code=%s status=%s detail=%s context=%s",
        error.code, error.status_code, detail, extra,
    )
    return JSONResponse(status_code=error.status_code, content=body, headers=headers)
