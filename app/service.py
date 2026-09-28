"""示例业务服务：创建订单。

治理层负责"能不能做"，这里只负责"怎么做"。价格表固定、计价确定性，
因此同一输入在 v1/v2 下产出的业务语义完全一致。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from .schemas import CreateOrderRequest, Order

PRICE_TABLE_CENTS = {
    "item-a": 1000,
    "item-b": 2500,
    "item-c": 320,
}


class OrderNotFound(Exception):
    pass


class OrderService:
    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._lock = threading.Lock()
        self._seq = 0

    def create(self, req: CreateOrderRequest) -> Order:
        unit_price = PRICE_TABLE_CENTS.get(req.item_id, 500)
        amount = unit_price * req.qty
        with self._lock:
            self._seq += 1
            order_id = f"order-{self._seq:06d}"
            order = Order(
                order_id=order_id,
                item_id=req.item_id,
                qty=req.qty,
                unit_price_cents=unit_price,
                amount_cents=amount,
                currency="CNY",
                note=req.note,
                created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            self._orders[order_id] = order
        return order

    def get(self, order_id: str) -> Order:
        with self._lock:
            order = self._orders.get(order_id)
        if order is None:
            raise OrderNotFound(order_id)
        return order
