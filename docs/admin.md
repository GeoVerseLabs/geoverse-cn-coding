# 数据管理

数据仍然是一个 SQLite 文件。在此之上提供三种维护入口，能力相同、共用同一套写入逻辑：

| 入口 | 适合 | 说明 |
|---|---|---|
| 管理界面 `/admin` | 运营、数据维护人员 | 浏览器打开，用 admin 角色的 API Key 登录 |
| 管理 API `/v1/admin/*` | 业务系统对接、批处理脚本 | 见下文“接口” |
| MCP 工具 | agent 自动维护 | 见 [mcp.md](mcp.md) |

可以维护的内容：

- **要素**：POI、门牌点、道路的新增、修改、删除；
- **确认结果**：“这种写法就是这个坐标”（签收点、人工核验），同样的写法再来时直接命中；
- **待审核队列**：兜底服务返回的结果（`GEOCODER_REVIEW_LOG`）和业务反馈（`GEOCODER_FEEDBACK_LOG`），审核通过即写入确认结果；
- **变更记录**：每一次修改的操作人、时间、修改前后内容，可导出。

行政区划（区划代码、别名、边界）不在线编辑，仍按 [data.md](data.md) 走建库流程。

## 1. 开启

```bash
cngeocoder keygen --name ops --role admin          # 生成管理员密钥，记下 key 和配置条目
export GEOCODER_API_KEYS="ops:admin:sha256:<上一步输出的哈希>"
export GEOCODER_EDITABLE=1                         # 允许在线编辑；不开时管理界面只读
export GEOCODER_REVIEW_LOG=var/review.jsonl        # 可选：兜底结果进入待审核队列
export GEOCODER_FEEDBACK_LOG=var/feedback.jsonl    # 可选：业务反馈进入待审核队列
cngeocoder serve --port 8000
# 浏览器打开 http://127.0.0.1:8000/admin
```

- 管理功能只对 admin 角色开放；**没有配置任何 API Key 时管理功能不可用**（匿名调用方永远不是 admin）。鉴权配置见 [auth.md](auth.md)。
- `GEOCODER_EDITABLE=1` 需要数据文件和它所在的目录都可写（SQLite 写入时要在同目录建日志文件），否则启动报错。
- 不开在线编辑时，管理界面和读接口照常可用（浏览、搜索、查看变更记录），写操作返回 409。

## 2. 管理界面

| 页面 | 功能 |
|---|---|
| 概览 | 各类要素数量、数据版本、缓存与兜底状态、API Key 列表与调用计数、MCP 接入示例 |
| 要素 | 按名称 / 地址 / 路名 / 别名 / uid、类型、区划编码前缀、来源查询；新增、编辑、删除。区划编码可按名称搜索选择，留空自动推断 |
| 确认结果 | 新增“地址写法 → 坐标”，查询、删除 |
| 待审核 | 兜底结果与业务反馈：通过、修正坐标后通过、驳回 |
| 变更记录 | 全部在线修改，查看前后内容，导出 JSONL |
| 地址测试 | 修改后立即用正向 / 逆向编码验证效果 |

右上角的坐标系选择同时作用于显示和输入：从高德、腾讯拾取的坐标选 GCJ-02，百度选 BD-09，入库统一转成 WGS84。

页面是一个静态文件，不依赖外网（私有化、离线环境可用）。密钥只保存在当前标签页（sessionStorage），关闭即失效。

## 3. 写入规则

**要素**

- 新增点要素需要坐标，以及名称或门牌之一；道路需要 `kind=road` 和 GeoJSON 线几何。
- 字段与建库输入一致（见 [data.md](data.md) 第 3 节）：没给路名、门牌时从地址解析；没给区划编码时按“地址文本 → 边界点落面”推断；简称自动生成。
- 修改只覆盖传入的字段。改了地址或坐标、又没有传区划编码时，重新推断所属行政区。
- 区划编码与坐标明显矛盾时（坐标落在别的区县、离所选行政区中心过远，常见于同名区划选错、经纬度写反、坐标系弄错），结果里带 `warnings` 提示，不拦截。
- 每个要素有稳定的 `uid`：建库数据取输入里的 `uid` 列，没有时按“来源文件 + 内容”生成，同样的输入每次建库 uid 不变；在线新增的为 `m-` 开头的随机串（也可自定义）。修改要素后内部 `id` 会变，`uid` 不变。

