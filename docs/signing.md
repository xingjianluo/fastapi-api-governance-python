# 来源签名、时间窗与密钥轮换

所有受治理接口（读/写）都必须携带签名头。签名采用 HMAC-SHA256，仅依赖
Python 标准库 `hmac` / `hashlib`，密钥为本地演示常量，无需真实凭据。

## 请求头

| 头 | 说明 |
|----|------|
| `X-Caller-Id` | 调用方标识（如 `acme`、`globex`） |
| `X-Timestamp` | 请求构造时的 Unix 秒（允许小数） |
| `X-Nonce` | 一次性随机串，参与签名并用于防重放 |
| `X-Signature` | 对规范化字符串计算的 HMAC-SHA256 十六进制摘要 |
| `Idempotency-Key` | （仅写入）幂等键 |

## 规范化字符串

按如下顺序、以 `\n` 拼接（精确字节）：

```
<METHOD 大写>
<路径，不含 query>
<X-Timestamp 原样字符串>
<X-Nonce>
<请求体原始字节的 SHA256 十六进制摘要>
```

空体（GET）的摘要即为 `sha256(b"")`。参考实现见 `app/signature.py` 的
`build_canonical_string` / `sign`。因为摘要覆盖的是**原始请求体字节**，
任何篡改（改字段、改金额）都会导致验签失败。

## 时间窗

服务端要求 `abs(服务端当前时间 - X-Timestamp) <= skew_seconds`，默认
**±300 秒**。超出窗口返回：

- HTTP 401，`error.code = signature.timestamp_expired`，
  `context` 内含实测漂移秒数与窗口大小。

## 密钥轮换与宽限边界

每个调用方可持有多个密钥，每个密钥带**显式、闭区间**有效期：

```
not_valid_before <= timestamp <= not_valid_after
```

- 轮换时先注册新密钥（`not_valid_before` 设为“现在 - 宽限”），再登记旧密钥的
  `not_valid_after`（默认“现在 + 10 分钟”，见 `ROTATION_GRACE_SECONDS`）；
- **重叠（宽限）期内新旧密钥同时生效**，客户端可在任意时刻切换；
- 边界明确且逐秒被测试覆盖：`timestamp == not_valid_after` 时旧密钥仍可用
  （闭区间），超过 1 秒则该密钥不再是候选。

> 时间窗与密钥有效期是两道独立的判定：先用时间窗排除过期/未来请求，再在
> “该时间戳处于有效期内”的候选密钥中逐一用 `hmac.compare_digest` 比对。

## 拒绝原因两两可区分

| 场景 | HTTP | 错误码 |
|------|------|--------|
| 缺少任一签名头 | 401 | `signature.missing_credentials` |
| `X-Caller-Id` 未注册 | 401 | `signature.unknown_caller` |
| 时间戳超出 ±300s 窗口或无法解析 | 401 | `signature.timestamp_expired` |
| 时间窗内但无有效密钥 / MAC 不匹配 / 体被篡改 | 401 | `signature.invalid` |
| 同 `(caller, nonce)` 在 TTL 内再次出现 | 401 | `signature.replay_detected` |

检查顺序即上表顺序，因此“过期”“签错”“重放”永远不会互相混淆。

## 防重放

- 以 `(caller_id, nonce)` 为键，记录到带 TTL 的内存缓存（`ReplayCache`）；
- TTL 等于时间窗（300s）：超过时间窗的旧 nonce 自动清理，之后它若再出现，
  会先因时间窗被拒为 `timestamp_expired`，而不会误报为重放；
- 不同调用方使用相同 nonce 互不影响；
- 缓存的“检查并登记”在单把锁内完成，并发的相同 nonce 只有第一个被接受。

## 写入幂等（与签名协同）

写入还必须带 `Idempotency-Key`，语义见下；幂等判定使用的载荷哈希包含
`Content-Type` 与原始体字节，因此不同载荷在同键下会被明确识别为冲突，
而不会覆盖已生效结果。
