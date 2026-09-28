# API 版本协商与下线策略

## 1. 客户端如何选择版本

所有业务接口（`/api/*`）都**必须**显式携带请求头：

```
X-API-Version: v2
```

服务端不做隐式版本选择。这样可以避免服务端升级默认版本后，老客户端在
不知情的情况下拿到语义不同的响应。

当前注册的版本（见 `app/config.py`）：

| 版本 | 状态（锚点 2026-09-28） | deprecated_at | sunset_at | 后继 |
|------|------------------------|---------------|-----------|------|
| v0   | 已下线（sunset）        | 2025-01-01    | 2025-06-01 | v1 |
| v1   | 弃用期（deprecated）    | 2026-06-01    | 2027-01-01 | v2 |
| v2   | 受支持（supported）     | -             | -         | -  |

## 2. 版本生命周期

```
introduced ──> supported ──> deprecated ──> sunset
                              弃用宽限期       彻底下线
```

- `today < deprecated_at`：正常服务。
- `deprecated_at <= today < sunset_at`：**请求仍被接受**，但每个响应都会带：
  - `Deprecation: <deprecated_at>`
  - `Sunset: <HTTP-date>`（RFC 8594 / RFC 7231 风格的下线时间）
  - `Link: <deprecation 说明>; rel="deprecation", <后继>; rel="successor-version"`
  - `API-Version-Status: deprecated`
- `today >= sunset_at`：请求被 **410 Gone** 拒绝，错误码 `version_sunset`，
  响应体 `details` 中给出 `sunset_at` 与 `successor`。

边界按"当天"判定：`today == deprecated_at` 当天即进入弃用期；
`today == sunset_at` 当天即下线。对应测试见
`tests/test_versioning.py::test_deprecation_boundary_day`。

## 3. 可区分的拒绝原因

| 场景 | HTTP | error.code |
|------|------|-----------|
| 未带 `X-API-Version` | 400 | `version_missing` |
| 版本号注册表中不存在（如 v9） | 404 | `version_unknown` |
| 版本存在，但该接口在该版本下不提供（如 `POST /api/orders/batch-confirm` 仅 v2） | 400 | `version_unsupported_combo` |
| 版本已过下线日期 | 410 | `version_sunset` |

"未知版本"与"不支持的组合"被刻意区分：前者是版本标识本身无法识别，
后者是版本合法、但能力在该版本不存在（例如版本太低，功能尚未引入）。

## 4. 相邻版本的业务语义一致性

内部只有一个与版本无关的规范模型 `app.schemas.Order`，每个版本的响应由
**显式序列化器** `serialize_order_v1 / serialize_order_v2` 生成：

- 新版本只能做**加法**（v2 相对 v1 仅新增 `line` 嵌套明细）；
- 同名字段在所有版本中来源相同、含义相同，不允许改名改义；
- 测试 `tests/test_api_version_semantics.py` 对同一订单在 v1/v2 下
  逐字段比对，验证"没有静默丢失、没有含义漂移"，并用
  `FIELD_PROVENANCE` 校验每个版本字段都能追溯到规范模型。

## 5. 新增/下线一个版本要改哪里

1. 在 `app/config.py::build_version_registry` 增加/修改 `VersionSpec`；
2. 如该版本对某接口不可用，在 `app/main.py::_ROUTE_VERSION_TABLE` 中
   声明该接口支持的版本集合；
3. 在 `app/schemas.py` 增加该版本的显式序列化器并注册到
   `ORDER_SERIALIZERS` / `FIELD_PROVENANCE`；
4. 补充对应单测。