**确认结果**

- 同一地址的原文归一化写法和标准写法都会写入，口语化的同义写法也能命中。
- `count` 为确认次数，越多置信度越高；`spread` 为多次确认的离散程度（米），超过 300 米的不采用。

**生效时间**

- 本进程立即生效（清空缓存）。
- 多 worker / 多进程部署时，其他进程每秒检查一次数据版本（`meta.edit_version`），发现变化后清空本地缓存，最多延迟约 1 秒。
- 写入用 SQLite 回滚日志，每次提交只锁几毫秒；读请求遇到锁会等待（最多 5 秒），不会报错。
- 所有共享同一个数据文件的进程必须使用相同的 `GEOCODER_EDITABLE` 设置：只读进程以 immutable 方式打开，看不到别的进程写入的内容。

## 4. 变更记录与重建

在线修改直接写在数据文件里。重新建库会生成全新的文件，所以**重建前要导出变更记录，建库时重放**：

```bash
cngeocoder --db /data/geocoder.db edits export --out var/edits.jsonl   # 或在管理界面“变更记录”页导出
cngeocoder build --places ... --roads ... --memory var/feedback.jsonl \
    --edits var/edits.jsonl --out var/geocoder.next.db
```

- 重放在导入全部要素和确认结果之后执行，按发生顺序逐条应用；
- 所有操作按 `uid` / `key` 幂等，同一份日志重放多次结果相同，可以一直累积使用同一份导出；
- 变更记录随数据带到新文件，下次导出仍是完整历史；
- 目标已不存在的操作（例如源数据里已删掉的要素）跳过并打印告警。

修改、删除要素时，全文索引里旧的条目会留到下次重建（SQLite FTS5 的 contentless 表不支持按 id 删除），检索时与要素表关联会自动过滤，不影响结果；要素 id 只增不复用，失效条目不会挂到新要素上。在线编辑量很大（例如超过总量的 10%）时建议导出变更后重建一次。

## 5. 接口

全部需要 admin 角色（`/v1/admin/regions` 只需 read）。坐标参数与返回都支持 `coordsys`。交互文档见 `/docs` 的 admin 分组。

| 接口 | 说明 |
|---|---|
| `GET /v1/admin/overview` | 数据与服务概况、当前调用方、密钥列表 |
| `GET /v1/admin/places?q=&kind=&adcode=&source=&offset=&limit=` | 查询要素 |
| `GET /v1/admin/places/{uid}` | 要素详情 |
| `POST /v1/admin/places` | 新增，`{"name","kind","alias","address","road","road_no","adcode","location":"lng,lat","coordsys","weight","geometry","uid"}` |
| `PATCH /v1/admin/places/{uid}` | 修改，只传要改的字段；传 `null` 表示清空 |
| `DELETE /v1/admin/places/{uid}` | 删除 |
| `GET /v1/admin/memory?q=` · `POST /v1/admin/memory` · `DELETE /v1/admin/memory?key=` | 确认结果，新增为 `{"address","location","coordsys","count","spread"}` |
| `GET /v1/admin/review?source=&status=` · `POST /v1/admin/review/{id}` | 待审核队列，处理为 `{"action":"approve"|"dismiss","location"}` |
| `GET /v1/admin/changes` · `GET /v1/admin/changes/export` | 变更记录、导出 JSONL |
| `GET /v1/admin/regions?q=` | 按名称或编码查行政区 |
| `GET /v1/admin/auth` | 密钥配置概况与调用计数（不含密钥） |

```bash
KEY=cng_xxx
curl -X POST localhost:8000/v1/admin/places -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"name":"望京SOHO T4","address":"北京市朝阳区望京街10号","location":"116.4812,39.9968","coordsys":"gcj02"}'
```
