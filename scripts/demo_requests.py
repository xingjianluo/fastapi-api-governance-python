"""Offline demo: exercise every governance acceptance/rejection path.

Run with:  uv run python scripts/demo_requests.py
Uses an in-process ASGI transport (httpx ASGITransport) - no network, no
real credentials, no external services.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.clock import FakeClock
from app.config import build_default_config
from app.main import create_app
from app.signature import sign

T0 = 2_000_000.0
clock = FakeClock(T0)
config = build_default_config(clock)
app = create_app(config)

_counter = iter(range(10_000))


def headers(method, path, *, caller="acme", secret=b"acme-new-secret",
            body=b"", version=None, nonce=None, timestamp=None,
            idem=None, mutate=None):
    ts = str(clock.now() if timestamp is None else timestamp)
    nonce = nonce or f"demo-{next(_counter)}"
    sig = sign(secret, method, path, ts, nonce, body)
    if mutate == "sig":
        sig = "0" * 64
    h = {"X-Caller-Id": caller, "X-Timestamp": ts, "X-Nonce": nonce,
         "X-Signature": sig}
    if version is not None:
        h["X-Api-Version"] = str(version)
    if idem is not None:
        h["Idempotency-Key"] = idem
    return h


def show(title, resp, expect):
    code = ""
    try:
        code = resp.json().get("error", {}).get("code", "")
    except Exception:
        pass
    flag = "OK " if (code == expect or (not code and str(resp.status_code) == expect)) else "?? "
    print(f"{flag}{title:42} http={resp.status_code} code={code or '-'}")


async def main():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://local") as c:
        body = json.dumps({"product": "widget", "quantity": 2,
                           "amount": 1999}).encode()

        r = await c.get("/_/versions"); show("version meta (unsigned)", r, "200")
        r = await c.post("/orders", content=body,
                         headers=headers("POST", "/orders", body=body, idem="d1"))
        show("create v2 (default) accepted", r, "201")
        r = await c.post("/orders", content=body,
                         headers=headers("POST", "/orders", body=body, idem="d1"))
        show("same idempotency key -> replay", r, "201")
        other = json.dumps({"product": "x", "quantity": 1, "amount": 1}).encode()
        r = await c.post("/orders", content=other,
                         headers=headers("POST", "/orders", body=other, idem="d1"))
        show("same key different payload -> conflict", r, "idempotency.key_conflict")

        r = await c.post("/orders", content=body,
                         headers=headers("POST", "/orders", body=body,
                                         version=1, idem="d2"))
        show("v1 deprecated (see Sunset header)", r, "201")
        r = await c.get("/inventory/summary",
                        headers=headers("GET", "/inventory/summary", version=1))
        show("v1 on v2-only endpoint -> unsupported", r, "version.unsupported")
        r = await c.get("/orders/x",
                        headers=headers("GET", "/orders/x", version=9))
        show("unknown version", r, "version.unknown")

        r = await c.get("/orders/x", headers=headers("GET", "/orders/x",
                                                     timestamp=T0 - 9999))
        show("expired timestamp", r, "signature.timestamp_expired")
        r = await c.get("/orders/x",
                        headers=headers("GET", "/orders/x", mutate="sig"))
        show("bad signature", r, "signature.invalid")
        r = await c.get("/orders/x",
                        headers=headers("GET", "/orders/x", caller="ghost",
                                        secret=b"z"))
        show("unknown caller", r, "signature.unknown_caller")
        h = headers("GET", "/orders/x", nonce="dup")
        await c.get("/orders/x", headers=h)
        r = await c.get("/orders/x", headers=h)
        show("replayed nonce", r, "signature.replay_detected")
        r = await c.get("/orders/x",
                        headers=headers("GET", "/orders/x",
                                        secret=b"acme-old-secret"))
        show("old key valid in rotation overlap", r, "404")

        # rate limiting: tighten globex then burst
        await c.put("/_/admin/rate-rules/globex",
                    json={"rate_per_second": 1, "burst": 2, "daily_quota": 10})
        codes = []
        for i in range(5):
            rr = await c.get("/orders/x", headers=headers(
                "GET", "/orders/x", caller="globex", secret=b"globex-secret",
                nonce=f"b{i}"))
            codes.append(rr.status_code)
        print(f"   burst under tight rule -> statuses={codes} "
              f"(expect two pass-through then 429s)")

        # v1 retirement past sunset
        clock.advance(61 * 86400)
        r = await c.get("/orders/x",
                        headers=headers("GET", "/orders/x", version=1))
        show("v1 past sunset -> retired", r, "version.retired")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
