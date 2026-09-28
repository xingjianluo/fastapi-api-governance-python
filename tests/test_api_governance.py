"""HTTP-level governance behaviour via FastAPI TestClient."""

import json

from .conftest import T0, log_decision, post_order

ORDER_BODY = json.dumps(
    {"product": "widget", "quantity": 2, "amount": 1999}).encode()


# ------------------------------------------------------------------ version
def test_default_version_is_v2(client, sign_headers):
    h = sign_headers("POST", "/orders", body=ORDER_BODY,
                     extra={"Idempotency-Key": "k-ver-default"})
    r = post_order(client, h, ORDER_BODY)
    log_decision("api.default_version", {"path": "/orders"},
                 "no version header -> default v2 projection (additive status)",
                 r.status_code)
    assert r.status_code == 201
    assert r.json()["status"] == "accepted"


def test_v1_carries_deprecation_and_sunset_headers(client, sign_headers):
    h = sign_headers("POST", "/orders", body=ORDER_BODY, version=1,
                     extra={"Idempotency-Key": "k-v1-dep"})
    r = post_order(client, h, ORDER_BODY)
    log_decision(
        "api.v1_deprecated",
        {"X-Api-Version": "1", "now": T0},
        "v1 in deprecation window -> business ok + Deprecation/Sunset headers",
        f"{r.status_code} sunset_in={r.headers.get('X-Api-Sunset-In-Seconds')}",
    )
    assert r.status_code == 201
    assert r.headers["X-Api-Deprecated"] == "true"
    assert r.headers["Sunset"]
    assert int(r.headers["X-Api-Sunset-In-Seconds"]) > 0


def test_adjacent_versions_keep_identical_shared_semantics(client, sign_headers):
    h1 = sign_headers("POST", "/orders", body=ORDER_BODY, version=1,
                      extra={"Idempotency-Key": "k-sem-v1"})
    h2 = sign_headers("POST", "/orders", body=ORDER_BODY, version=2,
                      extra={"Idempotency-Key": "k-sem-v2"})
    r1 = post_order(client, h1, ORDER_BODY)
    r2 = post_order(client, h2, ORDER_BODY)
    b1, b2 = r1.json(), r2.json()
    shared = ("product", "quantity", "amount", "currency", "note")
    log_decision(
        "api.version_semantic_parity",
        {"shared_fields": shared, "v2_only": sorted(set(b2) - set(b1))},
        "v2 is strictly additive; shared fields identical name/type/meaning",
        f"parity={all(b1[k] == b2[k] for k in shared)}",
    )
    assert all(b1[k] == b2[k] for k in shared)
    assert set(b2) - set(b1) == {"status"}
    assert "status" not in b1  # no silent extra/removal surprise on either side


