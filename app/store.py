"""In-memory order repository (local verification only)."""

from __future__ import annotations

import itertools
import threading

from app.models import OrderRecord


class OrderStore:
    def __init__(self) -> None:
        self._orders: dict[str, OrderRecord] = {}
        self._lock = threading.Lock()
        self._counter = itertools.count(1)

    def add(self, record: OrderRecord) -> OrderRecord:
        with self._lock:
            if record.id in self._orders:
                raise ValueError(f"duplicate order id {record.id}")
            self._orders[record.id] = record
            return record

    def get(self, order_id: str) -> OrderRecord | None:
        with self._lock:
            return self._orders.get(order_id)

    def next_id(self) -> str:
        with self._lock:
            return f"ORD-{next(self._counter):06d}"

    def count(self) -> int:
        with self._lock:
            return len(self._orders)
