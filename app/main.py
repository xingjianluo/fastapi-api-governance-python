"""FastAPI 应用装配：把治理组件、统一错误响应与业务路由接起来。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api.orders import router as orders_router
from .config import Governance, default_governance
from .governance.errors import GovernanceError
from .service import OrderNotFound, OrderService

log = logging.getLogger("governance.request")

# (path 前缀, 允许的方法集合, 允许的版本集合)
_ROUTE_VERSION_TABLE: list[tuple[str, set[str], set[str]]] = [
    ("/api/orders/batch-confirm", {"POST"}, {"v2"}),
    ("/api/orders", {"GET", "POST"}, {"v1", "v2"}),
]


def route_version_resolver(path: str, method: str) -> set[str] | None:
    """返回某 (路径, 方法) 支持的版本集合；未登记返回 None（不做接口级限制）。"""
    for prefix, methods, versions in _ROUTE_VERSION_TABLE:
        if path == prefix or path.startswith(prefix + "/"):
            return versions if method.upper() in methods else set()
    return None


def _error_headers(exc: GovernanceError) -> dict:
    return dict(exc.details.get("response_headers", {}))


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(GovernanceError)
    async def on_governance_error(request: Request, exc: GovernanceError) -> JSONResponse:
        log.warning("governance rejection code=%s path=%s details=%s", exc.code, request.url.path, exc.details)
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_body(),
            headers=_error_headers(exc),
        )

    @app.exception_handler(OrderNotFound)
    async def on_order_not_found(request: Request, exc: OrderNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": {"code": "order_not_found", "message": str(exc)}})

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "request_validation_failed",
                    "message": "请求体校验失败",
                    "details": {"errors": exc.errors()},
                }
            },
        )


def create_app(governance: Governance | None = None) -> FastAPI:
    app = FastAPI(
        title="API Governance Bootstrap",
        version="1.0.0",
        description="版本协商、来源签名/幂等、按调用方限流配额的统一治理层",
    )
    app.state.governance = governance or default_governance()
    app.state.order_service = OrderService()
    app.state.route_version_resolver = route_version_resolver

    register_exception_handlers(app)

    @app.get("/health", tags=["meta"])
    async def health() -> dict:
        return {"status": "ok"}

    app.include_router(orders_router, prefix="/api")
    return app


app = create_app()
