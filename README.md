# GeoVerse 中国地址编码服务（cngeocoder）

面向中国地址的轻量地理编码服务：**地址结构化解析、正向地理编码、逆地理编码、坐标系转换**。
按《地址解析与逆地理编码落地方案（国内 / 海外）》中的国内方案实现，并针对执行效率和部署简易性做了技术选型：
**一个 Python 进程 + 一个 SQLite 数据文件**，不依赖 Elasticsearch、PostGIS、Redis。

- 全国省 / 市 / 区县 / 乡镇四级区划词典、1,015 条历史旧名、3.8 万个区划中心点内置，开箱即可做地址标准化和区划级定位；
- 百万级 POI 下，单线程解析 0.08 ms、门牌 0.32 ms、POI 0.54 ms、逆地理 0.27 ms；
- 一个 Docker 镜像即可部署，私有化或离线环境把镜像和 `.db` 文件拷过去就能运行。

选型理由、与参考方案的差异和升级条件见 [docs/tech-selection.md](docs/tech-selection.md)。

## 针对国内地址的处理

| 问题 | 例子 | 处理 |
|---|---|---|
| 层级省略、简称 | “广东深圳南山科技园”“海淀区中关村大街27号” | AC 自动机一次扫出全部候选，动态规划找“位置递增、层级递增、编码前缀一致”的最优链，按编码补全上级 |
| 同名区县 | 北京 / 长春“朝阳区” | 直辖市、省会先验打破平局，同时保留多种解释，由检索阶段按数据判定 |
| 旧名、撤并、县改区 | 崇文区、襄樊市、密云县 | 别名表 + 自动生成旧称 |
| 民族自治地方 | “黔东南苗族侗族自治州”→“黔东南” | 按民族名逐级剥离生成简称 |
| 特殊建制 | 东莞、中山（不设区）；济源、仙桃（省直辖） | 层级补全时特殊处理 |
| 路名里的地名 | “中山东路”“朝阳北路” | 简称后紧跟“路/街/东/北…”时不当行政区 |
| 中文数字 | “人民路三段十八号”“三号楼二单元” | 只在量词前转换，“二七区”“十堰市”不受影响 |
| 门牌写法 | “100弄5号”“甲13号”“93号院”“3-2-501” | 规则切分出 road / road_no / building / unit / room |
| 噪声 | 收件人、手机号、“麻烦送到…谢谢”“放菜鸟驿站” | 归一化剔除，删掉的片段在 `signals.removed` 里可查 |
| 坐标系 | WGS84 / GCJ-02 / BD-09 / CGCS2000 | 内部统一 WGS84，接口用 `coordsys` 参数，只在出入口转换 |

解析输出结构：`province / city / district / town / community / road / road_no / poi / building / unit / floor / room` + 各级 adcode。

### 口语化与非标准写法

繁体、行政区错别字（海定区）、城市别称（魔都）、简称俗称（北大、工体）、子位置（南广场、B出口）、省略写法（腾讯大厦）、方位描述（往东 500 米、XX 对面的 YY）、交叉口都有专门处理。每个结果带 `signals` 说明置信度来源；多地同名时给出 `alternatives`。业务确认过的写法可通过 `POST /v1/feedback` 回流，下次建库后直接命中。

在口语化评测集上，与改动前相比：高置信度正确率 80% → 100%，负样本（数据中没有的地点）拒绝率 85.7% → 100%（样本量小，详见 [docs/colloquial.md](docs/colloquial.md)）。

## 架构

```mermaid
flowchart LR
  subgraph 离线建库
    A[区划 CSV + 别名] --> B[cngeocoder build]
    C[边界 GeoJSON] --> B
    D[POI / 门牌 CSV·GeoJSON] --> B
    E[道路 GeoJSON] --> B
    B --> F[(geocoder.db<br/>admin · place<br/>FTS5 · R*Tree)]
  end
  subgraph 在线服务（单进程）
    Q1[正向请求] --> P[归一化 + 行政区链 + 详细地址切分]
    P --> G[候选：门牌精确/插值 → POI → 道路 → 区划中心]
    G --> S[重打分 + 置信度]
    S -- 置信度不足 --> T[天地图兜底<br/>配额 + 熔断]
    Q2[逆向请求] --> R[边界点落面 / 周边投票<br/>最近门牌、道路、POI]
  end
  F -. 只读 mmap .-> G
  F -. 只读 mmap .-> R
```

