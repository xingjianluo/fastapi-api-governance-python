"""端到端冒烟：验证治理管线在真实 HTTP 请求下生效。"""

import time

from fastapi.testclient import TestClient

from app.config import default_governance
from app.governance.signing import sign_request
from app.main import create_app


def make_client():
    gov = default_governance()
    app = create_app(gov)
    return TestClient(app), gov


def signed_headers(body: bytes, *, version="v2", caller="premium", key="k-2026-09", secret="s3cr3t-current", idem="idem-1", path="/api/orders", ts=None, nonce="nonce-1", sig=None):
    ts = ts if ts is not None else str(int(time.time()))
    sig = sig if sig is not None else sign_request(secret, "POST", path, ts, nonce, body)
    return {
        "X-API-Version": version,
        "X-Caller-Id": caller,
        "X-Key-Id": key,
        "X-Timestamp": ts,
        "X-Nonce": nonce,
        "X-Signature": sig,
        "X-Idempotency-Key": idem,
    }


def test_health_open():
    client, _ = make_client()
    r = client.get("/health")
    assert r.status_code == 200


def test_missing_version_rejected_http():
    client, _ = make_client()
    r = client.get("/api/orders/order-000001")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version_missing"


def test_unknown_version_http():
    client, _ = make_client()
    r = client.get("/api/orders/x", headers={"X-API-Version": "v9"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "version_unknown"


def test_sunset_version_http():
    client, _ = make_client()
    r = client.get("/api/orders/x", headers={"X-API-Version": "v0"})
    assert r.status_code == 410
    assert r.json()["error"]["code"] == "version_sunset"


def test_deprecated_headers_on_v1():
    client, _ = make_client()
    body = b'{"item_id":"item-a","qty":2}'
    h = signed_headers(body, version="v1", idem="d1", nonce="dn1")
    r = client.post("/api/orders", content=body, headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 201, r.text
    assert r.headers["Deprecation"]
    assert r.headers["Sunset"]
    assert "successor-version" in r.headers["Link"]


def test_v2_success_and_idempotent_replay():
    client, _ = make_client()
    body = b'{"item_id":"item-b","qty":1}'
    h = signed_headers(body, version="v2", idem="ok-1", nonce="on1")
    r1 = client.post("/api/orders", content=body, headers={**h, "Content-Type": "application/json"})
    assert r1.status_code == 201
    first = r1.json()
    # 重试：同样的键与内容（注意 nonce 要换新，否则命中重放；用新 nonce 重签）
    h2 = signed_headers(body, version="v2", idem="ok-1", nonce="on2")
    r2 = client.post("/api/orders", content=body, headers={**h2, "Content-Type": "application/json"})
    assert r2.status_code == 201
    assert r2.headers.get("Idempotent-Replay") == "true"
    assert r2.json() == first  # 结果一致，没有生成第二个订单


def test_unsupported_combo_batch_v1():
    client, _ = make_client()
    body = b"{}"
    h = signed_headers(body, version="v1", idem="b1", nonce="bn1", path="/api/orders/batch-confirm")
    r = client.post("/api/orders/batch-confirm", content=body, headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "version_unsupported_combo"


def test_signature_mismatch_http():
    client, _ = make_client()
    body = b'{"item_id":"item-a","qty":1}'
    h = signed_headers(body, sig="0" * 64)
    r = client.post("/api/orders", content=body, headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "signature_mismatch"


def test_rate_limit_http_retry_after():
    client, _ = make_client()
    # default 规则 rps=2
    h1 = {"X-API-Version": "v2", "X-Caller-Id": "anon-test"}
    client.get("/api/orders/x", headers=h1)
    client.get("/api/orders/x", headers=h1)
    r3 = client.get("/api/orders/x", headers=h1)
    assert r3.status_code == 429
    assert r3.json()["error"]["code"] == "burst_rate_limited"
    assert r3.headers["Retry-After"]
