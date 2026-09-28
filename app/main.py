"""FastAPI application factory wiring the governance layer onto business API.

Governed endpoints:
  * POST /orders            (v1, v2; signed; idempotent; rate limited)
  * GET  /orders/{order_id} (v1, v2; signed; rate limited)
  * GET  /inventory/summary (v2 only -> v1 produces version.unsupported)
  * GET  /_/versions        (unsigned meta endpoint)

Local admin endpoints (not part of the external API):
  * GET/PUT /_/admin/rate-rules  (inspect / hot-swap rate-limit & quota rules)
"""

from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from app.clock import SystemClock
from app.config import GovernanceConfig, build_default_config
from app.dependencies import GovernanceRejection, govern
from app.errors import (
    IDEMPOTENCY_CONFLICT,
    IDEMPOTENCY_MISSING_KEY,
    error_response,
)
from app.idempotency import StoredResponse
from app.models import CreateOrderRequest
from app.ratelimit import RateLimitRule
from app.service import OrderService

logger = logging.getLogger("governance")

IDEMPOTENCY_HEADER = "Idempotency-Key"


def create_app(config: GovernanceConfig | None = None) -> FastAPI:
    config = config or build_default_config(SystemClock())
    service = OrderService(config.orders, config.clock)

    app = FastAPI(title="API Governance Demo", version="2.0")
    app.state.config = config
    app.state.service = service

    @app.exception_handler(GovernanceRejection)
    async def _on_rejection(_request: Request, exc: GovernanceRejection):
        return exc.response

    @app.get("/")
    async def root():
        return {"service": "api-governance-demo",
                "docs": "/docs", "versions": "/_/versions"}

    # ---- unsigned meta --------------------------------------------------
    @app.get("/_/versions")
    async def versions(request: Request):
        cfg = request.app.state.config
        now = cfg.clock.now()
        out = []
        for number, info in sorted(cfg.version_registry.versions.items()):
            out.append({
                "version": number,
                "lifecycle": cfg.version_registry.lifecycle_now(info).value,
                "deprecated_at": info.deprecated_at,
                "sunset_at": info.sunset_at,
                "seconds_until_sunset": (
                    max(0, int(info.sunset_at - now))
                    if info.sunset_at is not None else None
                ),
            })
        return {"default_version": cfg.version_registry.default_version,
                "versions": out}

    # ---- write: create order --------------------------------------------
    @app.post("/orders")
    async def create_order(request: Request):
        gate = await govern(request)
        cfg = request.app.state.config

        key = request.headers.get(IDEMPOTENCY_HEADER)
        if not key or not key.strip():
            return error_response(
                IDEMPOTENCY_MISSING_KEY,
                detail=f"Write requests require the {IDEMPOTENCY_HEADER} header.",
            )
        key = key.strip()

        raw = await request.body()
        content_type = request.headers.get("content-type", "")
        request_hash = cfg.idempotency.hash_payload(raw, content_type)
        preview = raw[:200].decode("utf-8", errors="replace")

        with cfg.idempotency.reserve(key, request_hash, preview) as (kind, obj):
            if kind == "conflict":
                stored = obj
                return error_response(
                    IDEMPOTENCY_CONFLICT,
                    detail=(
                        "Idempotency-Key was previously used with a different "
                        "request payload; the original result is unchanged."
                    ),
                    headers={"Idempotency-Conflict": "true"},
                    idempotency_key=key,
                    original_status=stored.status_code if stored else None,
                )

            if kind == "replay":
                stored: StoredResponse = obj
                headers = dict(stored.headers)
                headers["Idempotency-Replayed"] = "true"
                logger.info(
                    "idempotent replay key=%s status=%s", key, stored.status_code
                )
                return Response(
                    content=stored.body,
                    status_code=stored.status_code,
                    media_type="application/json",
                    headers=headers,
                )

            # kind == "execute": the single authoritative execution.
            try:
                payload = CreateOrderRequest.model_validate_json(raw)
            except ValidationError as exc:
                cfg.idempotency.fail(obj)
                return JSONResponse(
                    status_code=422,
                    content={"error": {"code": "request.invalid",
                                       "message": "Payload failed validation.",
                                       "detail": exc.errors(include_url=False)}},
                )

            record = service.create_order(payload)
            body = service.shape(record, gate.version.version)
            status_code = 201
            headers = {
                "Idempotency-Saved": "true",
                "X-Matched-Signing-Key": gate.matched_key_id,
                **gate.deprecation_headers,
                **gate.rate.headers,
            }
            raw_out = json.dumps(body, separators=(",", ":")).encode()
            cfg.idempotency.complete(
                obj,
                StoredResponse(status_code, raw_out, dict(headers),
                               request_hash, preview),
            )
            logger.info(
                "order accepted key=%s caller=%s version=v%s order_id=%s status=%s",
                key, gate.caller, gate.version.version, record.id, status_code,
            )
            return Response(content=raw_out, status_code=status_code,
                            media_type="application/json", headers=headers)

    # ---- read: fetch order ----------------------------------------------
    @app.get("/orders/{order_id}")
    async def get_order(order_id: str, request: Request):
        gate = await govern(request)
        record = service.get_order(order_id)
        if record is None:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "order.not_found",
                                   "message": f"No order {order_id}."}},
            )
        body = service.shape(record, gate.version.version)
        return JSONResponse(
            content=body,
            headers={**gate.deprecation_headers, **gate.rate.headers},
        )

    # ---- v2-only endpoint demonstrates version.unsupported --------------
    @app.get("/inventory/summary")
    async def inventory_summary(request: Request):
        gate = await govern(request, endpoint_versions=(2,))
        return JSONResponse(
            content={"version": gate.version.version, "items": 0,
                     "status": "ok"},
            headers={**gate.deprecation_headers, **gate.rate.headers},
        )

    # ---- local admin: rate rules ----------------------------------------
    @app.get("/_/admin/rate-rules")
    async def get_rules(request: Request):
        rl = request.app.state.config.rate_limiter
        rules = {}
        # snapshot via the public rule_for for known callers plus a direct
        # mapping read is avoided; admin keeps the config-side dict:
        with rl._rules_lock:  # local admin tool only
            for caller, rule in rl._rules.items():
                rules[caller] = {
                    "rate_per_second": rule.rate_per_second,
                    "burst": rule.burst,
                    "daily_quota": rule.daily_quota,
                }
        return {"rules": rules}

    @app.put("/_/admin/rate-rules/{caller_id}")
    async def put_rule(caller_id: str, request: Request):
        data = await request.json()
        rule = RateLimitRule(
            rate_per_second=float(data["rate_per_second"]),
            burst=int(data["burst"]),
            daily_quota=int(data["daily_quota"]),
        )
        request.app.state.config.rate_limiter.set_rule(caller_id, rule)
        logger.info("rate rule hot-swapped caller=%s rule=%s", caller_id, rule)
        return {"caller_id": caller_id, "rule": data}

    return app


app = create_app()
