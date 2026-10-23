"""离线建库：把行政区划、边界、POI/门牌、道路清洗后写入一个 SQLite 文件。

输入格式尽量宽松，方便接入不同来源：
- 行政区划：modood/Administrative-divisions-of-China 格式 CSV（仓库已内置 2023 版）+ 别名表；
- 边界：GeoJSON（属性里带 adcode/code），可来自天地图行政区划服务或自有数据；
- 中心点：CSV（adcode,lng,lat）；
- POI/门牌：CSV 或 GeoJSON 点，字段兼容中文表头和 OSM 的 addr:* 标签；
- 道路：GeoJSON 线。
坐标系可以是 wgs84/gcj02/bd09/cgcs2000，入库统一转 WGS84。
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass

import shapely
from shapely.geometry import shape

from . import coords
from .admin import Admin, AdminIndex, load_admin_aliases, load_admin_csvs, load_alias_csv, native_code
from .boundary import BoundaryIndex
from .normalize import normalize
from .parser import AddressParser
from .store import SCHEMA, SCHEMA_VERSION, region_tokens
from .tokenizer import index_text

log = logging.getLogger(__name__)

# 字段名映射：兼容英文、中文表头和 OSM 标签
FIELD_ALIASES = {
    "name": ("name", "名称", "poi_name", "title"),
    "kind": ("kind", "type", "类型"),
    "address": ("address", "地址", "addr", "full_address"),
    "province": ("province", "省", "addr:province"),
    "city": ("city", "市", "addr:city"),
    "district": ("district", "区县", "区", "addr:district"),
    "road": ("road", "street", "路名", "道路", "addr:street"),
    "road_no": ("road_no", "housenumber", "门牌", "门牌号", "addr:housenumber"),
    "adcode": ("adcode", "code", "区划代码", "gb"),
    "lng": ("lng", "lon", "longitude", "x", "经度"),
    "lat": ("lat", "latitude", "y", "纬度"),
    "weight": ("weight", "热度", "importance"),
    "alias": ("alias", "别名", "aliases", "short_name"),
    "coordsys": ("coordsys", "坐标系"),
}


def _pick(row: dict, key: str):
    for k in FIELD_ALIASES[key]:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


@dataclass
class BuildInputs:
    admin_dir: str
    out: str
    aliases: tuple[str, ...] = ()  # 区划目录自带 aliases.csv / history_aliases.csv 之外的别名文件
    boundaries: tuple[str, ...] = ()
    centroids: tuple[str, ...] = ()
    places: tuple[str, ...] = ()
    roads: tuple[str, ...] = ()
    coordsys: str = "wgs84"
    simplify: float = 0.0001  # 边界简化容差（度，约 10 米）


def _iter_geojson(path: str) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    feats = data["features"] if data.get("type") == "FeatureCollection" else [data]
    yield from feats


def _iter_rows(path: str) -> Iterator[dict]:
    """CSV 或 GeoJSON 点要素统一成 dict 行。"""
    if path.endswith((".geojson", ".json")):
        for f in _iter_geojson(path):
            props = dict(f.get("properties") or {})
            g = f.get("geometry")
            if g and g.get("type") == "Point":
                props.setdefault("lng", g["coordinates"][0])
                props.setdefault("lat", g["coordinates"][1])
            yield props
        return
    with open(path, encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(line for line in f if not line.startswith("#"))


def _transform_geom(geom, src: str):
    if coords.CoordSys.parse(src).base is coords.CoordSys.WGS84:
        return geom
    return shapely.transform(geom, lambda a: coords.to_wgs84_array(a, src))


class Builder:
    def __init__(self, inp: BuildInputs):
        self.inp = inp
        self.stats: dict[str, int] = {}

    def run(self) -> dict[str, int]:
        t0 = time.time()
        inp = self.inp
        tmp = inp.out + ".tmp"
        if os.path.exists(tmp):
            os.remove(tmp)
        os.makedirs(os.path.dirname(os.path.abspath(inp.out)), exist_ok=True)
        db = sqlite3.connect(tmp)
        db.execute("PRAGMA journal_mode = OFF")
        db.execute("PRAGMA synchronous = OFF")
        db.executescript(SCHEMA)

        admins = load_admin_csvs(inp.admin_dir)
        aliases = load_admin_aliases(inp.admin_dir)
        for path in inp.aliases:
            aliases.extend(load_alias_csv(path))
        self.by_code = {a.code: a for a in admins}
        # 中心点优先级：显式 --centroids > 区划目录自带 centroids.csv > 边界几何中心
        default_centroids = os.path.join(inp.admin_dir, "centroids.csv")
        if os.path.exists(default_centroids):
            self._load_centroids((default_centroids,))
        boundaries = self._load_boundaries(admins)
        self._load_centroids(inp.centroids)
        self.stats["admin_with_coord"] = sum(1 for a in admins if a.lng is not None)
        db.executemany("INSERT INTO admin VALUES (?,?,?,?,?,?,?)",
                       [(a.code, a.name, a.level, a.parent, a.lng, a.lat, boundaries.get(a.code)) for a in admins])
        db.executemany("INSERT INTO admin_alias VALUES (?,?,?)", aliases)
        self.stats["admin"] = len(admins)
        self.stats["boundary"] = len(boundaries)

        index = AdminIndex(admins, aliases)
        self.parser = AddressParser(index)
        self.bindex = BoundaryIndex.from_wkb(
            (c, self.by_code[c].level, wkb) for c, wkb in boundaries.items()) if boundaries else None

        next_id = 1
        for path in inp.places:
            next_id = self._load_places(db, path, next_id)
        for path in inp.roads:
            next_id = self._load_roads(db, path, next_id)

        db.execute("INSERT INTO place_fts(place_fts) VALUES ('optimize')")
        common = self._common_tokens(db)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "coordsys": "wgs84",
            "admin_source": os.path.abspath(inp.admin_dir),
            "stats": json.dumps(self.stats, ensure_ascii=False),
            "common_tokens": json.dumps(common, ensure_ascii=False),
        }
        db.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
        db.commit()
        db.execute("ANALYZE")
        db.execute("VACUUM")
        db.close()
        os.replace(tmp, inp.out)  # 原子替换，在线服务滚动重启即可切换数据
        self.stats["seconds"] = round(time.time() - t0, 1)
        log.info("build done: %s", self.stats)
        return self.stats

    # ----------------------------------------------------------------- 行政区
    def _load_boundaries(self, admins: list[Admin]) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for path in self.inp.boundaries:
            for f in _iter_geojson(path):
                props = f.get("properties") or {}
                raw = _pick(props, "adcode")
                if raw is None or not f.get("geometry"):
                    continue
                try:
                    code = native_code(raw)
                except ValueError:
                    continue
                a = self.by_code.get(code)
                if a is None:
                    continue
                geom = _transform_geom(shape(f["geometry"]), props.get("coordsys") or self.inp.coordsys)
                if self.inp.simplify:
                    geom = geom.simplify(self.inp.simplify, preserve_topology=True)
                if not geom.is_valid:
                    geom = shapely.make_valid(geom)
                c = geom.centroid
                if not geom.contains(c):
                    c = geom.representative_point()
                if a.lng is None:
                    a.lng, a.lat = round(c.x, 6), round(c.y, 6)
                out[code] = shapely.to_wkb(geom)
        return out

    def _load_centroids(self, paths: tuple[str, ...]) -> None:
        for path in paths:
            for row in _iter_rows(path):
                try:
                    code = native_code(_pick(row, "adcode"))
                    lng, lat = float(_pick(row, "lng")), float(_pick(row, "lat"))
                except (TypeError, ValueError):
                    continue
                a = self.by_code.get(code)
                if a is None:
                    continue
                cs = _pick(row, "coordsys") or self.inp.coordsys
                a.lng, a.lat = (round(v, 6) for v in coords.to_wgs84(lng, lat, cs))

    def _admin_code(self, row: dict, address: str | None, lng: float, lat: float) -> tuple[str | None, object]:
        """确定要素所属行政区：显式编码 > 解析地址 > 边界点落面。返回 (编码, 解析结果)。"""
        raw = _pick(row, "adcode")
        parsed = None
        if raw:
            try:
                c = native_code(raw)
                if c in self.by_code:
                    return c, None
            except ValueError:
                pass
        text = "".join(v for v in (_pick(row, "province"), _pick(row, "city"), _pick(row, "district"), address) if v)
        cand = None
        if text:
            parsed = self.parser.parse(text, alternatives=0)
            a = parsed.town or parsed.district
            cand = a.code if a is not None else parsed.region_code
        pip = self.bindex.locate(lng, lat) if self.bindex else None
        if pip and cand:
            # 文本和坐标一致时取更细的一级；冲突时相信坐标
            return (cand if cand.startswith(pip) else pip), parsed
        return pip or cand, parsed

    # ----------------------------------------------------------------- POI / 门牌
    def _load_places(self, db: sqlite3.Connection, path: str, next_id: int) -> int:
        place_rows, fts_rows, rtree_rows = [], [], []
        n = 0
        for row in _iter_rows(path):
            try:
                lng, lat = float(_pick(row, "lng")), float(_pick(row, "lat"))
            except (TypeError, ValueError):
                self.stats["skipped"] = self.stats.get("skipped", 0) + 1
                continue
            lng, lat = coords.to_wgs84(lng, lat, _pick(row, "coordsys") or self.inp.coordsys)
            name = normalize(_pick(row, "name") or "") or None
            address = _pick(row, "address")
            code, parsed = self._admin_code(row, address, lng, lat)
            road = normalize(_pick(row, "road") or "") or None
            road_no = normalize(_pick(row, "road_no") or "") or None
            if parsed is None and address and (road is None or name is None):
                parsed = self.parser.parse(address, alternatives=0)
            if parsed is not None:
                road = road or parsed.road
                road_no = road_no or parsed.road_no
                if name is None and parsed.poi and not road_no:
                    name = parsed.poi
            if road_no and road_no.isdigit():
                road_no += "号"
            road_no_num = None
            if road_no:
                digits = "".join(ch if ch.isdigit() else " " for ch in road_no).split()
                road_no_num = int(digits[0]) if digits else None
            kind = (_pick(row, "kind") or ("address" if road_no and not name else "poi")).lower()
            district = code[:6] if code and len(code) >= 6 else None
            weight = float(_pick(row, "weight") or 0)
            addr_detail = parsed.detail if parsed is not None else (normalize(address) if address else "")
            aliases = [normalize(a) for a in str(_pick(row, "alias") or "").replace("；", "|").replace(";", "|").split("|")]
            alias = "|".join(a for a in aliases if a) or None
            place_rows.append((next_id, kind, name, road, road_no, road_no_num, code, district,
                               normalize(address) if address else None, lng, lat, None, weight,
                               os.path.basename(path), alias))
            fts_rows.append((next_id, region_tokens(code), index_text(name, *(alias or "").split("|")),
                             index_text(road, addr_detail)))
            rtree_rows.append((next_id, lng, lng, lat, lat))
            next_id += 1
            n += 1
            if len(place_rows) >= 20000:
                self._flush(db, place_rows, fts_rows, rtree_rows)
        self._flush(db, place_rows, fts_rows, rtree_rows)
        self.stats["places"] = self.stats.get("places", 0) + n
        return next_id

    def _load_roads(self, db: sqlite3.Connection, path: str, next_id: int) -> int:
        place_rows, fts_rows, rtree_rows = [], [], []
        n = 0
        for f in _iter_geojson(path):
            props = f.get("properties") or {}
            name = normalize(_pick(props, "name") or _pick(props, "road") or "")
            if not name or not f.get("geometry"):
                continue
            geom = _transform_geom(shape(f["geometry"]), props.get("coordsys") or self.inp.coordsys)
            if geom.geom_type not in ("LineString", "MultiLineString"):
                continue
            mid = shapely.line_interpolate_point(geom, 0.5, normalized=True) if geom.geom_type == "LineString" \
                else geom.representative_point()
            code, _ = self._admin_code(props, None, mid.x, mid.y)
            if code is None and self.bindex:
                code = self.bindex.locate(mid.x, mid.y)
            minx, miny, maxx, maxy = geom.bounds
            district = code[:6] if code and len(code) >= 6 else None
            place_rows.append((next_id, "road", name, name, None, None, code, district, None,
                               mid.x, mid.y, shapely.to_wkb(geom), float(_pick(props, "weight") or 0),
                               os.path.basename(path), None))
            fts_rows.append((next_id, region_tokens(code), index_text(name), ""))
            rtree_rows.append((next_id, minx, maxx, miny, maxy))
            next_id += 1
            n += 1
        self._flush(db, place_rows, fts_rows, rtree_rows)
        self.stats["roads"] = self.stats.get("roads", 0) + n
        return next_id

    def _common_tokens(self, db: sqlite3.Connection) -> list[str]:
        """高频 token（如“大厦”“小区”“大道”）倒排很长，查询时剔除可大幅降低检索开销。"""
        total = self.stats.get("places", 0) + self.stats.get("roads", 0)
        min_df = max(2000, int(total * 0.003))
        db.execute("CREATE VIRTUAL TABLE temp.fts_vocab USING fts5vocab(main, place_fts, 'row')")
        rows = db.execute("SELECT term FROM temp.fts_vocab WHERE doc >= ? AND term NOT LIKE 'r%' "
                          "ORDER BY doc DESC LIMIT 2000", (min_df,)).fetchall()
        db.execute("DROP TABLE temp.fts_vocab")
        self.stats["common_tokens"] = len(rows)
        return [r[0] for r in rows]

    @staticmethod
    def _flush(db, place_rows: list, fts_rows: list, rtree_rows: list) -> None:
        if not place_rows:
            return
        db.executemany("INSERT INTO place VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", place_rows)
        db.executemany("INSERT INTO place_fts(rowid, region, name, addr) VALUES (?,?,?,?)", fts_rows)
        db.executemany("INSERT INTO place_rtree VALUES (?,?,?,?,?)", rtree_rows)
        place_rows.clear()
        fts_rows.clear()
        rtree_rows.clear()


def build(inp: BuildInputs) -> dict[str, int]:
    return Builder(inp).run()
