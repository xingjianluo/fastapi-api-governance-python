# 写入请求的来源签名、密钥轮换与幂等

## 1. 签名方案

对所有写入方法（`POST / PUT / PATCH / DELETE`），客户端需要携带：

| 请求头 | 含义 |
|--------|------|
| `X-Key-Id` | 使用的密钥 id（服务端据此选密钥） |
| `X-Timestamp` | Unix 秒级时间戳（字符串） |
| `X-Nonce` | 一次性随机串，时间窗内不可复用 |
| `X-Signature` | HMAC-SHA256 的十六进制摘要 |
| `X-Idempotency-Key` | 幂等键（写入建议必带） |
| `X-Caller-Id` | 调用方标识，用于限流配额 |

### 签名基串（canonical string）

行之间用 `\n` 连接：

```
<METHOD>
< path >
<X-Timestamp>
<X-Nonce>
<sha256(body) 十六进制摘要>
```

示例（Python）：

```python
import hashlib, hmac

def sign(secret: str, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    base = "\n".join([method.upper(), path, ts, nonce,
                      hashlib.sha256(body).hexdigest()]).encode()
    return hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
```

仓库内可直接使用 `app.governance.signing.sign_request`，演示见
`scripts/demo.py` 与 `tests/test_http_concurrency.py`。

## 2. 时间窗校验

`SIGNATURE_SKEW_SECONDS = 300`（`app/config.py`，可配置）。
服务端时间与 `X-Timestamp` 的绝对差超过该窗口即拒绝：

- HTTP 401，`error.code = signature_timestamp_expired`
- `details` 给出 `timestamp / server_time / drift_seconds / window_seconds`，
  拒绝原因可解释。
- 时间戳无法解析时使用同一错误码。

## 3. 密钥轮换规则与宽限边界

每个密钥有三种状态：

| 状态 | 含义 | 验签结果 |
|------|------|---------|
| `active` | 当前签名密钥 | 通过 |
| `grace` | 上一代密钥，在 `grace_until` 之前仍可验签 | `now <= grace_until` 通过；之后拒绝 |
| `retired` | 已过宽限期，彻底停用 | 一律拒绝 |

轮换流程：

1. 部署新密钥并标记为 `active`；
2. 同时把旧密钥改为 `grace`，设置绝对截止时间 `grace_until`
   （演示配置为当前时间 + 600 秒，生产应设置为真实重叠期）；
3. 在重叠期内，**新旧密钥同时生效**，客户端可平滑切换；
4. 宽限期结束后，旧密钥请求被明确拒绝为
   HTTP 401 `signature_key_retired`（`details` 带回 `grace_until`）。

宽限是**明确边界**而非模糊过渡：`now == grace_until` 仍允许，
`now > grace_until` 立即拒绝。测试：
`tests/test_signing.py::test_grace_key_boundary_then_retired`。

## 4. 可区分的拒绝原因

| 场景 | HTTP | error.code |
|------|------|-----------|
| 缺少任一签名头 | 401 | `signature_credentials_missing` |
| `X-Key-Id` 未知 | 401 | `signature_key_unknown` |
| 密钥已过轮换宽限期 / 已退役 | 401 | `signature_key_retired` |
| 时间戳缺失、非法或超出时间窗 | 401 | `signature_timestamp_expired` |
| 摘要不匹配（密钥有效、时间窗有效，但内容对不上） | 401 | `signature_mismatch` |
| 同一 nonce 在时间窗内重复出现（重放） | 409 | `signature_replay` |

`signature_mismatch` 与 `signature_timestamp_expired` 被刻意区分：
前者是签名/内容错误，后者是时间窗问题，客户端的补救动作不同。
nonce 重放在签名比对通过**之后**才判重，因此坏签名请求不会污染去重器。

## 5. 幂等语义

- 幂等键通过 `X-Idempotency-Key` 提供；
- 请求"指纹" = `sha256(版本 | content-type | 原始 body)`；
- 同一幂等键 + 同一指纹：第一次真正执行，之后的并发/串行重试都返回
  **首次保存的同一份结果**，响应头带 `Idempotent-Replay: true`；
- 同一幂等键 + **不同指纹**：HTTP 422 `idempotency_conflict`，
  明确报错，**绝不覆盖**已有结果；
- 首个请求仍在执行时的并发重试：短暂等待其落结果后回放；超时仍未完成
  则返回 HTTP 409 `idempotency_in_progress`，提示稍后重试。

并发安全性由 `IdempotencyStore` 单把锁的"读状态→判定→占位"原子操作
保证：N 个并发相同请求中只有 1 个拿到 `acquired`。
见 `tests/test_idempotency.py` 与
`tests/test_http_concurrency.py::test_http_concurrent_idempotent_submission_single_effect`。

## 6. 治理管线的执行顺序

```
限流/配额  ->  版本协商  ->  来源签名  ->  幂等占用  ->  业务执行
（写请求）；只读请求只做前两步。
```

限流放在最前：被限流的请求最便宜地被拒绝，且不会进入签名/幂等等任何
有状态环节。
