# 版本协商与下线策略

## 选择版本

客户端通过请求头选择目标版本：

```
X-Api-Version: 2        # 或 "v2"
```

- 值为整数，允许可选的 `v` 前缀；缺省该头时使用服务端**默认版本**（当前为 v2）。
- 每个响应都可据此判定实际生效版本；弃用版本还会回送额外头（见下）。

## 生命周期

每个版本处于且仅处于三种状态之一，由当前时间与注册的两个时间戳共同决定：

| 状态 | 条件 | 行为 |
|------|------|------|
| `active` | 未到 `deprecated_at` | 正常服务 |
| `deprecated` | `deprecated_at <= now < sunset_at` | 正常服务，但回送弃用/下线头 |
| `retired` | `now >= sunset_at`（边界含等号） | 硬拒绝 `410 version.retired` |

默认本地配置（以进程启动/测试构建时刻为锚点）：

- **v1**：处于弃用期，弃用开始于 1 天前，下线（sunset）在 60 天后；
- **v2**：`active`，且为默认版本。

## 三类拒绝原因可区分

| 场景 | HTTP | 错误码 `error.code` |
|------|------|---------------------|
| 版本号不存在（如 `9`、`banana`） | 400 | `version.unknown` |
| 版本存在，但该端点不提供（如用 v1 访问仅 v2 的端点） | 400 | `version.unsupported` |
| 版本存在但已过 sunset | 410 | `version.retired` |

错误体统一形如：

```json
{
  "error": {
    "code": "version.unsupported",
    "message": "The API version is recognised but not supported by this endpoint.",
    "detail": "Endpoint does not serve version 1; served versions: [2].",
    "context": { "requested": 1, "supported": [2] }
  }
}
```

`version.unknown`（版本号本身不合法）与 `version.unsupported`（合法但该端点不提供）
因此可以被客户端分别处理；`version.retired` 使用 410 表达“永久下线”而非临时错误。

## 弃用期响应头

对弃用版本的成功响应，自动附加：

| 头 | 含义 |
|----|------|
| `Deprecation: @<unix-timestamp>` | 弃用起点（RFC 8594 风格） |
| `Sunset: <HTTP-date>` | 计划下线时刻 |
| `X-Api-Deprecated: true` | 便于程序快速判定 |
| `X-Api-Sunset-In-Seconds: <n>` | 距离下线剩余秒数（最小为 0） |
| `Link: <...>; rel="deprecation"` | 指向迁移说明 |

下线边界逐秒定义并被测试覆盖：sunset 前 1 秒仍是 `deprecated`（业务正常 +
剩余秒数为 1），到达 sunset 当刻即为 `retired`（410）。

## 相邻版本的业务语义一致

写入路径与版本无关：v1/v2 共用同一份校验与持久化代码，随后只在**响应投影**层
按版本整形。投影遵循“**严格只增（strictly additive）**”：

- v1 的所有字段在 v2 中名称、类型、含义完全不变（`id, product, quantity,
  amount, currency, note, created_at`）；
- v2 仅**新增**字段（`status`），不重命名、不改变量纲（金额始终为整数分）、
  不改变枚举含义。

因此同一业务请求在相邻版本下不会出现字段静默丢失或含义漂移；
`tests/test_api_governance.py::test_adjacent_versions_keep_identical_shared_semantics`
对共享字段逐一断言相等，并断言 v2 相对 v1 的差集恰好是 `{"status"}`。

## 元接口

`GET /_/versions`（无需签名）返回每个版本的当前生命周期、弃用/下线时间戳与
距下线秒数，便于客户端与运维核查。
