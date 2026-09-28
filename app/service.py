"""Business service with version-aware response shaping.

The write path is version-independent: every version executes the same
validation and persistence. Responses are then projected to the negotiated
API version. The projection is **strictly additive**: every field present in
v1 keeps the same name, type and meaning in v2, and v2 only adds new fields.
This guarantees no silent field loss or semantic drift between adjacent
versions.
"""

from __future__ import annotations

from app.clock import Clock
from app.models import CreateOrderRequest, OrderRecord
from app.store import OrderStore


# Fields shared (identical semantics) by every version, in stable order.
_SHARED_FIELDS = ("id", "product", "quantity", "amount", "currency",
                  "note", "created_at")


class OrderService:
    def __init__(self, store: OrderStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def create_order(self, payload: CreateOrderRequest) -> OrderRecord:
        """Version-neutral creation: one code path for v1 and v2."""
        record = OrderRecord(
            id=self._store.next_id(),
            product=payload.product,
            quantity=payload.quantity,
            amount=payload.amount,
            currency=payload.currency.upper(),
            note=payload.note,
            created_at=self._clock.now(),
        )
        return self._store.add(record)

    def get_order(self, order_id: str) -> OrderRecord | None:
        return self._store.get(order_id)

    @staticmethod
    def shape(record: OrderRecord, version: int) -> dict:
        """Project the canonical record onto a specific API version."""
        base = {name: getattr(record, name) for name in _SHARED_FIELDS}
        if version == 1:
            # v1 view: stable legacy fields only.
            return dict(base)
        if version == 2:
            # v2 view: v1 fields untouched, purely additive ``status``.
            body = dict(base)
            body["status"] = record.status
            return body
        raise ValueError(f"no response projection for version {version}")
