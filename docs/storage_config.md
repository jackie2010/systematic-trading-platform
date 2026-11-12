# Storage 配置说明 (Phase 2)

在 `config.json`（或 `zvt_home/config.json`）中可添加 `storage` 配置块，用于自定义存储路径和路由。

## 配置结构

```json
{
  "storage": {
    "base_path": null,
    "path_template": null,
    "storage_routes": {},
    "schema_providers": {}
  }
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `base_path` | string \| null | 数据根路径。`null` 时使用 `zvt_env["data_path"]`（即 `zvt_home/data`） |
| `path_template` | string \| null | 路径模板。占位符：`{base_path}`, `{provider}`, `{db_name}`, `{storage_id}`。`null` 时使用默认规则 |
| `storage_routes` | object | 路由覆盖。键格式 `"provider\|db_name"`，值为自定义 `storage_id` |
| `schema_providers` | object | 无 Recorder 的 schema 的 provider 映射。键为 `db_name`，值为 `["provider"]` 数组。用于 fallback 或 internal schema |

### 默认行为（不配置时）

- `storage_id = "{provider}_{db_name}"`（如 `em_stock_meta`）
- 路径：`{data_path}/{provider}/{provider}_{db_name}.db`

### 示例

**自定义 base_path：**
```json
{
  "storage": {
    "base_path": "/custom/data/path"
  }
}
```

**自定义 path_template（扁平目录）：**
```json
{
  "storage": {
    "path_template": "{base_path}/{storage_id}.db"
  }
}
```

**自定义 storage_routes（某 provider 使用不同 storage_id）：**
```json
{
  "storage": {
    "storage_routes": {
      "qmt|stock_1d_kdata": "qmt_realtime_stock_1d"
    }
  }
}
```

## Internal Schema（内部 schema）

业务逻辑类数据（如 `zvt_info`、`trader_info`、`stock_tags`）与外部数据源无关，通过 `register_schema(..., internal=True)` 标记为 internal。Internal schema 只做存储路由，不参与 provider 切换。其 provider（如 `zvt`）在 `schema_providers` 中配置，用于确定存储路径。
