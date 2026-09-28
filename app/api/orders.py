"""示例业务接口：下单与查询。所有接口统一经过治理管线。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request, Response

from ..governance.idempotency import IdempotentResult
from ..schemas import CreateOrderRequest, serialize_order
from ..service import OrderService
from .dependencies import RequestGovernance, govern_request, replay_response

router = APIRouter()


@router.post("/orders", status_code=201)
async def create_order(
    request: Request,
    response: Response,
    payload: CreateOrderRequest,
    g: RequestGovernance = Depends(govern_request),
) -> dict:
    gov = request.app.state.governance

    def write_headers(extra: dict | None = None) -> None:
        if g.rate_limit_headers:
            response.headers.update(g.rate_limit_headers)
        response.headers.update(gov.versions.deprecation_headers(g.version))
        response.headers["API-Version"] = g.version.version
        response.headers["API-Version-Status"] = g.version.status
        if extra:
            response.headers.update(extra)

    # 幂等重放：返回首次执行保存的同一份结果
    if g.idempotency_outcome == "replay" and g.replay_result is not None:
        status, body, extra = replay_response(g.replay_result)
        response.status_code = status
        write_headers(extra)
        return body

    service: OrderService = request.app.state.order_service
    order = service.create(payload)
    body = serialize_order(order, g.version.version)

    # 只有真正 acquired 的请求才登记首次结果
    if g.idempotency_outcome == "acquired" and g.idempotency_key:
        gov.idempotency.finish(
            g.idempotency_key,
            IdempotentResult(201, json.dumps(body, ensure_ascii=False).encode("utf-8")),
        )

    write_headers()
    return body


@router.get("/orders/{order_id}")
async def get_order(
    order_id: str,
    request: Request,
    response: Response,
    g: RequestGovernance = Depends(govern_request),
) -> dict:
    service: OrderService = request.app.state.order_service
    order = service.get(order_id)
    body = serialize_order(order, g.version.version)
    gov = request.app.state.governance
    if g.rate_limit_headers:
        response.headers.update(g.rate_limit_headers)
    response.headers.update(gov.versions.deprecation_headers(g.version))
    response.headers["API-Version"] = g.version.version
    response.headers["API-Version-Status"] = g.version.status
    return body


@router.post("/orders/batch-confirm")
async def batch_confirm(
    request: Request,
    response: Response,
    g: RequestGovernance = Depends(govern_request),
) -> dict:
    """仅 v2 提供的能力：v1 下应被 version_unsupported_combo 拒绝。"""
    gov = request.app.state.governance
    if g.rate_limit_headers:
        response.headers.update(g.rate_limit_headers)
    response.headers.update(gov.versions.deprecation_headers(g.version))
    response.headers["API-Version"] = g.version.version
    response.headers["API-Version-Status"] = g.version.status
    return {"confirmed": True, "version": g.version.version}
