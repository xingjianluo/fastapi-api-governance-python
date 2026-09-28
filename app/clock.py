"""Pluggable clock protocol for deterministic tests."""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Clock(Protocol):
    """Returns the current POSIX timestamp in seconds (UTC)."""

    def now(self) -> float: ...


class SystemClock:
    """Wall-clock implementation backed by :func:`time.time`."""

    def now(self) -> float:
        return time.time()


class FakeClock:
    """Manually controllable clock for deterministic time-window tests."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = float(start)
        self._cond = threading.Condition()

    def now(self) -> float:
        with self._cond:
            return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward and wake up anyone waiting on it."""
        with self._cond:
            self._now += float(seconds)
            self._cond.notify_all()

    def sleep(self, seconds: float) -> None:
        """Sleep using the fake clock; returns once the clock advances enough."""
        deadline = self.now() + float(seconds)
        with self._cond:
            while self._now < deadline:
                self._cond.wait(timeout=5.0)
