# 本地验证方法

所有验证均可离线完成：不监听真实端口也可以（测试与演示脚本使用
FastAPI `TestClient` / httpx `ASGITransport` 走进程内 ASGI），
不依赖外部服务，密钥均为演示常量。

## 1. 安装与跑测试

```bash
uv sync          # 安装 fastapi + dev 组（pytest、httpx）
uv run pytest                 # 运行全部用例
uv run pytest -s              # 打印每个用例的“输入 / 判定依据 / 最终状态”
uv run pytest -k concurrency  # 只看并发场景
uv run pytest -k boundary     # 只看密钥轮换/版本下线边界
```

测试日志格式（来自 `tests/conftest.log_decision`）：

```
case=concurrency.duplicate_single_effect
  | input={"idempotency_key": "concurrent-key-1", "threads": 20}
  | basis=exactly one execute; identical concurrent retries block and replay
  | status=codes={201: 20}, unique_order_ids={'ORD-000001'}, replayed=19
```

## 2. 覆盖的关键场景

| 文件 | 覆盖点 |
|------|--------|
| `tests/test_versioning.py` | 默认版本、弃用头、unknown/unsupported/retired、sunset 前后逐秒边界 |
| `tests/test_signature.py` | 时间窗过期、签名错误、篡改体、重放、未知调用方/缺凭证；轮换重叠期内新旧双密钥、边界（含闭区间）、TTL 后 nonce 清理 |
| `tests/test_idempotency.py` | 16 线程同键同体只执行一次且结果一致；不同载荷 409 不覆盖；失败可接管重试；异常释放槽位；键间独立 |
| `tests/test_ratelimit.py` | 50 线程突发接受数精确等于 burst；refill；配额与限流区分；UTC 午夜重置；调用方隔离；规则热替换/整体替换 |
| `tests/test_api_governance.py` | HTTP 全链路：版本、签名各拒绝码、幂等回放/冲突/缺键/失败重试、限流 Retry-After、管理接口热更新、响应头 |
| `tests/test_concurrency.py` | 真实 ASGI：20 线程重复提交只落 1 单、并发冲突只有首单生效、突发 429 不影响已接受请求、被限流调用方不波及其它调用方 |
| `tests/test_boundary_scenarios.py` | 轮换重叠区间逐采样新旧密钥都通过 / 越过边界旧密钥失效；版本 sunset 前 1 秒、当刻、后 1 秒 |

## 3. 一键演示（打印每条请求的接受/拒绝判定）

```bash
uv run python scripts/demo_requests.py
```

输出示例（节选）：

```
OK same key different payload -> conflict   http=409 code=idempotency.key_conflict
OK v1 on v2-only endpoint -> unsupported    http=400 code=version.unsupported
OK expired timestamp                        http=401 code=signature.timestamp_expired
OK replayed nonce                           http=401 code=signature.replay_detected
   burst under tight rule -> statuses=[404, 404, 429, 429, 429] ...
OK v1 past sunset -> retired                http=410 code=version.retired
```

## 4. 手工构造签名请求（可选，真实 HTTP）

```bash
uv run python main.py        # 启动 uvicorn: http://127.0.0.1:8000
```

签名（Python 片段）：

```python
import hashlib, hmac, time
secret = b"acme-new-secret"          # 轮换重叠期内 acme-old-secret 同样有效
ts = str(int(time.time()))
nonce = "unique-random-string"
body = b'{"product":"widget","quantity":1,"amount":100}'
canonical = b"\n".join([
    b"POST", b"/orders", ts.encode(), nonce.encode(),
    hashlib.sha256(body).hexdigest().encode(),
])
sig = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
```

```bash
curl -sS http://127.0.0.1:8000/orders \
  -H "Content-Type: application/json" \
  -H "X-Caller-Id: acme" -H "X-Timestamp: $TS" -H "X-Nonce: $NONCE" \
  -H "X-Signature: $SIG" -H "Idempotency-Key: demo-key-1" \
  -H "X-Api-Version: 2" \
  --data '{"product":"widget","quantity":1,"amount":100}'
```

查看版本生命周期、查看/热改规则：

```bash
curl -sS http://127.0.0.1:8000/_/versions
curl -sS http://127.0.0.1:8000/_/admin/rate-rules
curl -sS -X PUT http://127.0.0.1:8000/_/admin/rate-rules/acme \
  -H 'Content-Type: application/json' \
  -d '{"rate_per_second":100,"burst":200,"daily_quota":1000000}'
```

## 5. 可注入时钟（确定性验证时间相关行为）

所有时间判定都依赖 `app.clock.Clock` 协议。生产用 `SystemClock`，
测试用 `FakeClock`（`advance(seconds)` 推进），因此时间窗过期、密钥轮换
边界、UTC 配额重置、版本 sunset 等场景无需真实等待即可逐秒验证。
