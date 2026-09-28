"""业务请求/响应模型与版本化序列化。

关键约定：内部只有一个"规范模型" :class:`Order`，各版本的响应都由它
**显式**映射而来。新版本只能做"加法"（新增字段），不允许悄悄删掉旧字段
或改变同名字段的含义。``FIELD_PROVENANCE`` 记录每个版本每个字段来自规范
模型的哪个属性，测试会校验没有字段被静默丢弃。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass

from pydantic import BaseModel, Field


class CreateOrderRequest(BaseModel):
    item_id: str = Field(min_length=1)
    qty: int = Field(gt=0, le=10_000)
    note: str | None = Field(default=None, max_length=200)


@dataclass(frozen=True)
class Order:
    """规范业务模型（版本无关）。"""

    order_id: str
    item_id: str
    qty: int
    unit_price_cents: int
    amount_cents: int
    currency: str
    note: str | None
    created_at: str  # ISO-8601 UTC


def serialize_order_v1(order: Order) -> dict:
    """v1 契约：字段全部显式取自规范模型，含义与规范模型一致。"""
    return {
        "order_id": order.order_id,
        "item_id": order.item_id,
        "qty": order.qty,
        "unit_price_cents": order.unit_price_cents,
        "amount_cents": order.amount_cents,
        "currency": order.currency,
        "note": order.note,
        "created_at": order.created_at,
    }


def serialize_order_v2(order: Order) -> dict:
    """v2 契约：与 v1 共享的字段逐字保留，仅做加法（新增明细与幂等回显）。"""
    base = serialize_order_v1(order)
    base["line"] = {
        "item_id": order.item_id,
        "qty": order.qty,
        "unit_price_cents": order.unit_price_cents,
        "amount_cents": order.amount_cents,
    }
    return base


ORDER_SERIALIZERS: dict[str, Callable[[Order], dict]] = {
    "v1": serialize_order_v1,
    "v2": serialize_order_v2,
}

# 每个版本响应中"直接透传规范字段"的字段集合，用于测试检测静默丢失。
V1_SHARED_FIELDS = frozenset(
        {"order_id", "item_id", "qty", "unit_price_cents", "amount_cents", "currency", "note", "created_at"}
)
FIELD_PROVENANCE: dict[str, frozenset[str]] = {
    "v1": V1_SHARED_FIELDS,
    "v2": V1_SHARED_FIELDS,  # v2 = v1 全集 + 嵌套 line（纯加法）
}


def serialize_order(order: Order, version: str) -> dict:
    try:
        return ORDER_SERIALIZERS[version](order)
    except KeyError:
        raise ValueError(f"no serializer registered for version {version!r}")


CANONICAL_FIELDS = frozenset(asdict(Order(
    order_id="x", item_id="x", qty=1, unit_price_cents=1,
    amount_cents=1, currency="CNY", note=None, created_at="1970-01-01T00:00:00Z"
)).keys())
