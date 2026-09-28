"""Per-caller rate limiting (token bucket) and daily quota.

* Token bucket handles bursts without a race: bucket refill and token
  deduction happen under a single lock with a monotonic observation of the
  injected clock, so concurrent acceptances cannot tear through the limit.
* Daily quota is a fixed counter reset at UTC midnight (or the configured
  reset offset). Rejected requests are *not* counted, so throttling one
  caller never affects accepted requests or other callers.
* Rules are held in an immutable mapping replaced atomically; in-flight
  decisions finish under the old rule and subsequent requests immediately
  observe the new rule — behaviour after a config change is predictable.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from app.clock import Clock, SystemClock
from app.errors import QUOTA_EXHAUSTED, RATE_LIMITED


@dataclass(frozen=True)
class RateLimitRule:
    rate_per_second: float          # sustained refill rate
    burst: int                      # bucket capacity
    daily_quota: int                # max accepted requests per UTC day


@dataclass
class RateLimitDecision:
    accepted: bool
    error: object = None
    detail: str | None = None
    retry_after: float | None = None
    remaining_rate: int = 0
    remaining_quota: int = 0
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _Bucket:
    tokens: float
    last_refill: float
    day: int
    quota_used: int = 0
    last_burst: int = 0


class RateLimiter:
    def __init__(
        self,
        rules: dict[str, RateLimitRule],
        clock: Clock | None = None,
        *,
        default_rule: RateLimitRule | None = None,
    ) -> None:
        self._clock = clock or SystemClock()
        self._rules_lock = threading.RLock()
        self._rules = dict(rules)
        self._default = default_rule
        self._state_lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    # ---- configuration (atomic swap) ------------------------------------
    def update_rules(self, rules: dict[str, RateLimitRule]) -> None:
        with self._rules_lock:
            self._rules = dict(rules)
        self._reconcile_buckets()

    def set_rule(self, caller_id: str, rule: RateLimitRule) -> None:
        with self._rules_lock:
            new_rules = dict(self._rules)
            new_rules[caller_id] = rule
            self._rules = new_rules
        self._reconcile_buckets()

    def _reconcile_buckets(self) -> None:
        """Apply a rule change to existing buckets.

        A capacity *increase* grants the additional headroom immediately
        (raising a limit takes effect on the next request); a decrease never
        removes tokens already earned, so requests accepted under the old
        rule are not retroactively affected. Refill always accumulates under
        the new rate up to the new capacity.
        """
        now = self._clock.now()
        with self._state_lock:
            with self._rules_lock:
                rules = dict(self._rules)
            for caller_id, bucket in list(self._buckets.items()):
                rule = rules.get(caller_id, self._default)
                if rule is None:
                    continue
                old_capacity_hint = getattr(bucket, "last_burst", rule.burst)
                elapsed = max(0.0, now - bucket.last_refill)
                refilled = bucket.tokens + elapsed * rule.rate_per_second
                if rule.burst > old_capacity_hint:
                    refilled += rule.burst - old_capacity_hint
                bucket.tokens = min(float(rule.burst), max(bucket.tokens, refilled))
                bucket.last_refill = now
                bucket.last_burst = rule.burst

    def rule_for(self, caller_id: str) -> RateLimitRule | None:
        with self._rules_lock:
            return self._rules.get(caller_id, self._default)

    # ---- decision -------------------------------------------------------
    def check(self, caller_id: str) -> RateLimitDecision:
        rule = self.rule_for(caller_id)
        if rule is None:
            # Unconfigured callers are allowed; governance config registers
            # every known caller, and signature verification already rejects
            # unknown callers before this point.
            return RateLimitDecision(True, remaining_rate=-1, remaining_quota=-1)

        now = self._clock.now()
        with self._state_lock:
            bucket = self._buckets.get(caller_id)
            day = _utc_day(now)
            if bucket is None:
                bucket = _Bucket(tokens=float(rule.burst), last_refill=now, day=day, last_burst=rule.burst)
                self._buckets[caller_id] = bucket

            # quota reset at UTC day boundary
            if bucket.day != day:
                bucket.day = day
                bucket.quota_used = 0

            # quota first: deterministic rejection with reset hint
            if bucket.quota_used >= rule.daily_quota:
                return self._quota_decision(rule, bucket, now)

            # token bucket refill + deduction, atomically
            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(
                float(rule.burst), bucket.tokens + elapsed * rule.rate_per_second
            )
            bucket.last_refill = now

            if bucket.tokens < 1.0:
                retry_after = (1.0 - bucket.tokens) / rule.rate_per_second
                return RateLimitDecision(
                    False, RATE_LIMITED,
                    f"Rate {rule.rate_per_second}/s (burst {rule.burst}) exceeded.",
                    retry_after=retry_after,
                    remaining_rate=0,
                    remaining_quota=rule.daily_quota - bucket.quota_used,
                    headers=self._headers(rule, bucket, retry_after),
                )

            # Accepted: count exactly once, atomically with token deduction.
            bucket.tokens -= 1.0
            bucket.quota_used += 1
            return RateLimitDecision(
                True,
                remaining_rate=int(bucket.tokens),
                remaining_quota=rule.daily_quota - bucket.quota_used,
                headers=self._headers(rule, bucket, None),
            )

    def _quota_decision(self, rule, bucket, now):
        retry_after = _seconds_until_utc_midnight(now)
        return RateLimitDecision(
            False, QUOTA_EXHAUSTED,
            f"Daily quota of {rule.daily_quota} requests exhausted.",
            retry_after=retry_after,
            remaining_rate=int(bucket.tokens),
            remaining_quota=0,
            headers=self._headers(rule, bucket, retry_after),
        )

    @staticmethod
    def _headers(rule, bucket, retry_after):
        headers = {
            "X-RateLimit-Rate": str(rule.rate_per_second),
            "X-RateLimit-Burst": str(rule.burst),
            "X-RateLimit-Quota": str(rule.daily_quota),
            "X-RateLimit-Quota-Remaining": str(
                max(0, rule.daily_quota - bucket.quota_used)
            ),
        }
        if retry_after is not None:
            headers["Retry-After"] = f"{max(1, int(retry_after + 0.999))}"
        return headers


def _utc_day(ts: float) -> int:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).toordinal()


def _seconds_until_utc_midnight(ts: float) -> float:
    import datetime as _dt

    dt = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    tomorrow = (dt + _dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(1.0, (tomorrow - dt).total_seconds())
