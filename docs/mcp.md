# MCP 接入

服务实现了 [Model Context Protocol](https://modelcontextprotocol.io) 的工具能力，agent（Claude Code、Claude Desktop 及其他 MCP 客户端）可以直接查询地址，也可以在授权后维护数据。协议层直接实现，不引入额外依赖。

## 1. 接入方式

| 方式 | 地址 / 命令 | 鉴权 | 适合 |
|---|---|---|---|
| HTTP（Streamable HTTP） | `POST http://<host>:8000/mcp` | 与 HTTP API 相同的 API Key | 共享的服务实例、远程 agent |
| stdio | `cngeocoder --db <数据文件> mcp --role read\|admin` | 由启动参数决定（本机进程，信任边界是操作系统用户） | 本机 agent 直接读写数据文件 |

Claude Code：

```bash
# HTTP
claude mcp add --transport http cngeocoder http://127.0.0.1:8000/mcp --header "Authorization: Bearer cng_xxx"

# stdio，只读
claude mcp add cngeocoder -- cngeocoder --db /data/geocoder.db mcp
# stdio，可维护数据（变更记录里的操作人为 --name）
claude mcp add cngeocoder-admin -- cngeocoder --db /data/geocoder.db mcp --role admin --name claude-local
```

Claude Desktop 等用 JSON 配置的客户端：

```json
{
  "mcpServers": {
    "cngeocoder": {"command": "cngeocoder", "args": ["--db", "/data/geocoder.db", "mcp", "--role", "admin"]}
  }
}
```

HTTP 方式说明：

- 无状态，响应为 `application/json`，不提供服务端推送流（`GET /mcp` 返回 405）；
- 支持协议版本 2024-11-05、2025-03-26、2025-06-18、2025-11-25；客户端用更新的版本试探时返回 `-32022` 并列出支持的版本，客户端随即改用 `initialize` 握手；
- 浏览器发起的跨源请求默认拒绝（防 DNS 重绑定），需要时用 `GEOCODER_MCP_ORIGINS` 放开；
- 每次 MCP 请求按一次 HTTP 请求计入限流。

stdio 方式下 admin 角色会以可写方式打开数据文件（`--no-editable` 关闭）。如果同一个文件同时被在线服务使用，在线服务也要开启 `GEOCODER_EDITABLE=1`（见 [admin.md](admin.md) 第 3 节）。

## 2. 工具

**查询（read 及以上）**

| 工具 | 说明 |
|---|---|
| `parse_address` | 地址结构化解析 |
| `geocode` | 地址 → 坐标；默认不调用兜底服务（`fallback=true` 开启，消耗配额） |
| `reverse_geocode` | 坐标 → 地址 |
| `convert_coordinates` | 坐标系转换 |
| `search_regions` | 按名称或编码查行政区（找 adcode） |
| `get_service_info` | 数据版本、数量、是否可编辑 |

**数据维护（admin，且服务开启在线编辑）**

| 工具 | 说明 |
|---|---|
| `search_places` · `get_place` | 查找、查看要素 |
| `add_place` · `update_place` · `delete_place` | 新增、修改、删除要素 |
| `list_memory` · `add_memory` · `delete_memory` | 确认结果 |
| `list_review` · `resolve_review` | 待审核队列 |
| `list_changes` | 变更记录 |

工具列表按调用方角色过滤：read 密钥看不到维护工具。工具带 `readOnlyHint` / `destructiveHint` 标注，客户端可据此决定是否需要人工确认。所有坐标参数都有 `coordsys`。

工具出错（参数不合法、要素不存在、只读模式等）时返回 `isError: true` 和中文说明，agent 可以据此修正后重试。新增 / 修改要素时，区划编码与坐标矛盾会在结果的 `warnings` 里提示。

## 3. 给 agent 的维护约定

服务在 `initialize` 时下发了使用说明，要点：

1. 新增前先 `search_places` 查重；
2. 用 `search_regions` 找区划编码，不要猜；
3. “某种写法应该落到某个坐标”用 `add_memory`，不要为此新建要素；
4. 修改后用 `geocode` 验证；
5. 每次修改都记入变更记录（操作人为密钥名称），可在管理界面审阅、导出。

建议给 agent 单独发一个 admin 密钥（如 `--name agent-maint`），便于在变更记录里区分人工和 agent 的修改。
