"""写入请求的幂等处理。

存储语义（进程内、线程安全）：

- 对每个 Idempotency-Key 记录一个状态机：``in_progress -> completed``。
- ``begin`` 用同一把锁原子判定，保证并发重试里只有一个请求拿到
  ``acquired``；其余请求要么收到 ``in_progress``（稍后重放即可拿到同一结果），
  要么在首次请求完成后收到 ``replay`` 并取回**同一份**结果。
- 幂等键复用于不同内容指纹时返回 ``conflict``，绝不覆盖已有结果。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .errors import IdempotencyConflict, IdempotencyInProgress

ACQUIRED = "acquired"
REPLAY = "replay"
CONFLICT = "conflict"
IN_PROGRESS = "in_progress"


@dataclass(frozen=True)
class IdempotentResult:
    status_code: int
    body: bytes


@dataclass
class _Entry:
    fingerprint: str
    result: IdempotentResult | None = None
    started_at: float = 0.0


class IdempotencyStore:
    def __init__(self, lock_timeout_seconds: float = 30.0) -> None:
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()
        self._lock_timeout = lock_timeout_seconds

    def begin(self, key: str, fingerprint: str, now: float | None = None) -> str:
        """尝试开始一个幂等请求。

        Returns:
            ``acquired``：调用方获得唯一执行权；
            ``in_progress``：同指纹的前一次请求仍在执行；
            ``replay``：已有完成结果，调用方应通过 :meth:`get` 取回；
            ``conflict``：键被不同指纹占用（不会覆盖）。
        """
        now = time.time() if now is None else now
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = _Entry(fingerprint=fingerprint, started_at=now)
                return ACQUIRED

            if entry.fingerprint != fingerprint:
                return CONFLICT

            if entry.result is None:
                return IN_PROGRESS
            return REPLAY

    def finish(self, key: str, result: IdempotentResult) -> None:
        """由 acquired 的持有者在业务执行完成后登记结果。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                # 理论上不会发生：begin 之后才允许 finish
                raise KeyError(f"unknown idempotency key: {key!r}")
            entry.result = result

    def get(self, key: str) -> IdempotentResult | None:
        with self._lock:
            entry = self._entries.get(key)
            return entry.result if entry is not None else None

    def wait_for_result(
        self, key: str, timeout: float = 2.0, step: float = 0.005, now: float | None = None
    ) -> IdempotentResult | None:
        """并发重试时等待首个请求落结果；超时返回 None（调用方回 in_progress）。"""
        deadline = (time.time() if now is None else now) + timeout
        while True:
            result = self.get(key)
            if result is not None:
                return result
            cur = time.time() if now is None else now
            if cur >= deadline:
                return None
            time.sleep(step)

    def claim_or_raise(
        self, key: str, fingerprint: str, now: float | None = None
    ) -> str:
        """begin 的异常风格封装，便于 FastAPI 依赖直接使用。"""
        outcome = self.begin(key, fingerprint, now=now)
        if outcome == CONFLICT:
            raise IdempotencyConflict(
                "Idempotency-Key 已被内容不同的请求占用，拒绝覆盖已有结果",
                details={"idempotency_key": key},
            )
        if outcome == IN_PROGRESS:
            raise IdempotencyInProgress(
                "相同幂等键的前一次请求仍在处理中，请稍后重试",
                details={"idempotency_key": key, "retry_after_seconds": 1},
            )
        return outcome

    def reset_for_tests(self) -> None:
        with self._lock:
            self._entries.clear()
