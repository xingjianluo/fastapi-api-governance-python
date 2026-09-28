"""Idempotency store with concurrent duplicate collapse.

Semantics for a given idempotency key:

* first request executes the handler once; concurrent retries with the same
  payload **block until the first completes** and then observe the *same*
  stored result (marked via ``Idempotency-Replayed: true``);
* a different payload under the same key -> 409 ``idempotency.key_conflict``,
  the stored result is never overwritten;
* if the first attempt fails before storing a result, the in-flight slot is
  released so a retry can execute (failures are not cached).
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class StoredResponse:
    status_code: int
    body: bytes
    headers: dict[str, str]
    request_hash: str
    request_preview: str


@dataclass
class _Slot:
    request_hash: str
    request_preview: str
    event: threading.Event = field(default_factory=threading.Event)
    response: StoredResponse | None = None
    failed: bool = False


class IdempotencyStore:
    def __init__(self) -> None:
        self._slots: dict[str, _Slot] = {}
        self._lock = threading.Lock()

    @staticmethod
    def hash_payload(raw_body: bytes, content_type: str | None = None) -> str:
        h = hashlib.sha256()
        h.update((content_type or "").encode())
        h.update(b"\0")
        h.update(raw_body or b"")
        return h.hexdigest()

    @contextmanager
    def reserve(self, key: str, request_hash: str, request_preview: str,
                wait_timeout: float = 10.0):
        """Acquire execution rights for *key*.

        Yields one of:
          * ``("execute", slot)``  - caller MUST call :meth:`complete` /
            :meth:`fail` on the slot;
          * ``("replay", stored)`` - an identical earlier request finished;
          * ``("conflict", stored_or_None)`` - payload differs.

        If an earlier identical attempt finished with :meth:`fail` (handler
        failed before producing a stored response), the waiting retry takes
        over the same slot and executes.
        """
        slot: _Slot | None = None
        mode = "new"
        conflict_response: StoredResponse | None = None
        with self._lock:
            existing = self._slots.get(key)
            if existing is None:
                slot = _Slot(request_hash, request_preview)
                self._slots[key] = slot
            elif existing.request_hash != request_hash:
                mode = "conflict"
                conflict_response = existing.response
            else:
                mode = "existing"
                slot = existing

        if mode == "conflict":
            yield ("conflict", conflict_response)
            return

        if mode == "new":
            try:
                yield ("execute", slot)
            except BaseException:
                with self._lock:
                    if self._slots.get(key) is slot:
                        del self._slots[key]
                raise
            return

        # Existing slot for the same payload.
        if slot.response is not None:
            yield ("replay", slot.response)
            return

        if slot.event.wait(timeout=wait_timeout):
            if slot.response is not None:
                yield ("replay", slot.response)
                return
            if slot.failed:
                # Take over: reset the slot and execute as the authority.
                slot.event = threading.Event()
                slot.failed = False
                try:
                    yield ("execute", slot)
                except BaseException:
                    with self._lock:
                        if self._slots.get(key) is slot:
                            del self._slots[key]
                    raise
                return

        # Timeout or impossible state: refuse rather than double-execute.
        yield ("conflict", None)

    def complete(self, slot: _Slot, response: StoredResponse) -> None:
        slot.failed = False
        slot.response = response
        slot.event.set()

    def fail(self, slot: _Slot) -> None:
        slot.failed = True
        slot.event.set()

    def stored(self, key: str) -> StoredResponse | None:
        with self._lock:
            slot = self._slots.get(key)
            return slot.response if slot else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._slots)