| 模块 | 职责 |
|---|---|
| `normalize.py` | 全角半角、噪声剔除、中文数字 |
| `colloquial.py` | 繁简转换、收寄件口语清洗、简称生成 |
| `admin.py` | 区划词典、简称/别名、AC 自动机、层级消歧 |
| `parser.py` | 详细地址切分（乡镇、社区、道路门牌、POI、楼栋单元室） |
| `tokenizer.py` | 中文 bigram、FTS5 查询表达式、名称相似度 |
| `store.py` | SQLite 访问层（FTS5 全文、R*Tree 空间、按路名取门牌） |
| `geocoder.py` | 正向：候选生成、门牌插值、交叉口、变体比对、同名歧义、历史确认、置信度、兜底 |
| `reverse.py` / `boundary.py` | 逆向：点落面、周边投票、最近道路、方位描述 |
| `coords.py` | WGS84 / GCJ-02 / BD-09 互转 |
| `fallback/tianditu.py` | 天地图兜底（配额、熔断） |
| `build.py` | 离线建库 ETL |
| `api.py` / `cli.py` | HTTP API、命令行 |

## 快速开始

### 本地运行

```bash
pip install -e ".[dev]"
make data          # 内置区划 + 示例 POI 建库 → var/geocoder.db
make test          # 107 个测试
make serve         # http://127.0.0.1:8000/docs
```

不建库也能先试解析：

```bash
cngeocoder parse "广东深圳南山科技园腾讯滨海大厦" "上海市徐汇区漕溪北路100弄5号301室"
```

### Docker

```bash
docker compose up -d --build
curl "http://127.0.0.1:8000/v1/geocode?address=北京朝阳望京SOHO T3 1201室&coordsys=gcj02"
```

国内构建可替换基础镜像和 pip 源：

```bash
docker build \
  --build-arg BASE_IMAGE=<镜像加速地址>/library/python:3.12-slim \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  -t geoverse/cngeocoder:0.1.0 .
```

镜像内预建了一份“全国区划 + 示例 POI”的数据文件。生产环境挂载自建的数据文件：

```bash
docker run -d -p 8000:8000 -v $PWD/var:/data -e GEOCODER_DB=/data/geocoder.db \
  -e WORKERS=4 -e TIANDITU_TK=<天地图 key> geoverse/cngeocoder:0.1.0
```

## API

所有接口支持 `coordsys=wgs84|gcj02|bd09|cgcs2000`（默认 wgs84），交互文档在 `/docs`。

| 接口 | 说明 |
|---|---|
| `GET /v1/parse?address=&city=` | 地址结构化解析 |
| `GET /v1/geocode?address=&city=&coordsys=&focus=lng,lat&limit=&fallback=` | 正向地理编码；`city` 可传名称或区划编码，`focus` 为位置偏好 |
| `POST /v1/geocode/batch` | 批量正向，`{"items":[{"id":1,"address":"…","city":"…"}],"coordsys":"gcj02"}`，单次 ≤ 1000 |
| `GET /v1/reverse?location=lng,lat&coordsys=&radius=&poi_limit=` | 逆地理编码 |
| `POST /v1/reverse/batch` | 批量逆地理 |
| `GET /v1/convert?location=&from=&to=` | 坐标系转换 |
| `POST /v1/feedback` | 回传确认坐标 `{"address","location","coordsys","source"}`，下次建库用 `--memory` 导入（需配置 `GEOCODER_FEEDBACK_LOG`） |
| `GET /v1/meta` · `GET /health` | 数据版本、计数、缓存与兜底状态；健康检查 |

正向结果示例（示例数据，`coordsys=gcj02`）：

```json
{
  "status": "ok",
  "query": "北京朝阳望京SOHO T3 1201室",
  "results": [{
    "location": {"lng": 116.481053, "lat": 39.996311},
    "precision": "poi",
    "confidence": 0.968,
    "source": "local",
    "formatted_address": "北京市朝阳区阜通东大街6号望京SOHO-T3-1201室",
    "components": {"province": "北京市", "city": "北京市", "district": "朝阳区", "poi": "望京SOHO",
                   "building": "T3", "room": "1201室", "...": "..."},
    "adcode": {"province": "110000", "city": "110100", "district": "110105", "town": null},
    "matched": {"id": 6, "kind": "poi", "name": "望京SOHO", "road": "阜通东大街", "road_no": "6号"}
  }],
  "coordsys": "gcj02"
}
```

`precision` 取值：`door`（门牌）、`interpolated`（门牌插值）、`poi`、`road`、`community`、`town`、`district`、`city`、`province`。
`confidence` 综合了名称相似度、门牌是否命中、结果精度是否达到输入地址的精度；只给到区县的输入命中区县中心，置信度也是高的。

逆地理结果示例：

```json
{
  "formatted_address": "北京市朝阳区建国门外大街1号",
  "description": "中国国际贸易中心东北49米",
  "adcode": {"province": "110000", "city": "110100", "district": "110105", "town": null},
  "admin_source": "nearby",
  "road": {"name": "建国门外大街", "distance": 25.0},
  "pois": [{"name": "中国国际贸易中心", "distance": 48.7, "direction": "东北"}]
}
```

