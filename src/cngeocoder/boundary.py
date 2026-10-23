"""行政区边界点落面（Shapely 2 / GEOS STRtree，常驻内存）。

区县级全国约 2900 个面、乡镇约 4 万个面，STRtree 查询在微秒级，不需要 PostGIS。
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry.base import BaseGeometry


class BoundaryIndex:
    def __init__(self, items: Iterable[tuple[str, int, BaseGeometry]]):
        by_level: dict[int, tuple[list[str], list[BaseGeometry]]] = {}
        for code, level, geom in items:
            if geom is None or geom.is_empty:
                continue
            codes, geoms = by_level.setdefault(level, ([], []))
            codes.append(code)
            geoms.append(geom)
        self.levels: dict[int, tuple[np.ndarray, list[BaseGeometry], STRtree]] = {}
        for level, (codes, geoms) in by_level.items():
            for g in geoms:
                shapely.prepare(g)
            self.levels[level] = (np.array(codes, dtype=object), geoms, STRtree(geoms))

    @classmethod
    def from_wkb(cls, rows: Iterable[tuple[str, int, bytes]]) -> BoundaryIndex:
        return cls((code, level, shapely.from_wkb(wkb)) for code, level, wkb in rows)

    def __bool__(self) -> bool:
        return bool(self.levels)

    @property
    def finest_level(self) -> int:
        return max(self.levels) if self.levels else 0

    def locate(self, lng: float, lat: float) -> str | None:
        """返回包含该点的最细一级行政区编码。"""
        pt = shapely.Point(lng, lat)
        for level in sorted(self.levels, reverse=True):
            codes, geoms, tree = self.levels[level]
            idx = tree.query(pt, predicate="intersects")
            if len(idx):
                return codes[int(idx[0])]
        return None

    def locate_many(self, lngs: np.ndarray, lats: np.ndarray) -> list[str | None]:
        """批量点落面，建库时给没有区划编码的数据补编码。"""
        out: list[str | None] = [None] * len(lngs)
        if len(lngs) == 0:
            return out
        pts = shapely.points(np.asarray(lngs, dtype=float), np.asarray(lats, dtype=float))
        for level in sorted(self.levels, reverse=True):
            codes, _, tree = self.levels[level]
            pending = np.array([i for i, v in enumerate(out) if v is None], dtype=int)
            if len(pending) == 0:
                break
            pairs = tree.query(pts[pending], predicate="intersects")
            for pi, gi in zip(pairs[0], pairs[1], strict=True):
                i = int(pending[pi])
                if out[i] is None:
                    out[i] = codes[int(gi)]
        return out
