"""FastAPI dependency that enforces the governance pipeline.

Order of checks on every governed request::

    1. version negotiation     (unknown / unsupported / retired distinguished)
    2. signature verification  (missing -> unknown caller -> expired ->
                                invalid -> replay distinguished)
    3. per-caller rate limit   (rate vs. quota distinguished)

Authentication is deliberately performed **before** rate limiting: an
unauthenticated request cannot be trusted to name the caller, so otherwise
spoofed ``X-Caller-Id`` headers could never burn a victim's quota.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.errors import error_response
from app.ratelimit import RateLimitDecision
from app.signature import verify_signature
from app.versioning import (
    VersionInfo,
    VersionNegotiationError,
    deprecation_headers,
    negotiate_version,
)


class GovernanceRejection(Exception):
    """Carries a ready-to-return JSON rejection response."""

    def __init__(self, response) -> None:
        super().__init__("governance rejection")
        self.response = response


@dataclass
class Gate:
    version: VersionInfo
    caller: str
    matched_key_id: str
    deprecation_headers: dict[str, str]
    rate: RateLimitDecision


def get_config(request: Request):
    return request.app.state.config


async def govern(
    request: Request,
    *,
    endpoint_versions: tuple[int, ...] | None = None,
    endpoint_min_version: int = 1,
) -> Gate:
    config = get_config(request)

    # 1 ---- version negotiation -------------------------------------------
    try:
        version_info, _used_default = negotiate_version(
            request.headers,
            config.version_registry,
            endpoint_min_version=endpoint_min_version,
            endpoint_versions=endpoint_versions,
        )
    except VersionNegotiationError as exc:
        raise GovernanceRejection(error_response(
            exc.error, detail=exc.detail,
            requested=exc.requested, supported=exc.supported,
        ))

    dep_headers = (
        deprecation_headers(version_info, config.clock)
        if config.version_registry.lifecycle_now(version_info).value == "deprecated"
        else {}
    )

    # 2 ---- signature / timestamp / replay --------------------------------
    raw_body = await request.body()
    decision = verify_signature(
        headers=request.headers,
        raw_body=raw_body,
        method=request.method,
        path=request.url.path,
        key_ring=config.key_ring,
        clock=config.clock,
        replay_cache=config.replay_cache,
        skew_seconds=config.skew_seconds,
    )
    if not decision.accepted:
        raise GovernanceRejection(error_response(
            decision.error, detail=decision.detail, **decision.context
        ))

    # 3 ---- rate limit / quota (only for an authenticated caller) ---------
    rate = config.rate_limiter.check(decision.context["caller_id"])
    if not rate.accepted:
        raise GovernanceRejection(error_response(
            rate.error, detail=rate.detail,
            caller_id=decision.context["caller_id"],
            headers=rate.headers,
        ))

    return Gate(
        version=version_info,
        caller=decision.context["caller_id"],
        matched_key_id=decision.matched_key_id,
        deprecation_headers=dep_headers,
        rate=rate,
    )
