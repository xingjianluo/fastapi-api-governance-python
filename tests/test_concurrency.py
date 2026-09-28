"""Concurrency tests through the real ASGI app (separate TestClient/thread).

Uses a SystemClock-backed config (real wall clock) so token-bucket timing and
thread scheduling behave like production. Nonces are unique per request; the
idempotency key is intentionally shared to prove single-effect execution.
"""

import json
import threading
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app.clock import SystemClock
from app.config import build_default_config
from app.main import create_app
from app.signature import sign

from .conftest import log_decision

BODY = json.dumps({"product": "widget", "quantity": 1, "amount": 4200}).encode()


@pytest.fixture
def live_app():
    config = build_default_config(SystemClock())
    return create_app(config), config


def _send(app, secret: bytes, key: str, body: bytes = BODY,
          caller: str = "acme", version: str | None = None,
          nonce: str | None = None):
    client = TestClient(app)
    ts = str(app.state.config.clock.now())
    nonce = nonce or f"c-{threading.get_ident()}-{id(body)}-{ts}-{id(object())}"
    sig = sign(secret, "POST", "/orders", ts, nonce, body)
    headers = {
        "X-Caller-Id": caller,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Signature": sig,
        "Idempotency-Key": key,
    }
    if version:
        headers["X-Api-Version"] = version
    r = client.post("/orders", content=body, headers=headers)
    return r


def test_concurrent_duplicate_submissions_have_single_effect(live_app):
    from app.ratelimit import RateLimitRule
    app, config = live_app
    # Wide limits so this test isolates idempotency, not rate limiting.
    config.rate_limiter.set_rule(
        "acme", RateLimitRule(rate_per_second=1000.0, burst=1000,
                              daily_quota=1_000_000))
    n = 20
    barrier = threading.Barrier(n)
    results: list = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        r = _send(app, b"acme-new-secret", "concurrent-key-1")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads: t.start()
    for t in threads: t.join()

    codes = Counter(r.status_code for r in results)
    order_ids = [r.json().get("id") for r in results if r.status_code == 201]
    replayed = sum(1 for r in results
                   if r.headers.get("Idempotency-Replayed") == "true")
    unique_ids = set(order_ids)

    log_decision(
        "concurrency.duplicate_single_effect",
        {"threads": n, "idempotency_key": "concurrent-key-1"},
        "exactly one execute; identical concurrent retries block and replay",
        f"codes={dict(codes)}, unique_order_ids={unique_ids}, replayed={replayed}",
    )
    assert codes[201] == n
    assert len(unique_ids) == 1, order_ids
    assert replayed == n - 1
    # business storage confirms only one order was ever created
    assert config.orders.count() == 1


def test_concurrent_conflicting_payloads_only_first_wins(live_app):
    app, config = live_app
    barrier = threading.Barrier(2)
    out = {}

    def worker(name, payload):
        barrier.wait()
        out[name] = _send(app, b"acme-new-secret", "concurrent-conflict",
                          body=payload)

    t1 = threading.Thread(
        target=worker, args=("a", json.dumps(
            {"product": "alpha", "quantity": 1, "amount": 100}).encode()))
    t2 = threading.Thread(
        target=worker, args=("b", json.dumps(
            {"product": "beta", "quantity": 2, "amount": 200}).encode()))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = sorted(v.status_code for v in out.values())
    conflict = next(r for r in out.values()
                    if r.status_code == 409)
    log_decision(
        "concurrency.payload_conflict",
        {"products": ["alpha", "beta"], "same_key": True},
        "first stored result survives; loser gets key_conflict, no overwrite",
        f"statuses={statuses}, code={conflict.json()['error']['code']}",
    )
    assert statuses == [201, 409]
    assert conflict.json()["error"]["code"] == "idempotency.key_conflict"
    assert config.orders.count() == 1


def test_concurrent_burst_rate_limited_without_affecting_accepted(live_app):
    from app.ratelimit import RateLimitRule
    app, config = live_app
    # globex: tiny burst to force 429s under a simultaneous burst.
    config.rate_limiter.set_rule(
        "globex", RateLimitRule(rate_per_second=1.0, burst=3, daily_quota=1000))
    n = 25
    barrier = threading.Barrier(n)
    results: list = []
    lock = threading.Lock()

    def worker(i):
        barrier.wait()
        client = TestClient(app)
        ts = str(config.clock.now())
        nonce = f"burst-{i}-{ts}"
        sig = sign(b"globex-secret", "GET", "/orders/x", ts, nonce, b"")
        r = client.get("/orders/x", headers={
            "X-Caller-Id": "globex", "X-Timestamp": ts,
            "X-Nonce": nonce, "X-Signature": sig})
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads: t.start()
    for t in threads: t.join()

    codes = Counter(r.status_code for r in results)
    accepted = codes[404]            # passed governance; order simply absent
    rejected = codes[429]
    retry_afters = [r.headers["Retry-After"] for r in results
                    if r.status_code == 429]
    log_decision(
        "concurrency.rate_burst",
        {"caller": "globex", "threads": n, "burst": 3},
        "atomic token deduction: accepted never exceeds burst; all 429 "
        "carry Retry-After; accepted requests remain 404 (unaffected)",
        f"accepted={accepted}, rejected={rejected}, "
        f"retry_after_samples={retry_afters[:3]}",
    )
    assert accepted == 3, codes
    assert rejected == n - 3
    assert all(int(x) >= 1 for x in retry_afters)


def test_throttled_caller_does_not_touch_other_callers(live_app):
    from app.ratelimit import RateLimitRule
    app, config = live_app
    config.rate_limiter.set_rule(
        "acme", RateLimitRule(rate_per_second=1.0, burst=1, daily_quota=1000))
    config.rate_limiter.set_rule(
        "globex", RateLimitRule(rate_per_second=1000.0, burst=1000,
                                daily_quota=1_000_000))
    # Exhaust acme's bucket concurrently.
    def acme_hit(i):
        return _send(app, b"acme-new-secret", f"acme-burn-{i}",
                     caller="acme")
    threads = [threading.Thread(target=acme_hit, args=(i,)) for i in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()

    # globex still fully served with its own independent bucket/quota.
    oks = []
    for i in range(5):
        client = TestClient(app)
        ts = str(config.clock.now())
        nonce = f"iso-{i}-{ts}"
        sig = sign(b"globex-secret", "GET", "/orders/x", ts, nonce, b"")
        r = client.get("/orders/x", headers={
            "X-Caller-Id": "globex", "X-Timestamp": ts,
            "X-Nonce": nonce, "X-Signature": sig})
        oks.append(r.status_code)
    log_decision("concurrency.caller_isolation",
                 {"throttled": "acme", "checked": "globex"},
                 "per-caller buckets/quota; one caller's 429s don't leak",
                 f"globex_statuses={oks}")
    assert oks == [404] * 5
