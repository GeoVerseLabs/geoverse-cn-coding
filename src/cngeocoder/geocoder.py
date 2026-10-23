"""正向地理编码：地址 → 坐标。

对解析出的每种行政区解释（通常 1 个，同名区县时 2~3 个）生成候选：
门牌精确 → 门牌插值 → POI → 道路 → 乡镇/区县/城市/省中心点，
统一打分后取最高者；置信度不足时走兜底服务（天地图等），并把兜底结果记入待审核日志。
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from dataclasses import dataclass, field, replace

from .admin import LEVEL_CITY, LEVEL_DISTRICT, LEVEL_PROVINCE, LEVEL_TOWN, AdminChain, AdminIndex
from .cache import LRUCache
from .geo import haversine
from .parser import AddressParser, ParsedAddress, join_parts
from .store import Place, Store
from .tokenizer import match_expr, name_similarity

log = logging.getLogger(__name__)

# 精度等级，越靠前越精细
PRECISION_RANK = {"door": 0, "interpolated": 1, "poi": 2, "road": 3, "community": 4, "town": 5,
                  "district": 6, "city": 7, "province": 8, "none": 9}
_REQUESTED_RANK = {"building": 0, "door": 0, "poi": 2, "road": 3, "community": 4, "town": 5,
                   "district": 6, "city": 7, "province": 8, "none": 9}
_ADMIN_PRECISION = {LEVEL_TOWN: "town", LEVEL_DISTRICT: "district", LEVEL_CITY: "city", LEVEL_PROVINCE: "province"}
_NO_CORE = re.compile(r"(号院|号|弄)$")


def _no_core(no: str | None) -> str | None:
    return _NO_CORE.sub("", no) if no else None


@dataclass
class Candidate:
    lng: float
    lat: float
    precision: str
    score: float
    parsed: ParsedAddress
    place: Place | None = None
    source: str = "local"
    detail: dict = field(default_factory=dict)


class Geocoder:
    def __init__(self, store: Store, admin: AdminIndex, parser: AddressParser, fallback=None,
                 cache_size: int = 20000, min_confidence: float = 0.6, review_log: str | None = None):
        self.store, self.admin, self.parser = store, admin, parser
        self.fallback = fallback
        self.min_confidence = min_confidence
        self.cache = LRUCache(cache_size)
        self.review_log = review_log
        self._review_lock = threading.Lock()
        self.common = store.common_tokens()

    # ------------------------------------------------------------------ 入口
    def geocode(self, address: str, city: str | None = None, focus: tuple[float, float] | None = None,
                limit: int = 1, use_fallback: bool = True) -> dict:
        key = (address, city, focus and (round(focus[0], 3), round(focus[1], 3)), limit, use_fallback)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        res = self._geocode(address, city, focus, limit, use_fallback)
        self.cache.put(key, res)
        return res

    def _geocode(self, address, city, focus, limit, use_fallback) -> dict:
        parsed = self.parser.parse(address, city=city, alternatives=4)
        interps = [parsed] + parsed.alternatives
        cands: list[Candidate] = []
        for i, p in enumerate(interps):
            for c in self._candidates(p, focus):
                if i > 0:
                    c.score -= 0.05  # 非首选解释略降权，只有数据明显更匹配时才翻盘
                cands.append(c)
        # 精确候选都没有时，才做代价更高的模糊路名检索
        if not any(PRECISION_RANK[c.precision] <= PRECISION_RANK["road"] for c in cands):
            for i, p in enumerate(interps):
                if p.road:
                    cands.extend(self._road_fuzzy(p, p.region_code, penalty=0.05 if i else 0.0))
        cands.sort(key=lambda c: c.score, reverse=True)
        cands = self._dedupe(cands)

        requested = parsed.target_level
        results = [self._result(c, requested, ambiguous=len(interps) > 1) for c in cands[:max(limit, 1)]]
        best = results[0] if results else None

        if use_fallback and self.fallback is not None and (best is None or best["confidence"] < self.min_confidence) \
                and _REQUESTED_RANK[requested] < PRECISION_RANK.get(best["precision"] if best else "none", 9):
            fb = self._fallback(address, parsed)
            if fb is not None:
                results.insert(0, fb)
                results = results[:max(limit, 1)]

        return {
            "status": "ok" if results else "not_found",
            "query": address,
            "parsed": parsed.to_dict(),
            "results": results,
        }

    # -------------------------------------------------------------- 候选生成
    def _candidates(self, p: ParsedAddress, focus) -> list[Candidate]:
        out: list[Candidate] = []
        region = p.region_code
        if p.road and p.road_no_num is not None:
            out.extend(self._door(p, region))
        if p.poi:
            out.extend(self._poi(p, p.poi, region, focus))
        if p.road:
            out.extend(self._road(p, region))
        if not p.poi and not p.road and p.community:
            out.extend(self._poi(p, p.community, region, focus, precision="community", base=0.75))
        if not p.poi and not p.road and not p.community and p.town_name and p.town is None:
            out.extend(self._poi(p, p.town_name, region, focus, precision="town", base=0.6))
        adm = self._admin_centroid(p)
        if adm is not None:
            out.append(adm)
        return out

    def _door(self, p: ParsedAddress, region: str | None) -> list[Candidate]:
        rows = [r for r in self.store.by_road(p.road, region) if r.road_no_num is not None]
        if not rows:
            return []
        target, core = p.road_no_num, _no_core(p.road_no)
        exact = [r for r in rows if _no_core(r.road_no) == core]
        if exact:
            # 同一门牌下有多个要素时，优先名称与 POI 一致的，其次纯门牌点
            def key(r: Place) -> float:
                s = max((name_similarity(p.poi, n) for n in r.names), default=0.0) if p.poi else 0.0
                return s + (0.05 if r.kind == "address" else 0) + self._town_bonus(p, r)

            r = max(exact, key=key)
            return [Candidate(r.lng, r.lat, "door", 0.98 + self._town_bonus(p, r), p, r)]

        # 门牌插值：同侧（同奇偶）优先，取目标两侧最近的已知门牌按号码线性插值
        same = [r for r in rows if r.road_no_num % 2 == target % 2] or rows
        lower = max((r for r in same if r.road_no_num < target), key=lambda r: r.road_no_num, default=None)
        upper = min((r for r in same if r.road_no_num > target), key=lambda r: r.road_no_num, default=None)
        if lower is not None and upper is not None:
            span = upper.road_no_num - lower.road_no_num
            if haversine(lower.lng, lower.lat, upper.lng, upper.lat) > 3000:
                return []
            t = (target - lower.road_no_num) / span
            lng = lower.lng + (upper.lng - lower.lng) * t
            lat = lower.lat + (upper.lat - lower.lat) * t
            score = 0.86 - 0.1 * min(1.0, span / 100)
            return [Candidate(lng, lat, "interpolated", score, p, None,
                              detail={"between": [lower.road_no, upper.road_no]})]
        near = lower or upper
        if near is not None and abs(near.road_no_num - target) <= 10:
            return [Candidate(near.lng, near.lat, "interpolated", 0.7, p, None,
                              detail={"nearest": near.road_no})]
        return []

    def _poi(self, p: ParsedAddress, text: str, region: str | None, focus, precision: str = "poi",
             base: float = 0.85) -> list[Candidate]:
        expr = match_expr(text, drop=self.common)
        if not expr:
            return []
        hits = self.store.search(expr, region=region, limit=40)
        out = []
        for r, _bm25 in hits:
            if r.kind == "road" or not r.name:
                continue
            sim = max(name_similarity(text, n) for n in r.names)
            if sim < 0.5:
                continue
            score = base * sim
            if p.road and r.road == p.road:
                score += 0.05
                if p.road_no and _no_core(r.road_no) == _no_core(p.road_no):
                    score += 0.08
            score += self._town_bonus(p, r) + min(0.03, max(r.weight, 0) * 0.003)
            if focus is not None:
                d = haversine(focus[0], focus[1], r.lng, r.lat)
                score += 0.08 * math.exp(-d / 20000)
            out.append(Candidate(r.lng, r.lat, precision, score, p, r, detail={"similarity": round(sim, 3)}))
        return out

    def _road(self, p: ParsedAddress, region: str | None) -> list[Candidate]:
        segs = self.store.roads_named(p.road, region)
        if segs:
            # 多段道路取最靠近整体中心的一段的中点
            clng = sum(s.lng for s in segs) / len(segs)
            clat = sum(s.lat for s in segs) / len(segs)
            s = min(segs, key=lambda s: haversine(clng, clat, s.lng, s.lat))
            return [Candidate(s.lng, s.lat, "road", 0.66 + self._town_bonus(p, s), p, s)]
        pts = self.store.by_road(p.road, region)
        if pts:
            pts.sort(key=lambda r: (r.road_no_num or 0))
            mid = pts[len(pts) // 2]
            return [Candidate(mid.lng, mid.lat, "road", 0.6, p, None, detail={"from": "address_points"})]
        return []

    def _road_fuzzy(self, p: ParsedAddress, region: str | None, penalty: float = 0.0) -> list[Candidate]:
        """路名写法不一致（“中关村大街” vs “中关村南大街”）时模糊找道路，只在区域内查。"""
        if region is None:
            return []
        expr = match_expr(p.road, drop=self.common)
        out = []
        for r, _ in self.store.search(expr, region=region, limit=20) if expr else []:
            if r.kind != "road":
                continue
            sim = name_similarity(p.road, r.name or "")
            if sim >= 0.75:
                out.append(Candidate(r.lng, r.lat, "road", 0.55 * sim - penalty, p, r,
                                     detail={"similarity": round(sim, 3)}))
        return out

    def _admin_centroid(self, p: ParsedAddress) -> Candidate | None:
        if p.chain is None:
            return None
        for level in (LEVEL_TOWN, LEVEL_DISTRICT, LEVEL_CITY, LEVEL_PROVINCE):
            a = p.chain.get(level)
            if a is not None and a.lng is not None:
                base = {LEVEL_TOWN: 0.5, LEVEL_DISTRICT: 0.42, LEVEL_CITY: 0.32, LEVEL_PROVINCE: 0.22}[level]
                return Candidate(a.lng, a.lat, _ADMIN_PRECISION[level], base, p, None, detail={"adcode": a.adcode})
        return None

    @staticmethod
    def _town_bonus(p: ParsedAddress, r: Place) -> float:
        t = p.town
        return 0.02 if t is not None and r.adcode and r.adcode.startswith(t.code) else 0.0

    @staticmethod
    def _dedupe(cands: list[Candidate]) -> list[Candidate]:
        out, seen = [], set()
        for c in cands:
            k = (c.place.id if c.place else None, round(c.lng, 5), round(c.lat, 5))
            if k in seen:
                continue
            seen.add(k)
            out.append(c)
        return out

    # ------------------------------------------------------------------ 结果
    def _result(self, c: Candidate, requested: str, ambiguous: bool) -> dict:
        p = c.parsed
        # 命中要素带有更细的区划编码时，用它补全/校正行政区（“望京SOHO” → 北京市朝阳区）
        chain_levels = dict(p.chain.levels) if p.chain else {}
        if c.place is not None and c.place.adcode:
            region = p.region_code
            if region is None or c.place.adcode.startswith(region):
                chain_levels = self.admin.lineage(c.place.adcode)
        comps = p.components()
        adcodes = p.adcodes()
        if chain_levels and (p.chain is None or chain_levels != p.chain.levels):
            view = replace(p, chain=AdminChain(levels=chain_levels, score=p.admin_score), alternatives=[])
            comps.update(view.admin_names())
            adcodes = view.adcodes()
        formatted = self._format(comps, c)
        conf = self._confidence(c, requested, ambiguous)
        matched = None
        if c.place is not None:
            matched = {"id": c.place.id, "kind": c.place.kind, "name": c.place.name,
                       "road": c.place.road, "road_no": c.place.road_no, "address": c.place.address}
        return {
            "location": {"lng": round(c.lng, 7), "lat": round(c.lat, 7)},
            "precision": c.precision,
            "confidence": conf,
            "source": c.source,
            "formatted_address": formatted,
            "components": comps,
            "adcode": adcodes,
            "matched": matched,
            "detail": c.detail or None,
        }

    @staticmethod
    def _format(comps: dict, c: Candidate) -> str:
        parts: list[str] = []
        for k in ("province", "city", "district", "town"):
            v = comps.get(k)
            if v and (not parts or parts[-1] != v):
                parts.append(v)
        if c.precision in ("door", "interpolated", "poi", "road", "community"):
            road, road_no = comps.get("road"), comps.get("road_no")
            if c.place is not None and c.place.kind != "road":
                road, road_no = road or c.place.road, road_no or c.place.road_no
            detail = (road or "") + (road_no or "")
            name = None
            if c.place is not None and c.place.name and c.place.kind != "road" and c.precision in ("poi", "door"):
                name = c.place.name
            elif comps.get("poi") and c.precision in ("door", "interpolated"):
                name = comps["poi"]
            if name:
                detail = name if road and name.startswith(road) and not road_no else detail + name
            if c.precision in ("door", "interpolated", "poi"):
                detail = join_parts([detail, comps.get("building"), comps.get("unit"), comps.get("floor"),
                                     comps.get("room")])
            parts.append(detail)
        return "".join(parts)

    @staticmethod
    def _confidence(c: Candidate, requested: str, ambiguous: bool) -> float:
        got = PRECISION_RANK[c.precision]
        want = _REQUESTED_RANK[requested]
        if c.precision == "interpolated" and want == PRECISION_RANK["door"]:
            conf = c.score  # 门牌插值视为达到门牌级请求，分数本身已体现跨度
        elif got <= want:
            # 达到了请求的精度：行政区级结果本身就是正确答案
            conf = max(c.score, 0.9) if c.precision in _ADMIN_PRECISION.values() else min(1.0, c.score + 0.1)
        else:
            conf = min(c.score, 0.55 - 0.05 * (got - want - 1))
        if ambiguous and c.place is None:
            conf *= 0.85  # 同名区县未被数据消歧
        return round(max(0.0, min(1.0, conf)), 3)

    # ------------------------------------------------------------------ 兜底
    def _fallback(self, address: str, parsed: ParsedAddress) -> dict | None:
        try:
            r = self.fallback.geocode(address)
        except Exception as e:  # noqa: BLE001 兜底失败不影响主链路
            log.warning("fallback failed: %s", e)
            return None
        if r is None:
            return None
        comps = parsed.components()
        res = {
            "location": {"lng": round(r.lng, 7), "lat": round(r.lat, 7)},
            "precision": r.precision,
            "confidence": r.confidence,
            "source": r.source,
            "formatted_address": parsed.formatted(),
            "components": comps,
            "adcode": parsed.adcodes(),
            "matched": None,
            "detail": r.raw,
        }
        self._log_review(address, res)
        return res

    def _log_review(self, address: str, res: dict) -> None:
        """兜底结果写入待审核文件，人工确认后可作为 places 数据回流建库。"""
        if not self.review_log:
            return
        line = json.dumps({"address": address, "lng": res["location"]["lng"], "lat": res["location"]["lat"],
                           "precision": res["precision"], "source": res["source"],
                           "adcode": res["adcode"].get("district")}, ensure_ascii=False)
        with self._review_lock, open(self.review_log, "a", encoding="utf-8") as f:
            f.write(line + "\n")
