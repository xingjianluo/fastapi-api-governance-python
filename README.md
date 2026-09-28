# FastAPI API Governance Bootstrap

在示例订单服务之上，提供一层统一、可解释的 API 接入与请求治理能力：

- **版本协商与下线**：显式版本选择；缺版本 / 未知版本 / 接口-版本组合不支持
  / 已下线四类拒绝原因可区分；弃用期响应带 `Deprecation`、`Sunset`、后继
  版本链接；相邻版本业务语义一致，新版本只做加法，禁止静默丢字段。
- **来源签名与幂等**：HMAC-SHA256 签名 + 时间窗 + nonce 防重放；密钥轮换
  重叠期内新旧密钥同时生效，宽限边界明确；过期 / 签名错误 / 重放分别拒绝；
  同一幂等键并发重试只真正执行一次，结果一致；内容冲突明确报错不覆盖。
- **按调用方限流与配额**：秒级突发 + 长周期配额两层计数，锁内原子判定，
  突发并发不会击穿计数；超限带 `Retry-After`，被拒不占额度、不影响已接受
  请求；规则可配置、可热更新且行为可预期。

## 目录结构

```
app/
  governance/
    errors.py        # 可区分错误码体系
    versioning.py    # 版本注册表 / 生命周期 / 弃用响应头
    signing.py       # HMAC 签名 / 时间窗 / 密钥轮换 / nonce 去重
    idempotency.py   # 线程安全幂等状态机
    ratelimit.py     # 秒级窗口 + 配额窗口
  api/
    dependencies.py  # 统一治理管线（限流->版本->签名->幂等）
    orders.py        # 示例业务接口
  config.py          # 版本表/密钥/限流规则集中配置
  schemas.py         # 规范模型与显式版本序列化器
  service.py         # 确定性计价的订单服务
  main.py            # 应用装配与统一错误响应
tests/               # 49 个测试（含并发/轮换/下线边界，带判定日志）
scripts/demo.py      # 进程内一键演示
docs/                # 策略与本地验证文档
```

## 快速开始

```bash
uv run pytest                      # 全量本地测试
uv run python scripts/demo.py      # 不启动服务的交互式场景演示
uv run python main.py              # 启动真实 HTTP 服务（/docs）
```

## 文档

- [版本协商与下线策略](docs/versioning.md)
- [签名与密钥轮换规则](docs/signing.md)
- [限流配额语义](docs/rate-limiting.md)
- [本地验证方法](docs/local-verification.md)