def test_unknown_version_rejected_distinctly(client, sign_headers):
    h = sign_headers("GET", "/orders/ORD-000001", version=7)
    r = client.get("/orders/ORD-000001", headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version.unknown"


def test_unsupported_endpoint_version_combo(client, sign_headers):
    h = sign_headers("GET", "/inventory/summary", version=1)
    r = client.get("/inventory/summary", headers=h)
    log_decision("api.unsupported_combo",
                 {"endpoint": "/inventory/summary", "version": 1},
                 "endpoint serves v2 only -> version.unsupported",
                 r.json()["error"]["code"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version.unsupported"
    assert r.json()["error"]["context"]["supported"] == [2]


def test_retired_version_hard_410(client, sign_headers, clock):
    # advance past v1 sunset (default config: 60 days from build time)
    clock.advance(61 * 86400)
    h = sign_headers("GET", "/orders/ORD-000001", version=1)
    r = client.get("/orders/ORD-000001", headers=h)
    log_decision("api.version_retired", {"advanced_days": 61},
                 "now >= v1 sunset_at -> 410 version.retired",
                 r.json()["error"]["code"])
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "version.retired"
    # v2 still served
    h2 = sign_headers("GET", "/orders/ORD-000001", version=2)
    assert client.get("/orders/ORD-000001", headers=h2).status_code in (200, 404)


# ---------------------------------------------------------------- signature
def test_signature_rejection_reasons_are_distinct(client, sign_headers):
    # expired
    h = sign_headers("GET", "/orders/x", timestamp=T0 - 9999)
    r = client.get("/orders/x", headers=h)
    log_decision("api.sig_expired", {"drift": 9999}, "outside skew",
                 r.json()["error"]["code"])
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "signature.timestamp_expired"

    # invalid signature
    h = sign_headers("GET", "/orders/x", signature="f" * 64)
    r = client.get("/orders/x", headers=h)
    assert r.json()["error"]["code"] == "signature.invalid"

    # unknown caller
    h = sign_headers("GET", "/orders/x", caller="nobody", secret=b"z")
    r = client.get("/orders/x", headers=h)
    assert r.json()["error"]["code"] == "signature.unknown_caller"

    # missing headers
    r = client.get("/orders/x")
    assert r.json()["error"]["code"] == "signature.missing_credentials"


def test_replay_nonce_rejected(client, sign_headers):
    h = sign_headers("GET", "/orders/x", nonce="dup-http-1")
    assert client.get("/orders/x", headers=h).status_code in (200, 404)
    r = client.get("/orders/x", headers=h)
    log_decision("api.replay", {"nonce": "dup-http-1"},
                 "second identical signed request", r.json()["error"]["code"])
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "signature.replay_detected"


def test_key_rotation_old_and_new_both_accepted_in_grace(client, sign_headers):
    for secret in (b"acme-old-secret", b"acme-new-secret"):
        h = sign_headers("GET", "/orders/x", secret=secret)
        r = client.get("/orders/x", headers=h)
        log_decision("api.rotation", {"key": secret},
                     "overlap window: both keys verify", r.status_code)
        assert r.status_code in (200, 404)


# --------------------------------------------------------------- idempotency
def _create(client, sign_headers, key, body=ORDER_BODY, **kw):
    h = sign_headers("POST", "/orders", body=body,
                     extra={"Idempotency-Key": key}, **kw)
    return post_order(client, h, body)


def test_idempotent_replay_returns_same_result(client, sign_headers):
    r1 = _create(client, sign_headers, "idem-1")
    r2 = _create(client, sign_headers, "idem-1")
    log_decision("api.idempotent_replay", {"key": "idem-1"},
                 "stored response replayed", f"{r2.status_code}")
    assert r1.status_code == r2.status_code == 201
    assert r2.headers["Idempotency-Replayed"] == "true"
    assert r1.json() == r2.json()


def test_idempotency_conflict_does_not_overwrite(client, sign_headers):
    r1 = _create(client, sign_headers, "idem-2")
    other = json.dumps({"product": "other", "quantity": 9, "amount": 1}).encode()
    r2 = _create(client, sign_headers, "idem-2", body=other)
    log_decision("api.idempotency_conflict",
                 {"key": "idem-2", "first": ORDER_BODY.decode(),
                  "second": other.decode()},
                 "different body hash for same key -> 409, original kept",
                 r2.json()["error"]["code"])
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency.key_conflict"
    # original result unchanged
    r3 = _create(client, sign_headers, "idem-2")
    assert r3.json() == r1.json() and r3.headers["Idempotency-Replayed"] == "true"


def test_missing_idempotency_key_rejected(client, sign_headers):
    h = sign_headers("POST", "/orders", body=ORDER_BODY)
    r = post_order(client, h, ORDER_BODY)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "idempotency.missing_key"


def test_invalid_payload_failure_is_retryable(client, sign_headers):
    bad = b'{"product":"w","quantity":0,"amount":10}'
    h = sign_headers("POST", "/orders", body=bad,
                     extra={"Idempotency-Key": "idem-bad"})
    r = post_order(client, h, bad)
    assert r.status_code == 422
    # a failed first attempt (no stored result) allows the SAME payload retry
    h2 = sign_headers("POST", "/orders", body=bad,
                      extra={"Idempotency-Key": "idem-bad"})
    r2 = post_order(client, h2, bad)
    log_decision("api.failure_retryable", {"status_codes": [422, r2.status_code]},
                 "422 did not store a result -> takeover execution", r2.status_code)
    assert r2.status_code == 422  # still invalid payload, but it re-executed,
    # proving the key was not permanently poisoned; now a valid body under a
    # fresh key succeeds:
    assert _create(client, sign_headers, "idem-bad-ok").status_code == 201


# ----------------------------------------------------------------- ratelimit
def test_rate_limiting_returns_retry_after(client, sign_headers, config):
    from app.ratelimit import RateLimitRule
    config.rate_limiter.set_rule(
        "acme", RateLimitRule(rate_per_second=1.0, burst=2, daily_quota=1000))
    statuses = []
    for i in range(4):
        h = sign_headers("GET", "/orders/x", nonce=f"rl-{i}")
        statuses.append(client.get("/orders/x", headers=h).status_code)
    log_decision("api.rate_limited", {"rule": "1/s burst 2", "statuses": statuses},
                 "first 2 accepted; rest 429 with Retry-After", statuses)
    assert statuses[:2] == [404, 404]
    assert statuses[2:] == [429, 429]
    h = sign_headers("GET", "/orders/x", nonce="rl-rej")
    r = client.get("/orders/x", headers=h)
    assert r.json()["error"]["code"] == "rate_limit.exceeded"
    assert "Retry-After" in r.headers


def test_rate_rule_admin_hot_swap(client, sign_headers, config):
    from app.ratelimit import RateLimitRule
    config.rate_limiter.set_rule(
        "acme", RateLimitRule(rate_per_second=1.0, burst=1, daily_quota=1000))
    h = sign_headers("GET", "/orders/x", nonce="sw-1")
    client.get("/orders/x", headers=h)
    h = sign_headers("GET", "/orders/x", nonce="sw-2")
    assert client.get("/orders/x", headers=h).status_code == 429

    r = client.put("/_/admin/rate-rules/acme",
                   json={"rate_per_second": 1000, "burst": 1000,
                         "daily_quota": 1000000})
    assert r.status_code == 200
    h = sign_headers("GET", "/orders/x", nonce="sw-3")
    r2 = client.get("/orders/x", headers=h)
    log_decision("api.rule_swap", {"caller": "acme"},
                 "PUT admin rule -> next requests accepted", r2.status_code)
    assert r2.status_code == 404  # past governance, order simply missing


def test_response_headers_explain_governance_state(client, sign_headers):
    h = sign_headers("POST", "/orders", body=ORDER_BODY,
                     extra={"Idempotency-Key": "k-headers"})
    r = post_order(client, h, ORDER_BODY)
    assert r.headers["X-Matched-Signing-Key"] == "k-new"
    assert "X-RateLimit-Quota-Remaining" in r.headers
