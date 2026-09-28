"""FastAPI 治理依赖：把版本协商、来源签名、限流配额、幂等串成统一管线。

对写入请求（POST/PUT/PATCH/DELETE）的处理顺序：

    1. 限流/配额（最先，拒绝最便宜，且被拒不影响任何业务状态）
    2. 版本协商（缺版本/未知版本/不支持组合/已下线 分别给出不同错误码）
    3. 来源签名 + 时间窗 + 重放（含密钥轮换宽限判定）
    4. 幂等占用（并发重试只有一个真正执行业务）

只读请求只做 1、2 两步。
任何阶段抛出的治理错误都会把当前已知的解释性响应头挂到 details 上，
由统一异常处理器写出，保证成功/失败响应行为一致、可解释。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from fastapi import Header, Request

from ..config import Governance
from ..governance.errors import GovernanceError
from ..governance.idempotency import (
    ACQUIRED,
    IN_PROGRESS,
    REPLAY,
    IdempotentResult,
)
from ..governance.versioning import VersionSpec

log = logging.getLogger("governance.request")

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass
class RequestGovernance:
    version: VersionSpec
    caller: str
    idempotency_key: str | None = None
    idempotency_outcome: str | None = None
    replay_result: IdempotentResult | None = None
    rate_limit_headers: dict | None = None


def _fingerprint(version: str, body: bytes, content_type: str) -> str:
    """幂等指纹：版本 + 内容类型 + 原始 body 的摘要。"""
    h = hashlib.sha256()
    h.update(version.encode())
    h.update(b"|")
    h.update((content_type or "").encode())
    h.update(b"|")
    h.update(body)
    return h.hexdigest()


def _log_decision(stage: str, accepted: bool, **basis) -> None:
    log.info(
        "governance stage=%s status=%s basis=%s",
        stage,
        "ACCEPTED" if accepted else "REJECTED",
        json.dumps(basis, ensure_ascii=False, default=str),
    )


async def govern_request(
    request: Request,
    x_api_version: str | None = Header(default=None, alias="X-API-Version"),
    x_caller_id: str | None = Header(default="anonymous", alias="X-Caller-Id"),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    x_key_id: str | None = Header(default=None, alias="X-Key-Id"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_nonce: str | None = Header(default=None, alias="X-Nonce"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> RequestGovernance:
    gov: Governance = request.app.state.governance
    method = request.method.upper()
    path = request.url.path
    caller = x_caller_id or "anonymous"
    body = await request.body()

    context_headers: dict = {}
    version: VersionSpec | None = None

    try:
        # 1) 限流 / 配额（所有请求，先做）。被限时 enforce 抛出，
        # 但决策里的 Retry-After / limit / reset 头仍需写进错误响应。
        decision = gov.rate_limiter.check(caller)
        if not decision.allowed:
            from ..governance.errors import BurstRateLimited, QuotaExhausted

            exc_cls = QuotaExhausted if decision.scope == "quota" else BurstRateLimited
            raise exc_cls(
                f"调用方 {caller} 触发{('配额' if decision.scope == 'quota' else '突发')}限流",
                details={
                    "caller": caller,
                    "scope": decision.scope,
                    "limit": decision.limit,
                    "retry_after_seconds": decision.retry_after_seconds,
                    "reset_at": decision.reset_at,
                    "response_headers": decision.headers(),
                },
            )
        context_headers.update(decision.headers())
        _log_decision(
            "ratelimit", True, caller=caller, scope=decision.scope, remaining=decision.remaining
        )

        # 2) 版本协商（由应用层注册的解析器声明各接口支持的版本）
        resolver = getattr(request.app.state, "route_version_resolver", None)
        route_versions = resolver(path, method) if resolver else None
        version = gov.versions.negotiate(x_api_version, route_versions=route_versions)
        context_headers["API-Version"] = version.version
        context_headers["API-Version-Status"] = version.status
        context_headers.update(gov.versions.deprecation_headers(version))
        _log_decision(
            "version",
            True,
            requested=x_api_version,
            status=version.status,
            route_versions=sorted(route_versions) if route_versions else None,
        )

        result = RequestGovernance(
            version=version,
            caller=caller,
            idempotency_key=x_idempotency_key,
            rate_limit_headers=decision.headers(),
        )

        if method not in WRITE_METHODS:
            return result

        # 3) 来源签名（时间窗、轮换宽限、重放在 verify 内区分）
        key = gov.signatures.verify(
            method=method,
            path=path,
            body=body,
            timestamp=x_timestamp,
            nonce=x_nonce,
            kid=x_key_id,
            signature=x_signature,
        )
        _log_decision("signature", True, kid=key.kid, key_state=key.state, method=method)

        # 4) 幂等（写入请求允许不带幂等键：不做去重但不会报错）
        if not x_idempotency_key:
            return result

        fp = _fingerprint(version.version, body, request.headers.get("content-type", ""))
        outcome = gov.idempotency.begin(x_idempotency_key, fp)

        if outcome == IN_PROGRESS:
            waited = gov.idempotency.wait_for_result(x_idempotency_key, timeout=2.0)
            if waited is not None:
                result.idempotency_outcome = REPLAY
                result.replay_result = waited
                _log_decision("idempotency", True, key=x_idempotency_key, outcome="waited-replay")
                return result
            # 等不到首个请求落结果 -> 明确拒绝为 in_progress（稍后重试）
            gov.idempotency.claim_or_raise(x_idempotency_key, fp)  # raises

        if outcome == REPLAY:
            result.idempotency_outcome = REPLAY
            result.replay_result = gov.idempotency.get(x_idempotency_key)
            _log_decision("idempotency", True, key=x_idempotency_key, outcome="replay")
            return result

        if outcome == "conflict":
            # 不同内容复用了同一幂等键 -> 明确报错，绝不覆盖
            gov.idempotency.claim_or_raise(x_idempotency_key, fp)  # raises

        result.idempotency_outcome = ACQUIRED
        _log_decision(
            "idempotency", True, key=x_idempotency_key, outcome="acquired", fingerprint=fp[:12]
        )
        return result

    except GovernanceError as exc:
        # 把当前已经确定的解释性响应头带给统一异常处理器
        exc.details.setdefault("response_headers", {}).update(context_headers)
        _log_decision(
            "governance",
            False,
            code=exc.code,
            caller=caller,
            requested_version=x_api_version,
            resolved_status=version.status if version else None,
            detail=exc.details,
        )
        raise


def replay_response(result: IdempotentResult) -> tuple[int, dict, dict]:
    """把首次执行保存的结果原样回放，保证重试结果一致。"""
    payload = json.loads(result.body.decode("utf-8")) if result.body else {}
    return result.status_code, payload, {"Idempotent-Replay": "true"}
