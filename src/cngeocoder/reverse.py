"""逆地理编码：坐标 → 行政区 + 最近道路/门牌 + 周边 POI + 方位描述。

行政区判定按可用数据逐级退化：
1. 有边界：STRtree 点落面（最准）；
2. 无边界：周边 3 km 内已知要素按距离加权投票；
3. 都没有：最近的区县中心点（仅供参考，边界附近可能错）。
"""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache

import numpy as np
import shapely

from .admin import LEVEL_DISTRICT, AdminChain, AdminIndex
from .boundary import BoundaryIndex
from .cache import LRUCache
from .geo import LocalProjection, bbox_around, bearing, direction_cn, haversine
from .parser import ParsedAddress
from .store import Place, Store


class ReverseGeocoder:
    def __init__(self, store: Store, admin: AdminIndex, boundaries: BoundaryIndex | None = None,
                 cache_size: int = 50000):
        self.store, self.admin, self.boundaries = store, admin, boundaries
        self.cache = LRUCache(cache_size)
        ds = [a for a in admin.by_code.values() if a.level == LEVEL_DISTRICT and a.lng is not None]
        self._dc_codes = [a.code for a in ds]
        self._dc_xy = np.array([[a.lng, a.lat] for a in ds], dtype=float).reshape(-1, 2)

    def reverse(self, lng: float, lat: float, radius: float = 500, poi_limit: int = 5) -> dict:
        radius = max(10.0, min(float(radius), 3000.0))
        key = (round(lng, 5), round(lat, 5), radius, poi_limit)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        res = self._reverse(lng, lat, radius, poi_limit)
        self.cache.put(key, res)
        return res

    # ------------------------------------------------------------------ 内部
    def _reverse(self, lng: float, lat: float, radius: float, poi_limit: int) -> dict:
        search_r = max(radius, 3000.0) if not self.boundaries else radius
        near = self._nearby(lng, lat, search_r)
        code, source = self._admin_code(lng, lat, near)
        levels = self.admin.lineage(code) if code else {}
        view = ParsedAddress(raw="", normalized="", chain=AdminChain(levels=levels, score=1.0) if levels else None)
        comps = view.admin_names()
        adcodes = view.adcodes()

        in_r = [(d, p) for d, p in near if d <= radius]
        address = next(({"road": p.road, "road_no": p.road_no, "distance": round(d, 1),
                         "location": {"lng": p.lng, "lat": p.lat}}
                        for d, p in in_r if p.road and p.road_no and d <= min(radius, 100)), None)
        road = self._nearest_road(lng, lat, radius)
        pois = []
        for d, p in in_r:
            if p.kind != "poi" or not p.name:
                continue
            pois.append({"id": p.id, "name": p.name, "distance": round(d, 1),
                         "direction": direction_cn(bearing(p.lng, p.lat, lng, lat)),
                         "address": p.address, "location": {"lng": p.lng, "lat": p.lat}})
            if len(pois) >= poi_limit:
                break

        comps["road"] = address["road"] if address else (road["name"] if road else None)
        comps["road_no"] = address["road_no"] if address else None
        parts: list[str] = []
        for k in ("province", "city", "district", "town", "road", "road_no"):
            v = comps.get(k)
            if v and (not parts or parts[-1] != v):
                parts.append(v)
        description = None
        if pois:
            p0 = pois[0]
            description = f"{p0['name']}附近" if p0["distance"] < 20 else \
                f"{p0['name']}{p0['direction']}{int(round(p0['distance']))}米"
        return {
            "status": "ok" if levels or pois or road else "not_found",
            "location": {"lng": lng, "lat": lat},
            "formatted_address": "".join(parts),
            "description": description,
            "components": comps,
            "adcode": adcodes,
            "admin_source": source,
            "address": address,
            "road": road,
            "pois": pois,
        }

    def _nearby(self, lng: float, lat: float, radius: float) -> list[tuple[float, Place]]:
        rows = self.store.in_bbox(*bbox_around(lng, lat, radius), kinds=("poi", "address"))
        out = [(haversine(lng, lat, r.lng, r.lat), r) for r in rows]
        out = [x for x in out if x[0] <= radius]
        out.sort(key=lambda x: x[0])
        return out

    def _admin_code(self, lng: float, lat: float, near: list[tuple[float, Place]]) -> tuple[str | None, str]:
        if self.boundaries:
            code = self.boundaries.locate(lng, lat)
            if code is not None:
                # 边界只到区县时，用 1 km 内同区县要素的乡镇编码细化
                if len(code) < 9:
                    town = self._vote(near, within=1000, prefix=code, min_len=9)
                    if town:
                        return town, "boundary+nearby"
                return code, "boundary"
        code = self._vote(near, within=3000)
        if code:
            return code, "nearby"
        if len(self._dc_codes):
            d = np.hypot((self._dc_xy[:, 0] - lng) * np.cos(np.radians(lat)), self._dc_xy[:, 1] - lat)
            i = int(np.argmin(d))
            if d[i] * 111_000 < 50_000:
                return self._dc_codes[i], "centroid"
        return None, "none"

    @staticmethod
    def _vote(near: list[tuple[float, Place]], within: float, prefix: str = "", min_len: int = 6) -> str | None:
        score: dict[str, float] = defaultdict(float)
        for d, p in near:
            if d > within:
                break
            c = p.adcode
            if not c or len(c) < min_len or not c.startswith(prefix):
                continue
            score[c[:min_len] if min_len == 9 else c] += 1.0 / (d + 50.0)
        if not score:
            return None
        if min_len == 6:
            # 先按区县投票，再在胜出区县内取乡镇
            dist: dict[str, float] = defaultdict(float)
            for c, s in score.items():
                dist[c[:6]] += s
            win = max(dist, key=dist.get)
            towns = {c: s for c, s in score.items() if c.startswith(win) and len(c) >= 9}
            return max(towns, key=towns.get) if towns else win
        return max(score, key=score.get)

    def _nearest_road(self, lng: float, lat: float, radius: float) -> dict | None:
        rows = self.store.in_bbox(*bbox_around(lng, lat, radius), kinds=("road",), limit=500)
        if not rows:
            return None
        proj = LocalProjection(lng, lat)
        origin = shapely.Point(0.0, 0.0)
        best: tuple[float, Place] | None = None
        for r in rows:
            if r.geom is None:
                continue
            g = shapely.transform(_wkb(r.geom), lambda a: np.column_stack(proj.forward(a[:, 0], a[:, 1])))
            d = g.distance(origin)
            if d <= radius and (best is None or d < best[0]):
                best = (d, r)
        if best is None:
            return None
        return {"id": best[1].id, "name": best[1].name, "distance": round(best[0], 1)}


@lru_cache(maxsize=4096)
def _wkb(b: bytes):
    return shapely.from_wkb(b)
