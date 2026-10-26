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
import hashlib
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
from .admin import Admin, AdminIndex, load_admin_aliases, load_admin_csvs, load_alias_csv, native_code, short_names
from .boundary import BoundaryIndex
from .colloquial import abbreviations
from .geo import haversine
from .normalize import normalize
from .parser import AddressParser
from .store import SCHEMA, SCHEMA_VERSION, UID_INDEX, region_tokens
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
    memory: tuple[str, ...] = ()  # 历史确认结果（签收坐标、人工核验、兜底审核通过）CSV/JSONL
    edits: tuple[str, ...] = ()  # 在线编辑的变更日志（cngeocoder edits export 导出的 JSONL），建库最后重放
    coordsys: str = "wgs84"
    simplify: float = 0.0001  # 边界简化容差（度，约 10 米）


def _iter_geojson(path: str) -> Iterator[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    feats = data["features"] if data.get("type") == "FeatureCollection" else [data]
    yield from feats


def _iter_rows(path: str) -> Iterator[dict]:
    """CSV 或 GeoJSON 点要素统一成 dict 行。"""
    if path.endswith(".jsonl"):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    row = json.loads(line)
                    loc = row.get("location")
                    if isinstance(loc, dict):  # /v1/feedback 写出的格式
                        row.setdefault("lng", loc.get("lng"))
                        row.setdefault("lat", loc.get("lat"))
                    yield row
        return
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


def _uid(*parts) -> str:
    """没有显式 uid 的要素按内容生成稳定 uid：同一来源、同样内容的行每次建库 uid 不变，在线编辑可按 uid 重放。"""
    h = hashlib.sha1("\x1f".join("" if p is None else str(p) for p in parts).encode("utf-8")).hexdigest()
    return h[:16]


class PlaceIndexer:
    """把一行 POI/门牌输入或一条道路转换成 place / place_fts / place_rtree 三张表的行。

    离线建库和在线编辑共用，保证两条路径写出的数据完全一致（在线编辑的变更日志在下次建库时重放）。
    """

    def __init__(self, index: AdminIndex, bindex: BoundaryIndex | None = None, coordsys: str = "wgs84"):
        self.index = index
        self.by_code = index.by_code
        self.parser = AddressParser(index)
        self.bindex = bindex
        self.coordsys = coordsys

    def admin_code(self, row: dict, address: str | None, lng: float, lat: float) -> tuple[str | None, object]:
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

    def prefixes(self, code: str | None) -> tuple[str, ...]:
        """要素所属省、市、区县的名称与简称，用于生成“去掉城市前缀”的别名。"""
        if not code:
            return ()
        out: list[str] = []
        for level, a in self.index.lineage(code).items():
            if level <= 3:
                out.append(a.name)
                out.extend(short_names(a.name, level))
        return tuple(out)

    def point(self, row: dict, pid: int, source: str) -> tuple[tuple, tuple, tuple]:
        """POI / 门牌点。坐标缺失或非法时抛 ValueError。"""
        try:
            lng, lat = float(_pick(row, "lng")), float(_pick(row, "lat"))
        except (TypeError, ValueError) as e:
            raise ValueError("缺少坐标或坐标格式错误") from e
        if not (-180 <= lng <= 180 and -90 <= lat <= 90):
            raise ValueError("经纬度超出范围")
        lng, lat = coords.to_wgs84(lng, lat, _pick(row, "coordsys") or self.coordsys)
        name = normalize(_pick(row, "name") or "") or None
        address = _pick(row, "address")
        code, parsed = self.admin_code(row, address, lng, lat)
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
        raw_alias = _pick(row, "alias") or ""
        if isinstance(raw_alias, (list, tuple)):
            raw_alias = "|".join(str(a) for a in raw_alias)
        aliases = [normalize(a) for a in str(raw_alias).replace("；", "|").replace(";", "|").split("|")]
        alias = "|".join(a for a in aliases if a) or None
        abbr = "|".join(a for a in abbreviations(name, self.prefixes(code)) if a not in aliases) or None \
            if name else None
        address_n = normalize(address) if address else None
        uid = str(row.get("uid") or "").strip() or _uid(source, kind, name, road, road_no, round(lng, 6), round(lat, 6))
        place = (pid, kind, name, road, road_no, road_no_num, code, district, address_n, lng, lat, None, weight,
                 source, alias, abbr, uid)
        fts = (pid, region_tokens(code), index_text(name, *(alias or "").split("|"), *(abbr or "").split("|")),
               index_text(road, addr_detail))
        return place, fts, (pid, lng, lng, lat, lat)

    def road(self, props: dict, geometry: dict, pid: int, source: str) -> tuple[tuple, tuple, tuple]:
        """道路线要素。名称缺失或几何不是线时抛 ValueError。"""
        name = normalize(_pick(props, "name") or _pick(props, "road") or "")
        if not name:
            raise ValueError("道路缺少名称")
        if not geometry:
            raise ValueError("道路缺少几何")
        geom = _transform_geom(shape(geometry), props.get("coordsys") or self.coordsys)
        if geom.geom_type not in ("LineString", "MultiLineString"):
            raise ValueError("道路几何应为 LineString / MultiLineString")
        mid = shapely.line_interpolate_point(geom, 0.5, normalized=True) if geom.geom_type == "LineString" \
            else geom.representative_point()
        code, _ = self.admin_code(props, None, mid.x, mid.y)
        if code is None and self.bindex:
            code = self.bindex.locate(mid.x, mid.y)
        minx, miny, maxx, maxy = geom.bounds
        district = code[:6] if code and len(code) >= 6 else None
        wkb = shapely.to_wkb(geom)
        uid = str(props.get("uid") or "").strip() or _uid(source, "road", name, hashlib.sha1(wkb).hexdigest())
        place = (pid, "road", name, name, None, None, code, district, None, mid.x, mid.y, wkb,
                 float(_pick(props, "weight") or 0), source, None, None, uid)
        return place, (pid, region_tokens(code), index_text(name), ""), (pid, minx, maxx, miny, maxy)


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
        bindex = BoundaryIndex.from_wkb(
            (c, self.by_code[c].level, wkb) for c, wkb in boundaries.items()) if boundaries else None
        self.indexer = PlaceIndexer(index, bindex, coordsys=inp.coordsys)
        self.parser = self.indexer.parser

        next_id = 1
        for path in inp.places:
            next_id = self._load_places(db, path, next_id)
        for path in inp.roads:
            next_id = self._load_roads(db, path, next_id)
        self._index_uids(db)
        if inp.memory:
            self._load_memory(db, inp.memory)
        if inp.edits:
            from .edit import replay

            self.stats["edits"], next_id = replay(db, self.indexer, inp.edits, next_id)

        db.execute("INSERT INTO place_fts(place_fts) VALUES ('optimize')")
        common = self._common_tokens(db)
        meta = {
            "schema_version": SCHEMA_VERSION,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "coordsys": "wgs84",
            "admin_source": os.path.abspath(inp.admin_dir),
            "stats": json.dumps(self.stats, ensure_ascii=False),
            "common_tokens": json.dumps(common, ensure_ascii=False),
            "place_seq": str(next_id),  # 下一个要素 id；在线编辑只增不复用（全文索引里删除的旧 id 不能被新要素占用）
            "edit_version": "0",
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

    # ----------------------------------------------------------------- POI / 门牌 / 道路
    @staticmethod
    def _index_uids(db: sqlite3.Connection) -> None:
        """内容完全相同的行会生成相同 uid：按导入顺序给第 2 个起追加 ~2、~3（输入不变时结果稳定），再建唯一索引。"""
        db.execute("""
            WITH d AS (SELECT id, ROW_NUMBER() OVER (PARTITION BY uid ORDER BY id) AS k FROM place)
            UPDATE place SET uid = uid || '~' || (SELECT k FROM d WHERE d.id = place.id)
            WHERE id IN (SELECT id FROM d WHERE k > 1)""")
        try:
            db.execute(UID_INDEX)
        except sqlite3.IntegrityError as e:
            raise ValueError("要素 uid 重复：检查输入数据里显式给出的 uid 列") from e

    def _load_places(self, db: sqlite3.Connection, path: str, next_id: int) -> int:
        place_rows, fts_rows, rtree_rows = [], [], []
        n = 0
        source = os.path.basename(path)
        for row in _iter_rows(path):
            try:
                place, fts, rt = self.indexer.point(row, next_id, source)
            except ValueError:
                self.stats["skipped"] = self.stats.get("skipped", 0) + 1
                continue
            place_rows.append(place)
            fts_rows.append(fts)
            rtree_rows.append(rt)
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
        source = os.path.basename(path)
        for f in _iter_geojson(path):
            try:
                place, fts, rt = self.indexer.road(f.get("properties") or {}, f.get("geometry"), next_id, source)
            except ValueError:
                continue
            place_rows.append(place)
            fts_rows.append(fts)
            rtree_rows.append(rt)
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

    def _load_memory(self, db: sqlite3.Connection, paths: tuple[str, ...]) -> None:
        """历史确认结果：同一写法的多次确认取中位点，离散度大的（多处签收）降低可信度。"""
        groups: dict[str, list[tuple[float, float, float, str | None, str]]] = {}
        for path in paths:
            for row in _iter_rows(path):
                address = _pick(row, "address")
                try:
                    lng, lat = float(_pick(row, "lng")), float(_pick(row, "lat"))
                except (TypeError, ValueError):
                    continue
                if not address:
                    continue
                lng, lat = coords.to_wgs84(lng, lat, _pick(row, "coordsys") or self.inp.coordsys)
                w = float(row.get("count") or row.get("n") or 1)
                p = self.parser.parse(address, alternatives=0)
                adcode = p.district.code if p.district else p.region_code
                for key in p.memory_keys():
                    groups.setdefault(key, []).append((lng, lat, w, adcode, address))
        rows = []
        for key, pts in groups.items():
            lngs = sorted(x for x, _, _, _, _ in pts)
            lats = sorted(y for _, y, _, _, _ in pts)
            mlng, mlat = lngs[len(lngs) // 2], lats[len(lats) // 2]
            dists = sorted(haversine(mlng, mlat, x, y) for x, y, _, _, _ in pts)
            spread = dists[len(dists) // 2]
            n = int(round(sum(w for _, _, w, _, _ in pts)))
            adcodes = [a for _, _, _, a, _ in pts if a]
            adcode = max(set(adcodes), key=adcodes.count) if adcodes else None
            rows.append((key, mlng, mlat, n, round(spread, 1), adcode, pts[0][4]))
        db.executemany("INSERT OR REPLACE INTO memory VALUES (?,?,?,?,?,?,?)", rows)
        self.stats["memory"] = len(rows)

    @staticmethod
    def _flush(db, place_rows: list, fts_rows: list, rtree_rows: list) -> None:
        if not place_rows:
            return
        db.executemany(f"INSERT INTO place VALUES ({','.join('?' * 17)})", place_rows)
        db.executemany("INSERT INTO place_fts(rowid, region, name, addr) VALUES (?,?,?,?)", fts_rows)
        db.executemany("INSERT INTO place_rtree VALUES (?,?,?,?,?)", rtree_rows)
        place_rows.clear()
        fts_rows.clear()
        rtree_rows.clear()


def build(inp: BuildInputs) -> dict[str, int]:
    return Builder(inp).run()
