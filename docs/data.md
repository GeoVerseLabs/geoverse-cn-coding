# 数据接入与更新

各类数据的来源、许可与选用理由见 [data-sources.md](data-sources.md)。

服务的效果主要取决于数据。所有数据离线清洗后写进一个 SQLite 文件（默认 `var/geocoder.db`），在线服务读这个文件；日常的单条修正可以在线完成（见 [admin.md](admin.md)），修改记入变更记录，下次建库用 `--edits` 重放。

```bash
cngeocoder build \
  --admin-dir data/admin \                 # 行政区划、别名、中心点（已内置，自动加载）
  --boundaries bounds/district.geojson \   # 行政区边界，可多个
  --centroids  bounds/centroids.csv \      # 额外的中心点，覆盖内置值
  --places     poi/orders.csv poi/osm_points.geojson \
  --roads      roads/roads.geojson \
  --edits      var/edits.jsonl \           # 在线编辑的变更记录（cngeocoder edits export 导出），最后重放
  --coordsys   wgs84 \                     # 输入坐标系，行内 coordsys 列优先
  --out var/geocoder.next.db
```

## 1. 行政区划

内置 `data/admin/`：省、市、区县、乡镇四级共 44,706 条，来自统计用区划代码 2023 版（截至 2023-06-30）的社区整理版 [modood/Administrative-divisions-of-China](https://github.com/modood/Administrative-divisions-of-China)（WTFPL）。统计局自 2024 年起不再公开具体代码，后续应按参考方案改为跟随民政部（年度）和省级民政部门（半年）的发布同步。

| 文件 | 字段 |
|---|---|
| `provinces.csv` | `code,name` |
| `cities.csv` | `code,name,provinceCode` |
| `areas.csv` | `code,name,cityCode,provinceCode` |
| `streets.csv` | `code,name,areaCode,provinceCode,cityCode` |
| `extra.csv` | `code,name,level,parent,source`（2023 版之后新增的现行区划、港澳台；脚本生成） |
| `history_aliases.csv` | `alias,code,old_code,changed_year`（1,015 条历史旧名；脚本生成） |
| `aliases.csv` | `alias,code,note`（人工维护的别名、口语简称） |
| `centroids.csv` | `adcode,name,lng,lat,coordsys,method`（38,428 个区划中心点，ODbL；脚本生成） |

`extra.csv` 和 `history_aliases.csv` 由 `scripts/sources/areacodes.py` 从 yescallop/areacodes（CC0）生成，县级以上区划因此更新到 2025 年，崇文区、萧山市这类旧名也能解析。`centroids.csv` 由 `scripts/sources/overture.py admin` 从 Overture Maps 生成，让只到区县或乡镇的地址也有坐标。人工别名写在 `aliases.csv`；“县改区”“县级市改区”的旧称程序会自动生成。

## 2. 行政区边界（逆地理用）

GeoJSON FeatureCollection，属性里需要区划编码字段（`adcode` / `code` / `gb` / `区划代码`，6、9 或 12 位均可），几何为 Polygon / MultiPolygon。

- 推荐来源：天地图行政区划服务（lbs.tianditu.gov.cn），CGCS2000 坐标，按 WGS84 导入即可；
- 可选来源：`python scripts/sources/overture.py boundaries` 从 Overture（OSM）抽取省、地、县三级面，仅限内部判定使用（见 data-sources.md 第 5 节）；
- 有边界时，逆地理用点落面判定行政区，建库时也会给没有编码的 POI 自动补编码；
- 建库默认按 0.0001°（约 10 米）简化边界，`--simplify 0` 关闭；
- 没有边界也能运行：逆地理退化为“周边要素投票 → 最近区县中心点”，结果里 `admin_source` 会标明来源。

边界数据对外展示涉及审图，使用前请确认合规。

## 3. POI / 门牌

CSV 或 GeoJSON 点要素，字段名兼容英文、中文表头和 OSM 标签：

| 含义 | 可用字段名 | 说明 |
|---|---|---|
| 名称 | `name` / `名称` | 纯门牌点可留空 |
| 别名 | `alias` / `别名` | `|` 分隔，如 `北大|北京大学`，参与检索 |
| 类型 | `kind` / `type` | `poi` 或 `address`，缺省时有名称为 poi |
| 地址 | `address` / `地址` | 完整地址，没给编码/路名时用它解析 |
| 省市区 | `province` `city` `district` | 可选，拼在地址前一起解析 |
| 路名 | `road` / `street` / `addr:street` | |
| 门牌 | `road_no` / `housenumber` / `addr:housenumber` | “18号”“甲13号”“100弄” |
| 区划编码 | `adcode` / `code` | 有则直接用，最可靠 |
| 坐标 | `lng`/`lon`/`x`，`lat`/`y` | GeoJSON 取几何 |
| 坐标系 | `coordsys` | `wgs84` / `gcj02` / `bd09` / `cgcs2000` |
| 热度 | `weight` | 同名候选时的排序加分 |
| 标识 | `uid` | 可选，要素的稳定标识；没有时按“来源文件 + 内容”生成。在线编辑与重放按 uid 定位 |

所属行政区的确定顺序：显式 `adcode` → 解析地址文本 → 边界点落面（文本与坐标冲突时以坐标为准）。

价值最高的是自有业务数据：订单地址 + 签收坐标、司机打点、人工标注。建议按“地址文本 + 坐标”导出，交给建库流程解析和去噪。

## 4. 道路

GeoJSON LineString / MultiLineString，属性 `name`（必填）、`adcode`（可选，没有时用边界补）。道路用于“只到路名”的正向结果和逆地理的最近道路。

## 5. 从 Overture / OSM 导入（可选）

Overture 抽取脚本按范围读取，不需要下载全量数据：

```bash
pip install -e ".[sources]"
python scripts/sources/overture.py roads  --bbox 116.0,39.6,116.8,40.2 --out var/sources/roads_bj.geojson
python scripts/sources/overture.py places --bbox 116.0,39.6,116.8,40.2 --cjk-only --out var/sources/places_bj.csv
```

也可以直接处理 OSM 原始数据：

国内 OSM 城市主干道和 POI 较全、门牌稀疏，可以作为补充。用 [osmium-tool](https://osmcode.org/osmium-tool/) 转成 GeoJSON：

```bash
# POI 与门牌点
osmium tags-filter china-latest.osm.pbf n/name n/addr:housenumber -o pts.osm.pbf
osmium export pts.osm.pbf --geometry-types=point -o osm_points.geojson
# 道路
osmium tags-filter china-latest.osm.pbf w/highway -o roads.osm.pbf
osmium export roads.osm.pbf --geometry-types=linestring -o osm_roads.geojson

cngeocoder build --places osm_points.geojson --roads osm_roads.geojson --boundaries district.geojson ...
```

OSM 数据为 ODbL 许可，对外提供服务时需要署名。

## 6. 兜底结果回流

配置 `TIANDITU_TK` 后，本地置信度低于 `GEOCODER_MIN_CONFIDENCE`（默认 0.6）且请求比本地结果更精细时调用天地图。设置 `GEOCODER_REVIEW_LOG` 后，每条兜底结果写一行 JSONL：

```json
{"address": "…", "lng": 116.4, "lat": 39.9, "precision": "poi", "source": "tianditu", "adcode": "110105"}
```

人工审核通过的记录整理成 POI CSV（`address,lng,lat,adcode`）加入下一次建库，本地命中率会随业务持续提高。

### 历史确认结果

签收坐标、人工核验等“这个写法确认在哪”的记录，用 `--memory` 导入，口语化写法再来时直接命中：

```bash
cngeocoder build ... --memory var/feedback.jsonl data/confirmed.csv
```

- CSV 字段 `address,lng,lat[,coordsys,count]`；JSONL 为 `/v1/feedback` 的输出格式；
- 同一写法多次确认取中位点，离散度超过 300 m 的不采用。规则详见 [colloquial.md](colloquial.md) 第 3 节；
- 入库前去掉收件人、电话等个人信息，只保留地址文本和坐标。

## 7. 发布流程

1. 建到新文件：`cngeocoder build ... --out var/geocoder.next.db`（写临时文件，完成后原子改名）；
2. 跑评测卡口：`cngeocoder --db var/geocoder.next.db eval data/eval/gold.csv --min-district-acc 0.95 --max-p90 500`；
3. 通过后替换线上文件（`mv geocoder.next.db geocoder.db`）并滚动重启容器。服务以只读方式打开数据文件，重启前继续使用旧版本，切换期间不中断。

## 8. 金标准评测集

`data/eval/gold_sample.csv` 只是格式示例（坐标取自示例数据，指标必然很高）。生产评测集建议从真实业务抽样 1,000 条以上，按城市、地址类型（门牌 / POI / 农村地址）分层，坐标来自人工核验或签收点，字段：

```
address,city,expect_adcode,expect_lng,expect_lat,coordsys
```
