# 本地验证指南

全部验证均可离线完成：只依赖仓库声明的 `fastapi[standard]`
（随附 `pytest`、`httpx`），不需要任何外部服务或真实凭据。

## 1. 环境准备

```bash
# 仓库自带 uv 锁文件；首次运行会自动创建 .venv
uv sync        # 可选
```

## 2. 运行全部单元测试

```bash
uv run pytest
```

测试用例组织：

| 文件 | 覆盖内容 |
|------|---------|
| `tests/test_versioning.py` | 缺版本 / 未知版本 / 不支持组合 / 弃用边界 / 下线 410 |
| `tests/test_signing.py` | 签名、时间窗、密钥轮换重叠期与宽限边界、退役、重放、篡改 |
| `tests/test_idempotency.py` | 8 线程并发同键只执行一次、回放一致、内容冲突不覆盖 |
| `tests/test_ratelimit.py` | 20 线程突发计数精确、拒绝不占额度、配额区分、规则热更 |
| `tests/test_api_version_semantics.py` | 同一订单 v1/v2 字段不丢不改、v2 只做加法、弃用信号差异 |
| `tests/test_http_concurrency.py` | HTTP 层并发幂等、轮换重叠、nonce 重放、下线边界、内容冲突 |
| `tests/test_http_ratelimit_boundary.py` | 被限流不影响已接受请求与幂等回放、长周期配额拒绝 |
| `tests/test_http_smoke.py` | 各治理分支的端到端冒烟 |

想看治理判定日志（输入 / 判定依据 / 最终状态）：

```bash
uv run pytest -s -o log_cli=true --log-cli-level=INFO
```

日志格式（每个治理阶段都会打印，拒绝时额外打印 code 与依据）：

```
case=http-concurrent-idempotency | input={'threads': 10, ...}
  | basis={'statuses': [...], 'distinct_order_ids': [...], 'replays': 9}
  | status=single_effect=True
governance stage=ratelimit status=ACCEPTED basis={"caller": "premium", ...}
governance stage=signature status=REJECTED basis={"code": "signature_mismatch", ...}
```

单测内统一通过 `tests/govlog.gov_log(case, inputs, basis, status)` 打印
"输入 → 判定依据 → 最终状态"三段信息。

## 3. 一键演示脚本（无需起服务）

```bash
uv run python scripts/demo.py
```

脚本使用 FastAPI `TestClient` 在进程内顺序演示四组场景，打印每次请求的
HTTP 状态、关键响应头与响应体：

1. 版本协商：缺版本(400)、未知(404)、已下线(410)、弃用期 v1 带 Deprecation/Sunset；
2. 签名：合法、时间窗过期、签名错误、轮换宽限期旧密钥、退役密钥、nonce 重放；
3. 幂等：6 并发同键只产生一个订单、内容冲突 422、回放头；
4. 限流：秒级突发 429（Retry-After）、长周期配额 429（scope=quota）。

## 4. 启动真实 HTTP 服务手工验证

```bash
uv run python main.py
# 默认监听 127.0.0.1:8000，可访问 /docs 查看 OpenAPI
```

最小合法写请求示例（Python）：

```python
import time, requests
from app.governance.signing import sign_request

body = b'{"item_id":"item-a","qty":1}'
ts = str(int(time.time()))
sig = sign_request("s3cr3t-current", "POST", "/api/orders", ts, "client-nonce-1", body)
r = requests.post("http://127.0.0.1:8000/api/orders", data=body, headers={
    "Content-Type": "application/json",
    "X-API-Version": "v2",
    "X-Caller-Id": "premium",
    "X-Key-Id": "k-2026-09",
    "X-Timestamp": ts,
    "X-Nonce": "client-nonce-1",
    "X-Signature": sig,
    "X-Idempotency-Key": "client-key-1",
})
print(r.status_code, r.headers, r.json())
```

只读请求只需版本头：

```bash
curl -i http://127.0.0.1:8000/api/orders/order-000001 -H 'X-API-Version: v2'
```

## 5. 内置演示密钥（仅用于本地验证，非真实凭据）

| key id | secret | 状态 |
|--------|--------|------|
| `k-2026-09` | `s3cr3t-current` | active |
| `k-2025-12` | `s3cr3t-previous` | 轮换宽限 grace（启动后 600 秒内有效） |
| `k-2024-06` | `s3cr3t-retired` | 已退役 retired |
