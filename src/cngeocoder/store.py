"""SQLite 存储层：一个 .db 文件装下行政区、POI/门牌/道路、全文索引和空间索引。

- ``place_fts``：FTS5 倒排（region 列放行政区编码 token，name/addr 列放中文 bigram）；
- ``place_rtree``：R*Tree 空间索引，逆地理找周边；
- 在线服务以只读 + immutable 方式打开，多线程/多进程并发读无锁。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import dataclass

SCHEMA_VERSION = "1"  # 改表结构时递增，服务启动会校验

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE admin (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    level INTEGER NOT NULL,
    parent TEXT,
    lng REAL,
    lat REAL,
    geom BLOB
);
CREATE TABLE admin_alias (alias TEXT NOT NULL, code TEXT NOT NULL);
CREATE TABLE place (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,            -- poi | address | road
    name TEXT,
    road TEXT,
    road_no TEXT,
    road_no_num INTEGER,
    adcode TEXT,                   -- 最细行政区原生编码（区县 6 位或乡镇 9 位）
    district_code TEXT,
    address TEXT,
    lng REAL NOT NULL,             -- WGS84
    lat REAL NOT NULL,
    geom BLOB,                     -- 道路线 WKB（WGS84）
    weight REAL NOT NULL DEFAULT 0,
    source TEXT,
    alias TEXT                     -- 别名/简称，'|' 分隔（北大|北京大学）
);
CREATE INDEX idx_place_road ON place (road, district_code);
CREATE VIRTUAL TABLE place_fts USING fts5 (region, name, addr, content='');
CREATE VIRTUAL TABLE place_rtree USING rtree (id, min_lng, max_lng, min_lat, max_lat);
"""


@dataclass(slots=True)
class Place:
    id: int
    kind: str
    name: str | None
    road: str | None
    road_no: str | None
    road_no_num: int | None
    adcode: str | None
    district_code: str | None
    address: str | None
    lng: float
    lat: float
    weight: float
    alias: str | None = None
    geom: bytes | None = None

    @property
    def names(self) -> list[str]:
        out = [self.name] if self.name else []
        if self.alias:
            out.extend(a for a in self.alias.split("|") if a)
        return out

    @classmethod
    def from_row(cls, r: sqlite3.Row | tuple) -> Place:
        return cls(*r)


_PLACE_COLS = "p.id, p.kind, p.name, p.road, p.road_no, p.road_no_num, p.adcode, p.district_code, " \
              "p.address, p.lng, p.lat, p.weight, p.alias"
_NCOLS = 13


def _region_clause(region: str | None) -> tuple[str, list]:
    """区县级用等值（可走联合索引），市/省级用前缀匹配。"""
    if not region:
        return "", []
    if len(region) >= 6:
        return " AND p.district_code = ?", [region[:6]]
    return " AND p.district_code LIKE ?", [region + "%"]


def region_tokens(code: str | None) -> str:
    """行政区编码 → FTS token：'110105001' → 'r11 r1101 r110105 r110105001'。"""
    if not code:
        return ""
    return " ".join("r" + code[:n] for n in (2, 4, 6, 9) if len(code) >= n)


class Store:
    """只读访问。每个线程一个连接（sqlite3 连接不能跨线程共享）。"""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        # 打开一次，确认文件存在且版本匹配
        meta = self.meta()
        if meta.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError(f"数据文件版本不匹配: {meta.get('schema_version')} != {SCHEMA_VERSION}")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(f"file:{self.path}?mode=ro&immutable=1", uri=True, check_same_thread=False)
            c.execute("PRAGMA query_only = 1")
            c.execute("PRAGMA mmap_size = 268435456")
            c.execute("PRAGMA cache_size = -65536")
            self._local.conn = c
        return c

    def meta(self) -> dict[str, str]:
        return dict(self.conn().execute("SELECT key, value FROM meta"))

    def common_tokens(self) -> frozenset[str]:
        raw = self.meta().get("common_tokens")
        return frozenset(json.loads(raw)) if raw else frozenset()

    # ------------------------------------------------------------ 行政区
    def iter_admins(self) -> Iterator[tuple]:
        yield from self.conn().execute("SELECT code, name, level, parent, lng, lat FROM admin")

    def iter_aliases(self) -> Iterator[tuple[str, str]]:
        yield from self.conn().execute("SELECT alias, code FROM admin_alias")

    def iter_boundaries(self) -> Iterator[tuple[str, int, bytes]]:
        yield from self.conn().execute("SELECT code, level, geom FROM admin WHERE geom IS NOT NULL")

    def road_names(self) -> set[str]:
        rows = self.conn().execute("SELECT DISTINCT road FROM place WHERE road IS NOT NULL AND road != ''")
        return {r[0] for r in rows}

    # ------------------------------------------------------------ 正向检索
    def search(self, expr: str, region: str | None = None, limit: int = 30) -> list[tuple[Place, float]]:
        """FTS5 全文检索，返回 (Place, bm25)。bm25 越小越相关。"""
        q = expr if not region else f'region : "r{region}" AND ({expr})'
        sql = (f"SELECT {_PLACE_COLS}, bm25(place_fts, 0.0, 4.0, 1.0) AS s "
               "FROM place_fts JOIN place p ON p.id = place_fts.rowid "
               "WHERE place_fts MATCH ? ORDER BY s LIMIT ?")
        try:
            rows = self.conn().execute(sql, (q, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(Place.from_row(r[:_NCOLS]), r[_NCOLS]) for r in rows]

    def by_road(self, road: str, region: str | None = None) -> list[Place]:
        """按路名精确取所有带门牌/道路的要素（idx_place_road 索引）。"""
        sql = f"SELECT {_PLACE_COLS} FROM place p WHERE p.road = ?"
        clause, args = _region_clause(region)
        return [Place.from_row(r) for r in self.conn().execute(sql + clause, [road, *args])]

    def roads_named(self, name: str, region: str | None = None) -> list[Place]:
        # 道路要素的 road 列等于其名称，走 idx_place_road 索引
        sql = f"SELECT {_PLACE_COLS}, p.geom FROM place p WHERE p.road = ? AND p.kind = 'road'"
        clause, args = _region_clause(region)
        return [Place.from_row(r) for r in self.conn().execute(sql + clause, [name, *args])]

    # ------------------------------------------------------------ 空间查询
    def in_bbox(self, min_lng: float, min_lat: float, max_lng: float, max_lat: float,
                kinds: tuple[str, ...] | None = None, limit: int = 2000) -> list[Place]:
        sql = (f"SELECT {_PLACE_COLS}, p.geom FROM place_rtree r JOIN place p ON p.id = r.id "
               "WHERE r.max_lng >= ? AND r.min_lng <= ? AND r.max_lat >= ? AND r.min_lat <= ?")
        args: list = [min_lng, max_lng, min_lat, max_lat]
        if kinds:
            sql += f" AND p.kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        sql += " LIMIT ?"
        args.append(limit)
        return [Place.from_row(r) for r in self.conn().execute(sql, args)]

    def counts(self) -> dict[str, int]:
        c = self.conn()
        out = {k: v for k, v in c.execute("SELECT kind, COUNT(*) FROM place GROUP BY kind")}
        out["admin"] = c.execute("SELECT COUNT(*) FROM admin").fetchone()[0]
        out["admin_with_coord"] = c.execute("SELECT COUNT(*) FROM admin WHERE lng IS NOT NULL").fetchone()[0]
        out["admin_with_boundary"] = c.execute("SELECT COUNT(*) FROM admin WHERE geom IS NOT NULL").fetchone()[0]
        return out
