"""Pydantic schemas shared across API versions.

V1 and V2 accept the *same* write contract. The response shaping differs per
version (see ``app.service``) but shared fields keep identical names and
semantics; V2 is strictly additive so no field is silently dropped or
reinterpreted between adjacent versions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CreateOrderRequest(BaseModel):
    """Write payload for ``POST /orders`` (identical across v1 and v2)."""

    product: str = Field(min_length=1, max_length=200)
    quantity: int = Field(gt=0, le=10_000)
    amount: int = Field(gt=0, description="Amount in integer cents")
    currency: str = Field(default="USD", min_length=3, max_length=3)
    note: str | None = Field(default=None, max_length=500)


class OrderRecord(BaseModel):
    """Canonical internal representation of an accepted order."""

    id: str
    product: str
    quantity: int
    amount: int
    currency: str
    note: str | None = None
    status: Literal["accepted"] = "accepted"
    created_at: float


class ErrorBody(BaseModel):
    code: str
    message: str
    detail: str | None = None
