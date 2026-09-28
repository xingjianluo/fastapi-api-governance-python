"""按调用方限流与配额测试，含突发并发场景。"""

import threading

import pytest

from app.governance.errors import BurstRateLimited, QuotaExhausted
from app.governance.ratelimit import BURST_SCOPE, QUOTA_SCOPE, RateLimiter, RateLimitRule

from tests.govlog import gov_log


def build_limiter():
    return RateLimiter(
        {
            "premium": RateLimitRule("premium", rps_limit=5, quota_limit=1000, quota_window_seconds=3600),
            "basic": RateLimitRule("basic", rps_limit=2, quota_limit=3, quota_window_seconds=3600),
        },
        default=RateLimitRule("default", rps_limit=1, quota_limit=10, quota_window_seconds=3600),
    )


def test_concurrent_burst_counter_not_broken():
    """20 个线程同一秒突发，只有 rps_limit 个请求被接受。"""
    limiter = build_limiter()
    now = 10_000.0
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        d = limiter.check("premium", now=now)
        with lock:
            results.append(d.allowed)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    accepted = sum(results)
    rejected = len(results) - accepted
    gov_log(
        "concurrent-burst",
        {"caller": "premium", "threads": 20, "rps_limit": 5},
        {"accepted": accepted, "rejected": rejected},
        "exactly limit accepted" if accepted == 5 else "BROKEN",
    )
    assert accepted == 5
    assert rejected == 15


def test_rejected_carries_retry_after_and_distinct_code():
    limiter = build_limiter()
    now = 20_000.0
    limiter.check("basic", now=now)
    limiter.check("basic", now=now)
    d = limiter.check("basic", now=now)
    gov_log(
        "burst-rejected",
        {"caller": "basic", "attempt": 3, "rps_limit": 2},
        {"retry_after_seconds": d.retry_after_seconds, "scope": d.scope},
        "REJECTED",
    )
    assert not d.allowed
    assert d.scope == BURST_SCOPE
    assert d.retry_after_seconds >= 1
    with pytest.raises(BurstRateLimited) as exc:
        limiter.enforce("basic", now=now)
    assert exc.value.code == "burst_rate_limited"


def test_rejected_requests_do_not_consume_quota():
    """被突发限流拒绝的请求不计数，不影响后续被接受的请求与配额。"""
    limiter = build_limiter()
    now = 30_000.0
    for _ in range(2):
        assert limiter.check("basic", now=now).allowed
    # 接下来一批全部被秒级限流拒绝
    for _ in range(50):
        assert not limiter.check("basic", now=now).allowed
    # 下一秒：秒级额度重置；长周期配额只统计此前被接受的 2 次
    next_second = now + 1
    d_first = limiter.check("basic", now=next_second)  # 秒级OK，配额 2->3
    d_second = limiter.check("basic", now=next_second)  # 秒级OK，但配额已满
    gov_log(
        "reject-no-side-effects",
        {"caller": "basic", "rejected_in_previous_second": 50},
        {"next_second_first": d_first.allowed, "next_second_second_scope": d_second.scope},
        "rejected requests never counted",
    )
    assert d_first.allowed
    assert not d_second.allowed
    assert d_second.scope == QUOTA_SCOPE  # 精确地被配额而非秒级窗口拒绝


def test_quota_exhaustion_distinct_from_burst():
    limiter = build_limiter()
    now = 40_000.0
    statuses = [limiter.check("basic", now=now + i).allowed for i in range(3)]
    assert statuses == [True, True, True]
    d = limiter.check("basic", now=now + 3)  # 新的一秒，秒级额度充足但配额耗尽
    gov_log(
        "quota-exhausted",
        {"caller": "basic", "quota_limit": 3, "used": 3},
        {"scope": d.scope, "retry_after_seconds": d.retry_after_seconds, "reset_at": d.reset_at},
        "REJECTED",
    )
    assert not d.allowed
    assert d.scope == QUOTA_SCOPE
    with pytest.raises(QuotaExhausted) as exc:
        limiter.enforce("basic", now=now + 3)
    assert exc.value.code == "quota_exhausted"


def test_callers_isolated():
    limiter = build_limiter()
    now = 50_000.0
    assert limiter.check("basic", now=now).allowed
    assert limiter.check("basic", now=now).allowed
    assert not limiter.check("basic", now=now).allowed
    # premium 不受 basic 影响
    assert limiter.check("premium", now=now).allowed
    gov_log("caller-isolation", {"basic_full": True}, {"premium": "still served"}, "isolated")


def test_rule_update_is_predictable():
    """规则热更：新阈值立即生效，当前窗口已用计数保留，remaining 可预期。"""
    limiter = build_limiter()
    now = 60_000.0
    for _ in range(3):
        limiter.check("premium", now=now)
    limiter.update_rules(
        {"premium": RateLimitRule("premium", rps_limit=4, quota_limit=1000, quota_window_seconds=3600)}
    )
    d4 = limiter.check("premium", now=now)
    d5 = limiter.check("premium", now=now)
    gov_log(
        "rule-update",
        {"old_rps": 5, "new_rps": 4, "used_in_window": 3},
        {"4th_allowed": d4.allowed, "5th_allowed": d5.allowed},
        "new limit applies against retained usage",
    )
    assert d4.allowed  # 已用 3，新阈值 4，还剩 1
    assert not d5.allowed  # 已用 4，达到新阈值
