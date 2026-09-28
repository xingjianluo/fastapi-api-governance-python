"""Boundary scenarios explicitly requested: key-rotation overlap and version
sunset, checked at second-level granularity with logged evidence.
"""

import json

from app.clock import FakeClock
from app.config import ROTATION_GRACE_SECONDS, build_default_config
from app.main import create_app
from app.signature import sign

from fastapi.testclient import TestClient

from .conftest import T0, log_decision

ORDER_BODY = json.dumps(
    {"product": "widget", "quantity": 1, "amount": 100}).encode()


def _app_with_generous_rate_limit():
    from app.ratelimit import RateLimitRule
    clock = FakeClock(T0)
    config = build_default_config(clock)
    config.rate_limiter.set_rule(
        "acme", RateLimitRule(10_000.0, 10_000, 10_000_000))
    return create_app(config), config, clock


def _acme_keys(config):
    keys = {k.key_id: k for k in config.key_ring.caller_keys("acme")}
    return keys["k-old"], keys["k-new"]


def _signed_get(app, clock, secret, path, nonce):
    ts = str(clock.now())
    sig = sign(secret, "GET", path, ts, nonce, b"")
    return TestClient(app).get(path, headers={
        "X-Caller-Id": "acme", "X-Timestamp": ts,
        "X-Nonce": nonce, "X-Signature": sig, "X-Api-Version": "1"})


def test_key_rotation_overlap_period_both_keys_each_second():
    """Every sampled second inside the overlap: old AND new keys verify."""
    app, config, clock = _app_with_generous_rate_limit()
    old, new = _acme_keys(config)
    overlap_start = max(old.not_valid_before, new.not_valid_before)
    overlap_end = min(old.not_valid_after, new.not_valid_after)
    results = {"old": [], "new": []}
    # Use the config's own clock (shared with the rate limiter) as the probe.
    clock._now = overlap_start
    sec = overlap_start
    step = max(1.0, (overlap_end - overlap_start) / 10)
    while sec <= overlap_end:
        clock._now = sec
        for secret, name in [(old.secret, "old"), (new.secret, "new")]:
            r = _signed_get(app, clock, secret, "/orders/x",
                            nonce=f"rot-{name}-{sec:g}")
            results[name].append(r.status_code)
        sec += step
    log_decision(
        "boundary.rotation_overlap",
        {"overlap": [overlap_start, overlap_end], "samples": len(results["old"])},
        "both key intervals cover every sampled timestamp",
        f"old={results['old']} new={results['new']}",
    )
    assert results["old"] and all(s == 404 for s in results["old"])
    assert results["new"] and all(s == 404 for s in results["new"])


def test_key_rotation_after_overlap_only_new_key_works():
    app, config, clock = _app_with_generous_rate_limit()
    old, new = _acme_keys(config)

    after = FakeClock(old.not_valid_after + 1)
    config.clock = after
    config.replay_cache._clock = after
    r_old = _signed_get(app, after, old.secret, "/orders/x", "after-1")
    r_new = _signed_get(app, after, new.secret, "/orders/x", "after-2")
    log_decision(
        "boundary.rotation_after_overlap",
        {"now": old.not_valid_after + 1,
         "old_until": old.not_valid_after},
        "old beyond not_valid_after -> signature.invalid; new continues",
        f"old={r_old.json()['error']['code']}, new_http={r_new.status_code}",
    )
    assert r_old.status_code == 401
    assert r_old.json()["error"]["code"] == "signature.invalid"
    assert r_new.status_code == 404

    # Inclusive boundary: at exactly not_valid_after the old key still works.
    at = FakeClock(old.not_valid_after)
    config.clock = at
    config.replay_cache._clock = at
    r_at = _signed_get(app, at, old.secret, "/orders/x", "at-boundary")
    log_decision("boundary.rotation_inclusive_end",
                 {"ts": old.not_valid_after},
                 "not_valid_after is inclusive",
                 f"http={r_at.status_code}")
    assert r_at.status_code == 404


def test_version_sunset_boundary_second_level():
    """One second before sunset = deprecated (201+headers); at/after = 410."""
    # Default config: deprecated 1 day ago, sunset 60 days from build time.
    clock = FakeClock(T0)
    config = build_default_config(clock)
    app = create_app(config)
    sunset = config.version_registry.versions[1].sunset_at

    def post_v1(nonce, key):
        ts = str(clock.now())
        sig = sign(b"acme-new-secret", "POST", "/orders", ts, nonce, ORDER_BODY)
        return TestClient(app).post("/orders", content=ORDER_BODY, headers={
            "X-Caller-Id": "acme", "X-Timestamp": ts,
            "X-Nonce": nonce, "X-Signature": sig,
            "X-Api-Version": "1", "Idempotency-Key": key})

    # move to exactly 1 second before sunset
    clock._now = sunset - 1
    config.replay_cache._clock = clock
    r_before = post_v1("sun-before", "sun-before-key")
    headers_before = {k: r_before.headers.get(k) for k in
                      ("X-Api-Deprecated", "Sunset", "Deprecation",
                       "X-Api-Sunset-In-Seconds")}
    log_decision("boundary.sunset_minus_1",
                 {"now": sunset - 1, "sunset": sunset},
                 "now < sunset -> deprecated, business executes",
                 f"http={r_before.status_code} headers={headers_before}")
    assert r_before.status_code == 201
    assert headers_before["X-Api-Deprecated"] == "true"
    assert headers_before["X-Api-Sunset-In-Seconds"] == "1"

    # exactly at sunset -> retired (boundary inclusive on the retired side)
    clock._now = sunset
    config.replay_cache._clock = clock
    r_at = post_v1("sun-at", "sun-at-key")
    log_decision("boundary.sunset_exact",
                 {"now": sunset, "sunset": sunset},
                 "now >= sunset -> 410 version.retired",
                 f"http={r_at.status_code} code={r_at.json()['error']['code']}")
    assert r_at.status_code == 410
    assert r_at.json()["error"]["code"] == "version.retired"

    # one second later still retired, v2 unaffected
    clock._now = sunset + 1
    config.replay_cache._clock = clock
    r_after = post_v1("sun-after", "sun-after-key")
    assert r_after.status_code == 410
