# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。数据文件表结构变化（`SCHEMA_VERSION`）需要重新建库，会单独注明。

## [未发布]

数据文件 `SCHEMA_VERSION = 4`（place 表新增 `uid` 列，新增 `change_log`、`review_state` 表），**需要用新版本重新建库**。

### 新增
- 数据管理：管理界面 `/admin`（要素、确认结果、待审核队列、变更记录、地址测试）与管理 API `/v1/admin/*`。`GEOCODER_EDITABLE=1` 时可在线增删改 POI / 门牌 / 道路和确认结果，本进程立即生效，其他 worker 约 1 秒内生效；区划编码与坐标明显矛盾时给出提示。
- 待审核队列：兜底结果（`GEOCODER_REVIEW_LOG`）与业务反馈（`GEOCODER_FEEDBACK_LOG`）可在线审核，通过后写入确认结果。
- 变更记录：每次修改记录操作人与前后内容；`cngeocoder edits export` 导出、`cngeocoder build --edits` 重放（按 uid / key 幂等），在线修改不会因重建丢失。
- 要素稳定标识 `uid`：输入可显式给出，否则按来源与内容生成，同样的输入每次建库不变。
- 鉴权：API Key（`GEOCODER_API_KEYS` / `GEOCODER_API_KEYS_FILE`，支持只存 SHA-256），read / write / admin 三级角色，按调用方限流（`GEOCODER_RATE_LIMIT`、密钥级 `rpm`），错误密钥节流，`/v1/auth/whoami`，`cngeocoder keygen`。
- MCP：`POST /mcp`（Streamable HTTP，无状态）与 `cngeocoder mcp`（stdio），6 个查询工具 + 11 个数据维护工具，按角色开放。

### 变更
- 没有配置 API Key 时行为不变（查询与反馈开放）；配置后默认所有接口都要带密钥（`GEOCODER_ANONYMOUS_ROLE` 可调整）。
- `/v1/feedback` 需要 write 角色；反馈日志增加 `by`（调用方）字段。
- 建库时间约 +12%、数据文件约 +18%（`uid` 列与索引）；查询性能不变。20 万条基准（`scripts/bench.py --n 200000`）：建库 23.4 → 26.3 s，文件 49 → 58 MB，内存峰值 107 → 108 MB；POI 0.53 → 0.53 ms，门牌 0.40 → 0.39 ms，逆地理 0.20 → 0.19 ms。

### 修复
- README 架构图：子图标题含全角括号导致 GitHub 上 Mermaid 渲染失败，改为 ASCII 子图 id + 带引号的标签。

### 变更
- `pyproject.toml` 补充 keywords、classifiers、项目链接。

## [0.1.0] - 2026-10-23

首个版本。数据文件 `SCHEMA_VERSION = 3`。

### 新增

- 地址结构化解析：省 / 市 / 区县 / 乡镇 + 社区、道路、门牌、POI、楼栋单元室；行政区 AC 自动机匹配与层级消歧、同名区县多解释、简称、旧名、民族自治地方、不设区的地级市等国内特性。
- 正向地理编码：门牌精确与插值、POI、道路、交叉口、区划中心点多级候选，置信度与兜底（天地图，配额 + 熔断 + 待审核日志）。
- 逆地理编码：边界点落面 / 周边投票 / 最近中心点，最近门牌、道路与方位描述。
- WGS84 / GCJ-02 / BD-09 / CGCS2000 坐标转换，接口统一 `coordsys` 参数。
- 口语化与非标准输入：繁体、收寄件口语、城市别称、行政区错别字纠正、子位置、简称生成、按序省略、方位位移与参照物、同名歧义备选、`signals` 说明字段。
- 历史确认结果：`POST /v1/feedback` 回流 + `build --memory` 导入。
- 内置数据：统计用区划代码 2023、国家地名信息库 2025 县级以上增量、1,015 条历史旧名、38,428 个区划中心点（Overture / OSM，ODbL）。
- 数据源脚本：`scripts/sources/areacodes.py`、`scripts/sources/overture.py`（区划点、边界、道路、POI 按范围抽取）。
- 评测：金标准评测与置信度校准指标（`confident_correct`、`confident_wrong`、`negatives_rejected`），CI 卡口。
- 部署：单镜像 Docker、docker compose、FastAPI 接口与 CLI。
