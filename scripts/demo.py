"""本地一键演示：不依赖外部服务与真实凭据。

运行::

    uv run python scripts/demo.py

会依次打印：版本协商（缺版本/未知/已下线/弃用头）、签名（合法/过期/错误/
轮换旧密钥/重放）、幂等（并发/冲突/回放）、限流（突发/配额/Retry-After）
每一步的输入、判定依据与最终状态。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

# 允许直接以 ``python scripts/demo.py`` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.config import default_governance
from app.governance.ratelimit import RateLimitRule
from app.governance.signing import sign_request
from app.main import create_app

BODY = b'{"item_id":"item-a","qty":2}'


def show(title: str, r) -> None:
    print(f"\n=== {title} ===")
    print(f"HTTP {r.status_code}")
    interesting = {
        k: v
        for k, v in r.headers.items()
        if k.lower().startswith(
            ("x-rate", "retry-after", "deprecation", "sunset", "link", "api-version", "idempotent")
        )
    }
    if interesting:
        print("headers:", json.dumps(interesting, ensure_ascii=False))
    try:
        print("body:", json.dumps(r.json(), ensure_ascii=False)[:300])
    except ValueError:
        print("body:", r.text[:300])


def signed_h(*, version="v2", caller="premium", kid="k-2026-09",
             secret="s3cr3t-current", idem="demo", nonce="n",
             ts=None, path="/api/orders", body=BODY, sig=None):
    ts = ts or str(int(time.time()))
    sig = sig if sig is not None else sign_request(secret, "POST", path, ts, nonce, body)
    return {
        "X-API-Version": version, "X-Caller-Id": caller, "X-Key-Id": kid,
        "X-Timestamp": ts, "X-Nonce": nonce, "X-Signature": sig,
        "X-Idempotency-Key": idem, "Content-Type": "application/json",
    }


def main() -> None:
    app = create_app(default_governance())
    app.state.governance.rate_limiter.update_rules(
        {
            "premium": RateLimitRule("premium", rps_limit=100, quota_limit=1000, quota_window_seconds=3600),
            "demo-tight": RateLimitRule("demo-tight", rps_limit=2, quota_limit=2, quota_window_seconds=3600),
            "demo-quota": RateLimitRule("demo-quota", rps_limit=100, quota_limit=2, quota_window_seconds=3600),
        }
    )
    c = TestClient(app)

    print("########## 1) 版本协商 ##########")
    show("缺版本", c.get("/api/orders/x"))
    show("未知版本 v9", c.get("/api/orders/x", headers={"X-API-Version": "v9", "X-Caller-Id": "premium"}))
    show("已下线 v0(410)", c.get("/api/orders/x", headers={"X-API-Version": "v0", "X-Caller-Id": "premium"}))
    show("弃用期 v1 成功(带 Deprecation/Sunset)", c.post(
        "/api/orders", content=BODY, headers=signed_h(version="v1", idem="v1-demo", nonce="v1-n")))

    print("\n########## 2) 签名 / 密钥轮换 / 重放 ##########")
    show("合法签名(新密钥)", c.post("/api/orders", content=BODY, headers=signed_h(idem="s1", nonce="s1n")))
    show("时间窗过期", c.post("/api/orders", content=BODY,
                            headers=signed_h(idem="s2", nonce="s2n", ts=str(int(time.time()) - 9999))))
    show("签名错误", c.post("/api/orders", content=BODY,
                          headers=signed_h(idem="s3", nonce="s3n", sig="0" * 64)))
    show("轮换宽限期内旧密钥仍有效", c.post("/api/orders", content=BODY, headers=signed_h(
        kid="k-2025-12", secret="s3cr3t-previous", idem="s4", nonce="s4n")))
    show("已退役老密钥被拒", c.post("/api/orders", content=BODY, headers=signed_h(
        kid="k-2024-06", secret="s3cr3t-retired", idem="s5", nonce="s5n")))
    rh = signed_h(idem="s6", nonce="s6n")
    c.post("/api/orders", content=BODY, headers=rh)
    show("nonce 重放(409)", c.post("/api/orders", content=BODY, headers=rh))

    print("\n########## 3) 幂等：并发 / 回放 / 冲突 ##########")
    barrier = threading.Barrier(6)
    results: list = []

    def worker(i):
        barrier.wait()
        r = c.post("/api/orders", content=BODY, headers=signed_h(idem="hot", nonce=f"hot-{i}"))
        results.append((r.status_code, r.headers.get("Idempotent-Replay", ""), r.json().get("order_id")))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("6 并发同幂等键结果:", results)
    print("唯一生效 order_id:", {r[2] for r in results})

    ba = b'{"item_id":"item-a","qty":1}'
    bb = b'{"item_id":"item-a","qty":2}'
    c.post("/api/orders", content=ba, headers=signed_h(idem="cf", nonce="cf1", body=ba))
    show("同键不同内容 -> 冲突(422)",
         c.post("/api/orders", content=bb, headers=signed_h(idem="cf", nonce="cf2", body=bb)))

    print("\n########## 4) 限流 / 配额 ##########")
    hs = {"X-API-Version": "v2", "X-Caller-Id": "demo-tight"}
    for i in range(3):
        show(f"GET 第 {i + 1} 次", c.get("/api/orders/x", headers=hs))
    # 配额用独立调用方：秒级额度充足，但长周期配额只有 2
    qs = [c.post("/api/orders", content=BODY,
                 headers=signed_h(caller="demo-quota", idem=f"q-{i}", nonce=f"q-n-{i}")) for i in range(3)]
    show("写请求配额耗尽(quota_exhausted)", qs[-1])


if __name__ == "__main__":
    main()
