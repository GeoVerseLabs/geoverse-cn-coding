# 内置数据出处

详细说明（选源原则、覆盖率、校验方法、许可义务）见 [docs/data-sources.md](../docs/data-sources.md)。

| 文件 | 内容 | 来源 | 许可 |
|---|---|---|---|
| `admin/provinces.csv`、`cities.csv`、`areas.csv`、`streets.csv` | 省、地、县、乡四级区划代码（截至 2023-06-30） | 国家统计局统计用区划代码，经 [modood/Administrative-divisions-of-China](https://github.com/modood/Administrative-divisions-of-China) 整理 | WTFPL |
| `admin/extra.csv` | 2023 版之后新增的现行区划，以及港澳台省级 | 国家地名信息库 2025 年代码，经 [yescallop/areacodes](https://github.com/yescallop/areacodes) 整理；GB/T 2260 | CC0 1.0 |
| `admin/history_aliases.csv` | 区划历史旧名 → 现行代码 | 民政部历年县级以上区划代码与变更情况，经 yescallop/areacodes 整理 | CC0 1.0 |
| `admin/aliases.csv` | 人工维护的别名 | 依据民政部公开的区划变更整理 | 同仓库 |
| `admin/centroids.csv` | 区划中心点（WGS84） | [Overture Maps](https://overturemaps.org/) divisions 2026-09-23.1，源自 OpenStreetMap | **ODbL 1.0**，© OpenStreetMap contributors, Overture Maps Foundation |
| `sample/places.csv`、`sample/roads.geojson` | 演示用地点和道路 | 公开资料中的近似坐标，人工整理 | 同仓库，仅演示 |
| `eval/gold_sample.csv` | 评测集格式示例 | 取自示例数据 | 同仓库 |

`admin/centroids.csv` 是 ODbL 派生数据库：再分发须保持 ODbL；对外提供服务时须署名 OpenStreetMap 贡献者。这些义务只约束该文件，不影响仓库代码的许可。

重新生成（需网络与 `pip install -e ".[sources]"`）：

```bash
python scripts/sources/areacodes.py        # extra.csv、history_aliases.csv
python scripts/sources/overture.py admin   # centroids.csv，匹配报告写到 var/sources/
```
