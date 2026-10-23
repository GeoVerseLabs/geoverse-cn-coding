# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。数据文件表结构变化（`SCHEMA_VERSION`）需要重新建库，会单独注明。

## [未发布]

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
