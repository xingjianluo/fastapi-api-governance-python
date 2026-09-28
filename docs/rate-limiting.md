# 按调用方维度的限流与配额

## 1. 两层模型

调用方由请求头 `X-Caller-Id` 标识（缺省为 `anonymous`）。每个调用方有
两层独立计数：

| 层 | 配置 | 窗口 | 用途 |
|----|------|------|------|
| 秒级突发 | `rps_limit` | 固定 1 秒（对齐 epoch 秒） | 挡住瞬时突发并发 |
| 长周期配额 | `quota_limit` / `quota_window_seconds` | 固定窗口，对齐 epoch | 控制总量（如每小时） |

任一层超出即拒绝；秒级窗口先判定。默认配置见
`app/config.py::build_rate_limiter`。

## 2. 并发安全：计数不会被竞态击穿

`RateLimiter.check` 在**同一把锁**内完成
"读当前窗口计数 → 与阈值比较 → 计数加一"。并发请求不可能同时读到
"未满"而双双放行。`tests/test_ratelimit.py::test_concurrent_burst_counter_not_broken`
用 20 个线程同时冲击 `rps_limit=5`，断言接受数恰好为 5。

## 3. 拒绝不连累已接受请求

- 被拒请求**不增加任何计数**（先判定后加计数，拒绝路径直接返回）；
- 因此大量被 429 拒绝的请求不会挤占额度，也不会影响后续合法请求；
  见 `tests/test_ratelimit.py::test_rejected_requests_do_not_consume_quota`
  与 `tests/test_http_ratelimit_boundary.py::test_rejected_does_not_affect_accepted_then_replay`。
- 不同调用方计数器完全隔离。

## 4. 拒绝响应与重试提示

两种拒绝原因可区分：

| 场景 | HTTP | error.code | scope |
|------|------|-----------|-------|
| 秒级突发超限 | 429 | `burst_rate_limited` | `burst` |
| 长周期配额耗尽 | 429 | `quota_exhausted` | `quota` |

响应头（成功响应也会带前三个）：

```
X-RateLimit-Scope: burst | quota | ok
X-RateLimit-Limit: 5
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1790606315
Retry-After: 1                 # 仅拒绝时；建议等待秒数
```

`Retry-After` 是按"到下一窗口重置时刻的秒数"计算的，客户端应等待后重试。

## 5. 规则配置与热更新

规则在 `app/config.py` 集中配置，也可运行时热替换：

```python
app.state.governance.rate_limiter.update_rules({
    "premium": RateLimitRule("premium", rps_limit=10, quota_limit=2000,
                             quota_window_seconds=3600),
})
```

热更新行为是**可预期**的：

- 新阈值立即生效；
- 当前窗口已接受请求的计数保留，不重置；
- 因此 `remaining = 新阈值 - 当前窗口已用计数`。

例如当前秒已接受 3 次，把 `rps_limit` 从 5 收紧到 4，则下一次请求被接受
（已用 3，新阈值 4），再下一次被拒绝（已用 4）。
见 `tests/test_ratelimit.py::test_rule_update_is_predictable`。

## 6. 与幂等的配合

限流先于幂等执行。一个需要注意但符合预期的边界：如果客户端在同一秒内
高频并发重试，可能先撞上秒级限流（429）而拿不到幂等回放；按
`Retry-After` 等到下一秒重试即可，此时同一幂等键会返回首次结果
（对应集成测试 `test_rejected_does_not_affect_accepted_then_replay`）。
被限流请求不会消耗幂等键，也不会影响已经被接受的首次请求。
