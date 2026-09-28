# 限流、配额与写入幂等语义

## 令牌桶限流（突发）

按**调用方维度**独立计数，规则为三元组：

```
rate_per_second : 稳态补充速率（令牌/秒）
burst           : 桶容量（允许的瞬时突发）
daily_quota     : 每个 UTC 自然日的接受上限
```

判定过程在**单把锁内、基于对注入时钟的一次观测**完成“补充令牌 → 判断 →
扣减令牌 → 累加配额”，因此突发并发下不会出现“读旧值—各自通过—重复扣减”的
竞态击穿：N 个并发请求最多接受 `burst` 个（对应测试用 50 线程打 burst=10/3，
接受数精确等于容量）。

被限流时返回：

- HTTP 429，`error.code = rate_limit.exceeded`；
- `Retry-After`（秒，向上取整，最小 1）给出建议重试间隔；
- 响应头同时回报 `X-RateLimit-Rate / -Burst / -Quota / -Quota-Remaining`。

**被拒绝的请求不扣令牌、不计入配额**，因此限流不会“连带影响已被接受的请求”。

## 每日配额

- 计数器在 UTC 午夜按天重置（`quota.exhausted` 的 `Retry-After` 即到下个
  UTC 午夜的剩余秒数）；
- 配额耗尽返回 HTTP 429，`error.code = quota.exhausted`，与突发超限明确区分；
- 桶与配额都是 **per-caller**：一个调用方被限流不会影响其它调用方
  （`tests/test_concurrency.py::test_throttled_caller_does_not_touch_other_callers`）。

## 规则可配置且变更可预期

- 通过 `PUT /_/admin/rate-rules/{caller}`（本地管理接口）原子热更新单个调用方规则，
  或通过 `RateLimiter.update_rules` / `set_rule` 在进程内替换；
- 规则保存在一张**不可变映射**中，替换是整体原子操作：在途请求用旧规则走完，
  之后的请求立即看到新规则；
- **扩容**（提高 burst）立即补充新增容量，下一个请求即生效；
- **缩容**不回收已赚取的令牌，不会追溯影响已经接受的请求；
- 配额计数独立于规则替换，不会因换规则而被清零或翻倍。

## 写入幂等

`POST /orders` 必须携带 `Idempotency-Key`。服务端以该键为单位建立槽位：

| 情形 | 行为 |
|------|------|
| 同键、同载荷的首次请求 | 执行业务一次，结果连同状态码/响应头落盘，返回 `201`，带 `Idempotency-Saved: true` |
| 同键、同载荷的**并发**重试 | 阻塞等待首次完成，然后返回**同一份**结果，带 `Idempotency-Replayed: true` |
| 同键、同载荷的后续重试 | 直接回放存储结果，`Idempotency-Replayed: true` |
| 同键、**不同载荷** | 返回 `409 idempotency.key_conflict`，**绝不覆盖**原有结果；原结果仍可正常回放 |
| 首次请求在产出结果前失败（如 422） | 槽位标记为失败，相同载荷的后续请求可接管执行（失败结果不被缓存） |

“同载荷”的判据是 `sha256(Content-Type + 0x00 + 原始体字节)`，因此哪怕只有
一个字段不同也会触发冲突保护。并发只生效一次的保证来自槽位 +
`threading.Event`：所有相同载荷的等待者在首次完成后读到的是同一个
`StoredResponse`；并发测试用 20 个线程同键同体提交，断言
`201==20`、唯一订单号集合大小为 1、回放数为 19、底层订单仓储只新增 1 条。

## 治理顺序对配额的保护

流水线顺序为 **版本 → 验签 → 限流 → 幂等/业务**。限流在验签之后，意味着
只有通过身份校验的请求才可能消耗配额；伪造 `X-Caller-Id` 的请求会先被
`signature.unknown_caller / signature.invalid` 等拒绝，无法烧穿他人配额。
