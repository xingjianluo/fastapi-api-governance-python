"""HTTP 层限流边界：被限流不影响已接受请求；规则热更后行为可预期。"""

import time

from fastapi.testclient import TestClient

from app.config import default_governance
from app.governance.ratelimit import RateLimitRule
from app.governance.signing import sign_request
from app.main import create_app
from tests.govlog import gov_log

BODY = b'{"item_id":"item-a","qty":1}'


def signed(c, *, caller, idem, nonce, ts=None):
    ts = ts or str(int(time.time()))
    sig = sign_request("s3cr3t-current", "POST", "/api/orders", ts, nonce, BODY)
    return {
        "X-API-Version": "v2", "X-Caller-Id": caller, "X-Key-Id": "k-2026-09",
        "X-Timestamp": ts, "X-Nonce": nonce, "X-Signature": sig,
        "X-Idempotency-Key": idem, "Content-Type": "application/json",
    }


def test_rejected_does_not_affect_accepted_then_replay():
    app = create_app(default_governance())
    app.state.governance.rate_limiter.update_rules(
        {"tight": RateLimitRule("tight", rps_limit=1, quota_limit=100, quota_window_seconds=3600)}
    )
    c = TestClient(app)

    h1 = signed(c, caller="tight", idem="K-1", nonce="n-1")
    r1 = c.post("/api/orders", content=BODY, headers=h1)
    assert r1.status_code == 201
    first = r1.json()

    # 同一秒内第二个请求被突发限流（携带 Retry-After）
    h2 = signed(c, caller="tight", idem="K-2", nonce="n-2")
    r2 = c.post("/api/orders", content=BODY, headers=h2)
    assert r2.status_code == 429
    assert r2.json()["error"]["code"] == "burst_rate_limited"
    assert int(r2.headers["Retry-After"]) >= 1

    # 下一秒重试 K-1（合法新签名）：应原样回放首次结果，未被限流污染
    time.sleep(1.05)
    h3 = signed(c, caller="tight", idem="K-1", nonce="n-3")
    r3 = c.post("/api/orders", content=BODY, headers=h3)
    gov_log(
        "limit-then-replay",
        {"first": 201, "rejected": 429, "retry_next_second": 201},
        {"replayed_order": r3.json().get("order_id"), "first_order": first["order_id"]},
        "accepted request unaffected by rejections",
    )
    assert r3.status_code == 201
    assert r3.headers.get("Idempotent-Replay") == "true"
    assert r3.json() == first


def test_quota_exhaustion_distinct_code_over_long_window():
    app = create_app(default_governance())
    app.state.governance.rate_limiter.update_rules(
        {"shortq": RateLimitRule("shortq", rps_limit=100, quota_limit=2, quota_window_seconds=3600)}
    )
    c = TestClient(app)
    codes = []
    for i in range(3):
        h = signed(c, caller="shortq", idem=f"Q-{i}", nonce=f"qn-{i}")
        r = c.post("/api/orders", content=BODY, headers=h)
        codes.append(r.status_code)
        last = r
    body = last.json()
    gov_log(
        "quota-vs-burst",
        {"quota_limit": 2, "rps_limit": 100},
        {"statuses": codes, "third_code": body["error"]["code"], "scope": body["error"]["details"]["scope"]},
        "quota rejection distinct",
    )
    assert codes == [201, 201, 429]
    assert body["error"]["code"] == "quota_exhausted"
    assert body["error"]["details"]["scope"] == "quota"