`admin_source`：`boundary`（边界点落面）、`boundary+nearby`（边界 + 周边要素细化到乡镇）、`nearby`（周边要素投票）、`centroid`（最近区县中心）。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `GEOCODER_DB` | `var/geocoder.db` | 数据文件 |
| `WORKERS` | 2（容器内） | uvicorn worker 数，每个约 100 MB |
| `TIANDITU_TK` | 空 | 天地图 key，填写后开启兜底 |
| `TIANDITU_DAILY_QUOTA` | 10000 | 兜底每日配额 |
| `GEOCODER_MIN_CONFIDENCE` | 0.6 | 低于该置信度才考虑兜底 |
| `GEOCODER_REVIEW_LOG` | 空 | 兜底结果写入的待审核 JSONL |
| `GEOCODER_CACHE_SIZE` | 20000 | 进程内 LRU 缓存条数 |
| `GEOCODER_FEEDBACK_LOG` | 空 | `/v1/feedback` 写入的 JSONL，建库时 `--memory` 导入 |

## 数据

数据来源与许可见 [docs/data-sources.md](docs/data-sources.md)，接入、更新与发布流程见 [docs/data.md](docs/data.md)。仓库内置：

- `data/admin/`：
  - 全国四级区划：统计用区划代码 2023 版 44,706 条，加上国家地名信息库 2025 年新增的区县；
  - 历史旧名 1,015 条与人工别名；
  - 区划中心点 38,428 个（源自 Overture/OSM，ODbL；覆盖地级 332/333、区县 2,851、乡镇 85%）；
- `data/sample/`：30 个知名地点、5 条道路，坐标为公开资料中的近似值（GCJ-02），**仅用于演示和测试**；
- `data/eval/gold_sample.csv`：评测集格式示例。

`scripts/sources/` 下的脚本可重新生成上述补充数据，并能按范围从 Overture 抽取道路、POI 和区划边界（`make sources`）。

门牌级效果取决于自有数据（订单地址 + 签收坐标、司机打点等），这一点与参考方案的判断一致：效果主要取决于数据，其次才是算法。

## 性能

`scripts/bench.py` 生成随机 POI/门牌（三分之一为纯门牌点）后建库压测，单线程、不走缓存，开发容器实测：

| 数据量 | 建库 | 数据文件 | 服务加载 | 地址解析 | 正向·POI | 正向·门牌 | 逆地理 |
|---|---|---|---|---|---|---|---|
| 20 万 | 15 s | 49 MB | 0.32 s | 0.08 ms | 0.36 ms | 0.26 ms | 0.13 ms |
| 100 万 | 79 s | 232 MB | 0.32 s | 0.08 ms | 0.54 ms（P95 1.2） | 0.32 ms（P95 0.57） | 0.27 ms（P95 0.49） |

百万级时进程内存峰值约 120 MB（含建库过程）。在线服务无网络往返，吞吐随 worker 数（CPU 核数）线性增加。合成数据的名称是随机拼的，命中率（99.7%）只说明检索链路正确，不代表真实地址的准确率；真实准确率请用业务金标准集评测：

```bash
cngeocoder --db var/geocoder.db eval data/eval/gold.csv --min-district-acc 0.95 --max-p90 500
```

## 目录

```
src/cngeocoder/   服务代码
data/admin/       区划词典、历史旧名、别名、中心点（出处见 data/README.md）
data/sample/      示例 POI / 道路
data/eval/        评测集示例
docs/             技术选型、数据接入
scripts/bench.py  性能基准
scripts/sources/  数据源抽取与生成脚本（areacodes、Overture）
tests/            单元与接口测试（合成数据）
```

## 已知限制与后续计划

- 乡级区划停在统计局 2023 版（县级以上已更新到 2025），乡级还需整理省级民政部门的半年发布；
- 未内置行政区边界和真实 POI/门牌：边界有合规要求，Overture 边界只建议内部使用；门牌需按 `docs/data.md` 导入自有数据；
- 不做输入联想和拼音检索，需要时单独加 suggest 服务；
- 规则解析对口语化、错别字地址召回有限，可按评测集难例接入 MGeo 做结构化与重排；
- 数据只读，更新靠重建（百万级约 1 分钟）；需要在线写入或超过数千万条时，按选型文档升级存储层。

## 数据来源与许可

- 区划代码：[modood/Administrative-divisions-of-China](https://github.com/modood/Administrative-divisions-of-China)（WTFPL），原始数据来自国家统计局统计用区划代码；
- 县级以上区划更新与历史旧名：[yescallop/areacodes](https://github.com/yescallop/areacodes)（CC0），原始数据来自国家地名信息库、民政部；
- 区划中心点：© OpenStreetMap contributors, Overture Maps Foundation（ODbL 1.0）；
- 坐标转换算法为业界公开的 GCJ-02 / BD-09 近似算法；
- 不包含、也不应导入抓取自商业图商的数据。
