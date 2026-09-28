"""按调用方（caller）维度的两层限流：秒级突发 + 长周期配额。

- 秒级窗口：``rps_limit``，固定 1 秒窗口，应对突发并发。
- 配额窗口：``quota_limit`` / ``quota_window_seconds``，固定对齐到 epoch。
- check 在同一把锁内完成"读计数 -> 判定 -> 加计数"，并发请求不可能
  竞态击穿（不会出现两个请求同时读到未满计数而双双放行）。
- 被拒请求不增加任何计数，因此不会挤占已接受请求的额度。
- 规则可热替换：替换后**新阈值立即生效**，当前窗口的已用计数保留，
  行为可预期：remaining = 新阈值 - 当前窗口已用计数。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .errors import BurstRateLimited, QuotaExhausted

BURST_SCOPE = "burst"
QUOTA_SCOPE = "quota"
OK = "ok"


@dataclass(frozen=True)
class RateLimitRule:
    caller: str
    rps_limit: int
    quota_limit: int
    quota_window_seconds: int


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    scope: str
    reason: str
    retry_after_seconds: int
    limit: int
    remaining: int
    reset_at: float

    def headers(self) -> dict[str, str]:
        h = {
            "X-RateLimit-Scope": self.scope,
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(int(self.reset_at)),
        }
        if not self.allowed:
            h["Retry-After"] = str(self.retry_after_seconds)
        return h


class RateLimiter:
    def __init__(
        self,
        rules: dict[str, RateLimitRule],
        default: RateLimitRule | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._rules = dict(rules)
        self._default = default
        # (caller, scope, window_start) -> 已接受请求数
        self._counters: dict[tuple[str, str, int], int] = {}

    # -- 规则配置 ----------------------------------------------------------
    def update_rules(
        self, rules: dict[str, RateLimitRule], default: RateLimitRule | None = None
    ) -> None:
        with self._lock:
            self._rules = dict(rules)
            if default is not None:
                self._default = default

    def rule_for(self, caller: str) -> RateLimitRule:
        rule = self._rules.get(caller) or self._default
        if rule is None:
            # 未配置且没有默认规则：以调用方名为键的"零额度"判定交给调用方，
            # 这里给一个保守默认（每秒 10、每小时 100），保证可解释。
            rule = RateLimitRule(caller=caller, rps_limit=10, quota_limit=100, quota_window_seconds=3600)
        return rule

    # -- 核心判定 ----------------------------------------------------------
    def _current_count(self, caller: str, scope: str, window_start: int) -> int:
        return self._counters.get((caller, scope, window_start), 0)

    def _prune(self, now_window_burst: int, now_window_quota: int) -> None:
        dead = [
            k
            for k in self._counters
            if (k[1] == BURST_SCOPE and k[2] < now_window_burst)
            or (k[1] == QUOTA_SCOPE and k[2] < now_window_quota)
        ]
        for k in dead:
            self._counters.pop(k, None)

    def check(self, caller: str, now: float | None = None) -> RateLimitDecision:
        """原子地判定并占用一个额度；拒绝时不占用额度。"""
        import time

        now = time.time() if now is None else now
        with self._lock:
            rule = self.rule_for(caller)

            burst_start = int(now)
            quota_len = max(1, rule.quota_window_seconds)
            quota_start = int(now // quota_len) * quota_len

            self._prune(burst_start, quota_start)

            burst_used = self._current_count(caller, BURST_SCOPE, burst_start)
            # 1) 秒级突发：先判定，被拒不计数
            if burst_used >= rule.rps_limit:
                reset_at = float(burst_start + 1)
                return RateLimitDecision(
                    allowed=False,
                    scope=BURST_SCOPE,
                    reason="rps_limit_exceeded",
                    retry_after_seconds=max(1, int(reset_at - now) + 1),
                    limit=rule.rps_limit,
                    remaining=0,
                    reset_at=reset_at,
                )

            quota_used = self._current_count(caller, QUOTA_SCOPE, quota_start)
            # 2) 长周期配额
            if quota_used >= rule.quota_limit:
                reset_at = float(quota_start + quota_len)
                return RateLimitDecision(
                    allowed=False,
                    scope=QUOTA_SCOPE,
                    reason="quota_exhausted",
                    retry_after_seconds=max(1, int(reset_at - now) + 1),
                    limit=rule.quota_limit,
                    remaining=0,
                    reset_at=reset_at,
                )

            # 3) 两层均通过：同锁内加计数（accepted 请求才计数）
            self._counters[(caller, BURST_SCOPE, burst_start)] = burst_used + 1
            self._counters[(caller, QUOTA_SCOPE, quota_start)] = quota_used + 1

            return RateLimitDecision(
                allowed=True,
                scope=OK,
                reason="accepted",
                retry_after_seconds=0,
                limit=rule.rps_limit,
                remaining=min(rule.rps_limit - burst_used - 1, rule.quota_limit - quota_used - 1),
                reset_at=float(burst_start + 1),
            )

    # -- 供 HTTP 层抛出语义化异常 ------------------------------------------
    def enforce(self, caller: str, now: float | None = None) -> RateLimitDecision:
        decision = self.check(caller, now=now)
        if not decision.allowed:
            exc_cls = QuotaExhausted if decision.scope == QUOTA_SCOPE else BurstRateLimited
            raise exc_cls(
                f"调用方 {caller} 触发{('配额' if decision.scope == QUOTA_SCOPE else '突发')}限流",
                details={
                    "caller": caller,
                    "scope": decision.scope,
                    "limit": decision.limit,
                    "retry_after_seconds": decision.retry_after_seconds,
                    "reset_at": decision.reset_at,
                },
            )
        return decision

    def reset_for_tests(self) -> None:
        with self._lock:
            self._counters.clear()
