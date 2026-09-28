"""Rate limiting: burst atomicity, quota distinction, hot rule swap."""

import threading

from app.clock import FakeClock
from app.errors import QUOTA_EXHAUSTED, RATE_LIMITED
from app.ratelimit import RateLimiter, RateLimitRule

from .conftest import T0, log_decision


def test_concurrent_burst_cannot_race_through_limit():
    clock = FakeClock(T0)
    rl = RateLimiter(
        {"acme": RateLimitRule(rate_per_second=2.0, burst=10, daily_quota=10_000)},
        clock,
    )
    n = 50
    barrier = threading.Barrier(n)
    decisions = []
    lock = threading.Lock()

    def hit():
        barrier.wait()
        d = rl.check("acme")
        with lock:
            decisions.append(d)

    threads = [threading.Thread(target=hit) for _ in range(n)]
    for t in threads: t.start()
    for t in threads: t.join()

    accepted = sum(1 for d in decisions if d.accepted)
    rejected = [d for d in decisions if not d.accepted]
    log_decision(
        "ratelimit.burst_atomic",
        {"caller": "acme", "threads": n, "burst": 10},
        "refill + deduction under one lock at one clock observation",
        f"accepted={accepted}, rejected={len(rejected)}",
    )
    assert accepted == 10, accepted
    assert len(rejected) == n - 10
    assert all(d.error is RATE_LIMITED for d in rejected)
    assert all("Retry-After" in d.headers for d in rejected)


def test_refill_grants_one_token_per_rate_second():
    clock = FakeClock(T0)
    rl = RateLimiter({"acme": RateLimitRule(2.0, 2, 100)}, clock)
    assert rl.check("acme").accepted
    assert rl.check("acme").accepted
    assert not rl.check("acme").accepted
    clock.advance(0.5)
    assert rl.check("acme").accepted  # one token refilled
    assert not rl.check("acme").accepted


def test_quota_exhaustion_is_distinct_from_rate_limit():
    clock = FakeClock(T0)
    rl = RateLimiter({"acme": RateLimitRule(100.0, 100, daily_quota=3)}, clock)
    for i in range(3):
        assert rl.check("acme").accepted
    d = rl.check("acme")
    log_decision(
        "ratelimit.quota_exhausted",
        {"quota": 3, "used": 3},
        "bucket has tokens but quota_used >= daily_quota",
        f"{d.error.code} retry_after={d.retry_after:.0f}s",
    )
    assert not d.accepted and d.error is QUOTA_EXHAUSTED
    assert d.headers["X-RateLimit-Quota-Remaining"] == "0"
    # rejected requests don't consume more quota, still same rejection
    assert rl.check("acme").error is QUOTA_EXHAUSTED


def test_quota_resets_at_utc_midnight():
    clock = FakeClock(T0)
    rl = RateLimiter({"acme": RateLimitRule(100.0, 100, daily_quota=2)}, clock)
    assert rl.check("acme").accepted
    assert rl.check("acme").accepted
    assert not rl.check("acme").accepted
    clock.advance(24 * 3600 + 1)
    assert rl.check("acme").accepted


def test_throttled_caller_does_not_affect_other_callers_or_accepted():
    clock = FakeClock(T0)
    rl = RateLimiter(
        {"acme": RateLimitRule(1.0, 1, 100),
         "globex": RateLimitRule(100.0, 100, 100)},
        clock,
    )
    assert rl.check("acme").accepted
    rejected = rl.check("acme")
    assert not rejected.accepted
    # globex unaffected by acme's throttling
    for _ in range(5):
        assert rl.check("globex").accepted
    # acme already-accepted request state unchanged (still rate limited)
    assert not rl.check("acme").accepted


def test_rule_hot_swap_is_predictable():
    clock = FakeClock(T0)
    rl = RateLimiter({"acme": RateLimitRule(1.0, 1, 100)}, clock)
    assert rl.check("acme").accepted
    assert not rl.check("acme").accepted
    # operator raises limits; subsequent requests immediately observe them
    rl.set_rule("acme", RateLimitRule(1000.0, 1000, 100_000))
    log_decision("ratelimit.rule_swap", {"caller": "acme"},
                 "atomic rule mapping replacement",
                 "new limits effective on next check")
    for _ in range(20):
        assert rl.check("acme").accepted
    # atomic bulk swap too
    rl.update_rules({})
    assert rl.check("acme").accepted  # unconfigured -> allowed
