"""统一的治理层错误类型。

每一种拒绝都对应一个稳定、可区分的 ``code``，方便客户端做分支处理，
也方便日志里解释"为什么拒绝"。
"""

from __future__ import annotations


class GovernanceError(Exception):
    """所有治理层拒绝的基类。

    Attributes:
        code: 稳定的机器可读错误码。
        status_code: 对应的 HTTP 状态码。
        details: 附加上下文（判定依据），会进入响应体与日志。
    """

    code: str = "governance_error"
    status_code: int = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        self.message = message
        self.details = details or {}
        super().__init__(message)

    def to_body(self) -> dict:
        # response_headers 仅用于在异常处理器里传递响应头，不回显到 body
        visible = {k: v for k, v in self.details.items() if k != "response_headers"}
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": visible,
            }
        }


# ---------------------------------------------------------------------------
# 版本协商
# ---------------------------------------------------------------------------
class VersionNegotiationError(GovernanceError):
    """版本协商类拒绝的基类。"""

    code = "version_error"
    status_code = 400


class VersionMissing(VersionNegotiationError):
    """客户端没有指定 API 版本，而服务端不允许隐式选择版本。"""

    code = "version_missing"
    status_code = 400


class VersionUnknown(VersionNegotiationError):
    """请求的版本号在服务端注册表中完全不存在。"""

    code = "version_unknown"
    status_code = 404


class VersionUnsupportedCombo(VersionNegotiationError):
    """版本存在，但该 (接口, 方法, 版本) 组合不受支持。

    例如某接口是在更高版本才引入的，在旧版本上不存在。
    """

    code = "version_unsupported_combo"
    status_code = 400


class VersionSunset(VersionNegotiationError):
    """版本曾提供过，但已经过了下线日期，彻底不可用。"""

    code = "version_sunset"
    status_code = 410


# ---------------------------------------------------------------------------
# 签名 / 来源校验
# ---------------------------------------------------------------------------
class SignatureError(GovernanceError):
    """来源签名校验类拒绝的基类。"""

    code = "signature_error"
    status_code = 401


class SignatureCredentialsMissing(SignatureError):
    """缺少签名相关的必要请求头。"""

    code = "signature_credentials_missing"
    status_code = 401


class SignatureKeyUnknown(SignatureError):
    """签名头里的 key id 在服务端找不到。"""

    code = "signature_key_unknown"
    status_code = 401


class SignatureKeyRetired(SignatureError):
    """密钥已过轮换宽限期，被正式停用。"""

    code = "signature_key_retired"
    status_code = 401


class SignatureTimestampExpired(SignatureError):
    """时间戳缺失/无法解析，或超出允许的时间窗（防重放）。"""

    code = "signature_timestamp_expired"
    status_code = 401


class SignatureMismatch(SignatureError):
    """时间窗有效、密钥有效，但摘要对不上（签名错误或内容被篡改）。"""

    code = "signature_mismatch"
    status_code = 401


class SignatureReplay(SignatureError):
    """同一 nonce 在时间窗内被重复使用，判定为重放。"""

    code = "signature_replay"
    status_code = 409


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------
class IdempotencyError(GovernanceError):
    """幂等处理类拒绝的基类。"""

    code = "idempotency_error"


class IdempotencyConflict(IdempotencyError):
    """同一个幂等键被复用于指纹不同的请求（内容冲突），拒绝覆盖。"""

    code = "idempotency_conflict"
    status_code = 422


class IdempotencyInProgress(IdempotencyError):
    """同一幂等键的前一次请求仍在执行中（并发重试），请稍后重试。"""

    code = "idempotency_in_progress"
    status_code = 409


# ---------------------------------------------------------------------------
# 限流 / 配额
# ---------------------------------------------------------------------------
class RateLimitError(GovernanceError):
    """限流配额类拒绝的基类，带 Retry-After 语义。"""

    code = "rate_limited"
    status_code = 429


class BurstRateLimited(RateLimitError):
    """短周期突发限流被拒（令牌/秒级窗口）。"""

    code = "burst_rate_limited"
    status_code = 429


class QuotaExhausted(RateLimitError):
    """长周期配额被耗尽。"""

    code = "quota_exhausted"
    status_code = 429
