# FastAPI API Governance（版本协商 / 来源签名 / 限流配额）

在现有 FastAPI 服务之上实现一层面向外部的 API 接入与请求治理能力：
**版本协商与下线**、**写入请求签名与幂等**、**按调用方维度的限流与配额**，
三者在所有受治理接口上行为一致、拒绝原因可区分，且全部可在本地离线验证。

## 快速开始

```bash
uv sync                       # 安装依赖（含 dev 组的 pytest / httpx）
uv run pytest -s              # 运行全部测试并打印“输入/判定依据/最终状态”
uv run python main.py         # 本地启动 http://127.0.0.1:8000
```

不依赖任何外部服务或真实凭据：签名密钥为本地演示常量，存储全部为内存实现。

## 受治理接口

| 方法 | 路径 | 支持版本 | 说明 |
|------|------|----------|------|
| POST | `/orders` | v1, v2 | 签名 + 幂等 + 限流 |
| GET  | `/orders/{id}` | v1, v2 | 签名 + 限流 |
| GET  | `/inventory/summary` | **仅 v2** | 用 v1 访问得到 `version.unsupported` |
| GET  | `/_/versions` | 不限 | 查询版本生命周期（未签名元接口） |
| GET/PUT | `/_/admin/rate-rules[/{caller}]` | 不限 | 本地查看 / 热更新限流配额规则 |

治理流水线在每个受治理请求上严格按以下顺序执行（顺序本身也是安全策略）：

1. **版本协商**：`unknown / unsupported / retired` 三类拒绝彼此可区分；
2. **来源签名**：`缺凭证 / 未知调用方 / 时间窗过期 / 签名错误 / 重放` 彼此可区分；
3. **限流配额**：`突发超限 rate_limit.exceeded` 与 `每日配额 quota.exhausted` 可区分。

> 验签在限流之前：未通过身份校验的请求无法信任其声称的 `X-Caller-Id`，
> 因此攻击者不能伪造他人身份去消耗受害者的配额。

## 文档

- [`docs/versioning.md`](docs/versioning.md) — 版本协商与下线策略
- [`docs/signing.md`](docs/signing.md) — 签名、时间窗与密钥轮换规则
- [`docs/rate-limiting.md`](docs/rate-limiting.md) — 限流与配额语义、热更新
- [`docs/local-validation.md`](docs/local-validation.md) — 本地验证方法与脚本

## 目录结构

```
app/
  clock.py         # 时钟协议（SystemClock / FakeClock），测试可注入
  errors.py        # 稳定的机器可读错误码与统一 JSON 错误体
  versioning.py    # 版本注册表 / 协商 / 弃用与下线判定 / Deprecation 头
  signature.py     # HMAC 验签、时间窗、密钥轮换区间、nonce 防重放
  idempotency.py   # 并发幂等槽位：只执行一次、冲突不覆盖、失败可重试
  ratelimit.py     # 令牌桶限流 + UTC 每日配额（原子计数，规则原子热替换）
  models.py        # 跨版本共享的 Pydantic 模型
  store.py         # 内存订单仓储
  service.py       # 业务逻辑 + 版本视图投影（v2 相对 v1 严格只增字段）
  config.py        # 聚合治理组件与默认本地配置
  dependencies.py  # FastAPI 治理网关依赖（版本→验签→限流）
  main.py          # 应用工厂与路由
tests/             # 50 个单测/并发/边界场景，全部本地可跑
```
