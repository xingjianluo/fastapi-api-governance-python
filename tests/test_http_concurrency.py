"""端到端边界：并发重复提交、密钥轮换重叠期、版本下线、内容冲突。"""

import threading
import time

from fastapi.testclient import TestClient

from app.config import default_governance
from app.governance.signing import sign_request
from app.main import create_app
from tests.govlog import gov_log

BODY = b'{"item_id":"item-c","qty":5}'


def make_signed_headers(*, version="v2", caller="premium", kid="k-2026-09",
                        secret="s3cr3t-current", idem="K", nonce="N",
                        ts=None, path="/api/orders", body=BODY, sig=None):
    ts = ts if ts is not None else str(int(time.time()))
    sig = sig if sig is not None else sign_request(secret, "POST", path, ts, nonce, body)
    return {
        "X-API-Version": version, "X-Caller-Id": caller, "X-Key-Id": kid,
        "X-Timestamp": ts, "X-Nonce": nonce, "X-Signature": sig,
        "X-Idempotency-Key": idem, "Content-Type": "application/json",
    }


def fresh_app():
    app = create_app(default_governance())
    # 幂等并发测试需要隔离限流维度：给专用调用方一个足够大的秒级额度
    from app.governance.ratelimit import RateLimitRule

    app.state.governance.rate_limiter.update_rules(
        {
            "premium": RateLimitRule("premium", rps_limit=5, quota_limit=1000, quota_window_seconds=3600),
            "idem-load": RateLimitRule("idem-load", rps_limit=100, quota_limit=10_000, quota_window_seconds=3600),
            "basic": RateLimitRule("basic", rps_limit=3, quota_limit=50, quota_window_seconds=3600),
        }
    )
    return TestClient(app)


def test_http_concurrent_idempotent_submission_single_effect():
    c = fresh_app()
    n = 10
    barrier = threading.Barrier(n)
    statuses: list[int] = []
    bodies: list[dict] = []
    replay_flags: list[str] = []
    lock = threading.Lock()

    def worker(i: int):
        h = make_signed_headers(caller="idem-load", idem="concurrent-K", nonce=f"c-nonce-{i}")
        barrier.wait()
        r = c.post("/api/orders", content=BODY, headers=h)
        with lock:
            statuses.append(r.status_code)
            if r.status_code == 201:
                bodies.append(r.json())
            replay_flags.append(r.headers.get("Idempotent-Replay", ""))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    order_ids = {b.get("order_id") for b in bodies}
    replays = sum(1 for f in replay_flags if f == "true")
    gov_log(
        "http-concurrent-idempotency",
        {"threads": n, "idempotency_key": "concurrent-K"},
        {"statuses": sorted(statuses), "distinct_order_ids": sorted(order_ids), "replays": replays},
        f"single_effect={len(order_ids) == 1}",
    )
    assert all(s == 201 for s in statuses)
    assert len(order_ids) == 1           # 只真正创建了一个订单
    assert replays == n - 1              # 其余全部拿到同一份回放
    assert len({b["amount_cents"] for b in bodies}) == 1


def test_http_idempotency_content_conflict_rejected():
    c = fresh_app()
    body_a = b'{"item_id":"item-a","qty":1}'
    body_b = b'{"item_id":"item-a","qty":2}'
    h1 = make_signed_headers(idem="same-key", nonce="cf-1", body=body_a)
    r1 = c.post("/api/orders", content=body_a, headers=h1)
    h2 = make_signed_headers(idem="same-key", nonce="cf-2", body=body_b)
    r2 = c.post("/api/orders", content=body_b, headers=h2)
    gov_log(
        "http-idempotency-conflict",
        {"key": "same-key", "body_a": body_a.decode(), "body_b": body_b.decode()},
        {"first_status": r1.status_code, "second_code": r2.json()["error"]["code"]},
        "conflict rejected, first kept",
    )
    assert r1.status_code == 201
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "idempotency_conflict"


def test_http_key_rotation_overlap_both_keys_work():
    c = fresh_app()
    # 新密钥
    h_new = make_signed_headers(kid="k-2026-09", secret="s3cr3t-current", idem="rot-1", nonce="rot-n1")
    r_new = c.post("/api/orders", content=BODY, headers=h_new)
    # 旧密钥：处于轮换宽限重叠期
    h_old = make_signed_headers(kid="k-2025-12", secret="s3cr3t-previous", idem="rot-2", nonce="rot-n2")
    r_old = c.post("/api/orders", content=BODY, headers=h_old)
    # 已退役老密钥
    h_dead = make_signed_headers(kid="k-2024-06", secret="s3cr3t-retired", idem="rot-3", nonce="rot-n3")
    r_dead = c.post("/api/orders", content=BODY, headers=h_dead)
    gov_log(
        "http-key-rotation",
        {"kids": ["k-2026-09(active)", "k-2025-12(grace)", "k-2024-06(retired)"]},
        {
            "active_status": r_new.status_code,
            "grace_status": r_old.status_code,
            "retired_code": r_dead.json()["error"]["code"],
        },
        "new+grace accepted, retired rejected",
    )
    assert r_new.status_code == 201
    assert r_old.status_code == 201
    assert r_dead.status_code == 401
    assert r_dead.json()["error"]["code"] == "signature_key_retired"


def test_http_replay_nonce_rejected_distinct():
    c = fresh_app()
    h = make_signed_headers(idem="rp-1", nonce="replayed-nonce")
    r1 = c.post("/api/orders", content=BODY, headers=h)
    r2 = c.post("/api/orders", content=BODY, headers=h)  # 完全相同的头 -> nonce 重放
    gov_log(
        "http-nonce-replay",
        {"nonce": "replayed-nonce"},
        {"first": r1.status_code, "second_code": r2.json()["error"]["code"]},
        "replay rejected distinct",
    )
    assert r1.status_code == 201
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "signature_replay"


def test_http_sunset_version_boundary():
    """v0 已过下线日：410 + version_sunset，且响应体说明后继版本。"""
    c = fresh_app()
    r = c.get("/api/orders/whatever", headers={"X-API-Version": "v0", "X-Caller-Id": "premium"})
    gov_log(
        "http-version-sunset",
        {"version": "v0"},
        {"status_code": r.status_code, "code": r.json()["error"]["code"],
         "successor": r.json()["error"]["details"]["successor"]},
        "REJECTED sunset",
    )
    assert r.status_code == 410
    assert r.json()["error"]["details"]["successor"] == "v1"


def test_http_timestamp_expired_distinct_from_bad_signature():
    c = fresh_app()
    old_ts = str(int(time.time()) - 10_000)
    h = make_signed_headers(idem="ts-1", nonce="ts-n1", ts=old_ts)
    r = c.post("/api/orders", content=BODY, headers=h)
    gov_log(
        "http-timestamp-window",
        {"skew_seconds": 10_000},
        {"code": r.json()["error"]["code"], "window": r.json()["error"]["details"]["window_seconds"]},
        "REJECTED timestamp",
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "signature_timestamp_expired"
