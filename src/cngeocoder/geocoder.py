"""正向地理编码：地址 → 坐标。

对解析出的每种行政区解释（通常 1 个，同名区县时 2~3 个）生成候选：
历史确认结果 → 门牌精确 → 门牌插值 / 交叉口 → POI → 道路 → 乡镇/区县/城市/省中心点，
统一打分后取最高者；置信度不足时走兜底服务（天地图等），并把兜底结果记入待审核日志。

口语化、非标准输入的处理（与 colloquial.py、parser.py 配合）：
- POI 名称比对时生成查询变体（去掉“南广场”“A口”等子位置）和名称变体（去掉用户已写出的城市前缀、
  数据别名、建库生成的简称），按序省略的写法（腾讯大厦 → 腾讯滨海大厦）也能匹配；
- “X 对面的 Y”先定位 X，再在 X 周边 2 km 找 Y；“往东 500 米”按方位距离位移；
- 同名 POI 分布在多地且没有区县或位置上下文时，降低置信度并返回备选；
- 每个结果带 signals，说明置信度从哪里来（纠错、繁体、去掉的口语、相似度、备选数）。
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from dataclasses import dataclass, field, replace

import shapely
from shapely.ops import nearest_points, unary_union

from .admin import (
    LEVEL_CITY,
    LEVEL_DISTRICT,
    LEVEL_PROVINCE,
    LEVEL_TOWN,
    AdminChain,
    AdminIndex,
    native_code,
    short_names,
)
from .cache import LRUCache
from .colloquial import GENERIC_BIGRAMS, is_generic
from .geo import destination, haversine
from .parser import AddressParser, ParsedAddress, join_parts
from .store import Place, Store
from .tokenizer import match_expr, name_similarity, ngrams

log = logging.getLogger(__name__)

# 精度等级，越靠前越精细
PRECISION_RANK = {"door": 0, "interpolated": 1, "intersection": 1, "poi": 2, "road": 3, "community": 4, "town": 5,
                  "district": 6, "city": 7, "province": 8, "none": 9}
_REQUESTED_RANK = {"building": 0, "door": 0, "intersection": 1, "poi": 2, "road": 3, "community": 4, "town": 5,
                   "district": 6, "city": 7, "province": 8, "none": 9}
_ADMIN_PRECISION = {LEVEL_TOWN: "town", LEVEL_DISTRICT: "district", LEVEL_CITY: "city", LEVEL_PROVINCE: "province"}
_NO_CORE = re.compile(r"(号院|号|弄)$")
# POI 的子位置：车站广场、出入口、方位门/区（“火车站南广场出口”→“火车站”）；单独的“广场”不去掉（万达广场）
_SUBLOC_RE = re.compile(r"(?:[东西南北](?:广场|门|区|侧|口|楼)|出口|入口|进站口|出站口|停车场|地下车库|候车室|售票处"
                        r"|[A-Z]\d*口|\d+号口|[A-Z]\d*出口)$")
# 方位关系对置信度的影响与大致范围（米）
_RELATION_EFFECT = {"near": (0.92, 300), "opposite": (0.92, 100), "at": (1.0, 0)}


def query_variants(text: str) -> list[tuple[str, float]]:
    """查询的 POI 文本及去掉子位置后的变体，附带折扣。"""
    out = [(text, 1.0)]
    s = text
    for _ in range(2):
        m = _SUBLOC_RE.search(s)
        if not m or m.start() < 2:
            break
        s = s[: m.start()]
        out.append((s, 0.97))
    return out


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
        gen = self.cache.generation
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        res = self._geocode(address, city, focus, limit, use_fallback)
        self.cache.put(key, res, gen)
        return res

    def _geocode(self, address, city, focus, limit, use_fallback) -> dict:
        parsed = self.parser.parse(address, city=city, alternatives=4)
        interps = [parsed] + parsed.alternatives
        cands: list[Candidate] = []
        mem = self._memory(parsed)
        if mem is not None:
            cands.append(mem)
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
        if parsed.near_target and cands:
            cands = self._near_target(parsed, cands) + cands

        requested = parsed.target_level
        competitors = self._competitors(cands, parsed, focus)
        results = [self._result(c, requested, admin_ambiguous=len(interps) > 1,
                                competitors=competitors if i == 0 else [])
                   for i, c in enumerate(cands[:max(limit, 1)])]
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
        if p.road and p.cross_road:
            out.extend(self._intersection(p, region))
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
        qvars = query_variants(text)
        if p.chain is not None and p.chain.matched and p.chain.end + len(text) <= len(p.normalized) + 2:
            # 行政区简称可能是 POI 名称的一部分（“上海中心大厦”被切成 上海 + 中心大厦），把它接回去再比一次
            qvars.append((p.chain.matched[-1].form + text, 1.0))
        prefixes = self._chain_prefixes(p)
        for r, _bm25 in hits:
            if r.kind == "road" or not r.name:
                continue
            sim, how = self._best_similarity(qvars, r, prefixes, self.common)
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
            out.append(Candidate(r.lng, r.lat, precision, score, p, r,
                                 detail={"similarity": round(sim, 3), "matched_by": how}))
        return out

    @staticmethod
    def _best_similarity(qvars: list[tuple[str, float]], r: Place, prefixes: tuple[str, ...],
                         common: frozenset[str] = frozenset()) -> tuple[float, str]:
        """在查询变体 × 名称变体（名称、数据别名、去城市前缀、生成简称）里取最高相似度。

        两道保护避免口语化放宽带来的误配：去掉前缀后只剩类别词的名称变体不用；
        查询里有辨识度的词（“虹桥”火车站）在候选所有名称里都没出现时，相似度打 6 折。
        """
        names: list[tuple[str, float, str]] = []
        for i, n in enumerate(r.names):
            names.append((n, 1.0, "name" if i == 0 else "alias"))
            for pre in prefixes:
                rest = n[len(pre):]
                if n.startswith(pre) and len(rest) >= 3 and not is_generic(rest):
                    names.append((rest, 0.97, "name_without_city"))
        names.extend((a, 0.9, "abbreviation") for a in r.abbrs)
        best, how, best_q = 0.0, "name", qvars[0][0]
        for q, qf in qvars:
            for n, nf, kind in names:
                s = name_similarity(q, n) * qf * nf
                if s > best:
                    best, how, best_q = s, kind if qf == 1.0 else kind + "+sublocation", q
        # 行政区名（“上海”虹桥火车站）不算辨识词
        admin_grams = {g for pre in prefixes for g in ngrams(pre)}
        distinctive = [g for g in ngrams(best_q) if g not in GENERIC_BIGRAMS and g not in common
                       and g not in admin_grams]
        if distinctive and best < 0.99:
            pool = "|".join(n for n, _, _ in names).lower()
            if not any(g in pool for g in distinctive):
                best *= 0.6
        return best, how

    def _chain_prefixes(self, p: ParsedAddress) -> tuple[str, ...]:
        """用户已写出（或推断出）的省、市、区县名及简称：比对 POI 名称时可去掉这些前缀。"""
        if p.chain is None:
            return ()
        out: list[str] = []
        for level, a in p.chain.levels.items():
            if level <= LEVEL_DISTRICT:
                out.append(a.name)
                out.extend(short_names(a.name, level))
        return tuple(sorted(set(out), key=len, reverse=True))

    def _intersection(self, p: ParsedAddress, region: str | None) -> list[Candidate]:
        """“A 路与 B 路交叉口”：两条道路几何求交；没有相交但相距 150 m 内时取最近点中点。"""
        segs1 = [s for s in self.store.roads_named(p.road, region) if s.geom]
        segs2 = [s for s in self.store.roads_named(p.cross_road, region) if s.geom]
        if not segs1 or not segs2:
            return []
        # 同名道路可能在多个区县，按区县分组求交
        for d in dict.fromkeys(s.district_code for s in segs1):
            g1 = unary_union([shapely.from_wkb(s.geom) for s in segs1 if s.district_code == d])
            others = [s for s in segs2 if s.district_code == d] or segs2
            g2 = unary_union([shapely.from_wkb(s.geom) for s in others])
            inter = g1.intersection(g2)
            if not inter.is_empty:
                x, y = shapely.get_coordinates(inter)[0]
                ref = next(s for s in segs1 if s.district_code == d)
                return [Candidate(float(x), float(y), "intersection", 0.95, p, ref,
                                  detail={"roads": [p.road, p.cross_road]})]
            a, b = nearest_points(g1, g2)
            gap = haversine(a.x, a.y, b.x, b.y)
            if gap <= 150:
                ref = next(s for s in segs1 if s.district_code == d)
                return [Candidate((a.x + b.x) / 2, (a.y + b.y) / 2, "intersection", 0.85, p, ref,
                                  detail={"roads": [p.road, p.cross_road], "gap_m": round(gap, 1)})]
        return []

    def _near_target(self, p: ParsedAddress, anchors: list[Candidate]) -> list[Candidate]:
        """“X 对面的 Y”：以 X 的最佳候选为参照，在 2 km 内找名称匹配 Y 的 POI，越近越优先。"""
        anchor = anchors[0]
        if anchor.place is None and anchor.precision in _ADMIN_PRECISION.values():
            return []
        expr = match_expr(p.near_target, drop=self.common)
        if not expr:
            return []
        out = []
        for r, _ in self.store.search(expr, region=p.region_code, limit=60):
            if r.kind == "road" or not r.name or (anchor.place is not None and r.id == anchor.place.id):
                continue
            d = haversine(anchor.lng, anchor.lat, r.lng, r.lat)
            if d > 2000:
                continue
            sim, how = self._best_similarity(query_variants(p.near_target), r, self._chain_prefixes(p), self.common)
            if sim < 0.5:
                continue
            score = 0.85 * sim + 0.1 * math.exp(-d / 500)
            anchor_name = anchor.place.name if anchor.place is not None else p.poi
            out.append(Candidate(r.lng, r.lat, "poi", score, p, r,
                                 detail={"similarity": round(sim, 3), "matched_by": how,
                                         "anchor": anchor_name, "distance_to_anchor_m": round(d)}))
        out.sort(key=lambda c: c.score, reverse=True)
        return out[:3]

    def _memory(self, p: ParsedAddress) -> Candidate | None:
        """历史确认结果：同一写法被多次确认过的坐标，离散度越小、次数越多越可信。"""
        row = self.store.memory(p.memory_keys())
        if not row:
            return None
        lng, lat, n, spread, adcode, key = row
        if spread > 300:
            return None
        prec = "door" if spread <= 30 else ("poi" if spread <= 150 else "road")
        score = min(0.99, 0.88 + 0.05 * math.log10(max(n, 1) + 1) - spread / 3000)
        return Candidate(lng, lat, prec, score, p, None, source="memory",
                         detail={"confirmations": n, "spread_m": spread, "adcode": adcode, "key": key})

    def _competitors(self, cands: list[Candidate], p: ParsedAddress, focus) -> list[Candidate]:
        """分数接近、相距 2 km 以上的其他 POI：没有区县或位置上下文时视为同名歧义。"""
        if not cands or cands[0].place is None or cands[0].source != "local":
            return []
        if p.district is not None or focus is not None:
            return []
        best = cands[0]
        out = []
        for c in cands[1:]:
            if c.score < best.score - 0.06:
                break
            if c.place is None or c.precision not in ("poi", "door", "community"):
                continue
            if haversine(best.lng, best.lat, c.lng, c.lat) > 2000:
                out.append(c)
        return out[:3]

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
        """同一要素的多个候选合并：保留最高分，精度取其中最细的（门牌命中的 POI 记为门牌级）。"""
        out: list[Candidate] = []
        index: dict[tuple, int] = {}
        for c in cands:
            k = (c.place.id if c.place else None, round(c.lng, 5), round(c.lat, 5))
            if k in index:
                kept = out[index[k]]
                if PRECISION_RANK[c.precision] < PRECISION_RANK[kept.precision]:
                    kept.precision = c.precision
                continue
            index[k] = len(out)
            out.append(c)
        return out

    # ------------------------------------------------------------------ 结果
    def _result(self, c: Candidate, requested: str, admin_ambiguous: bool,
                competitors: list[Candidate] | None = None) -> dict:
        p = c.parsed
        competitors = competitors or []
        # 命中要素带有更细的区划编码时，用它补全/校正行政区（“望京SOHO” → 北京市朝阳区）
        chain_levels = dict(p.chain.levels) if p.chain else {}
        data_code = c.place.adcode if c.place is not None else c.detail.get("adcode") if c.source == "memory" else None
        admin_from_data = False
        if data_code:
            region = p.region_code
            if region is None or data_code.startswith(region):
                admin_from_data = p.district is None
                chain_levels = self.admin.lineage(data_code)
        comps = p.components()
        adcodes = p.adcodes()
        if chain_levels and (p.chain is None or chain_levels != p.chain.levels):
            view = replace(p, chain=AdminChain(levels=chain_levels, score=p.admin_score), alternatives=[])
            comps.update(view.admin_names())
            adcodes = view.adcodes()
        formatted = self._format(comps, c)
        conf = self._confidence(c, requested, admin_ambiguous)
        signals: dict = {}
        # 行政区纠错：命中数据落在纠正后的区县内，说明纠错被数据证实，不扣分
        if p.corrections:
            confirmed = data_code is not None and all(
                data_code.startswith(native_code(x["adcode"])) for x in p.corrections)
            if not confirmed:
                conf *= 0.85 if any(x["weak"] for x in p.corrections) else 0.95
            signals["corrections"] = p.corrections
        if competitors:
            # 多地同名、无法判断是哪一个：降到“需确认”区间，并在 alternatives 里给出备选
            conf = min(conf * 0.7, 0.55)
            signals["competitors"] = len(competitors)
        lng, lat = c.lng, c.lat
        relation = None
        if p.relation and c.source == "local" and not c.detail.get("anchor"):
            relation = dict(p.relation)
            relation["anchor"] = c.place.name if c.place is not None and c.place.name else formatted
            if p.relation["type"] == "offset":
                lng, lat = destination(c.lng, c.lat, p.relation["bearing"], p.relation["distance_m"])
                relation["anchor_location"] = {"lng": round(c.lng, 7), "lat": round(c.lat, 7)}
                conf *= 0.9 if p.relation["distance_m"] <= 1000 else 0.8
            else:
                factor, radius = _RELATION_EFFECT[p.relation["type"]]
                conf *= factor
                if radius:
                    relation["radius_m"] = radius
        elif c.detail.get("anchor"):
            relation = {"type": p.relation["type"] if p.relation else "near", "anchor": c.detail["anchor"],
                        "distance_to_anchor_m": c.detail.get("distance_to_anchor_m")}
        if p.near_target and not c.detail.get("anchor") and c.source == "local":
            conf = min(conf, 0.55)  # 只找到了参照物，没找到要找的地方
            signals["target_not_found"] = p.near_target
        for k, v in (("traditional", p.traditional), ("removed", p.removed), ("admin_from_data", admin_from_data),
                     ("similarity", c.detail.get("similarity")), ("matched_by", c.detail.get("matched_by"))):
            if v:
                signals[k] = v
        matched = None
        if c.place is not None:
            matched = {"id": c.place.id, "kind": c.place.kind, "name": c.place.name,
                       "road": c.place.road, "road_no": c.place.road_no, "address": c.place.address}
        out = {
            "location": {"lng": round(lng, 7), "lat": round(lat, 7)},
            "precision": c.precision,
            "confidence": round(max(0.0, min(1.0, conf)), 3),
            "source": c.source,
            "formatted_address": formatted,
            "components": comps,
            "adcode": adcodes,
            "matched": matched,
            "detail": c.detail or None,
            "signals": signals or None,
        }
        if relation:
            out["relation"] = relation
        if competitors:
            out["alternatives"] = [
                {"name": x.place.name if x.place else None, "location": {"lng": round(x.lng, 7), "lat": round(x.lat, 7)},
                 "adcode": x.place.adcode if x.place else None, "address": x.place.address if x.place else None}
                for x in competitors]
        return out

    @staticmethod
    def _format(comps: dict, c: Candidate) -> str:
        parts: list[str] = []
        for k in ("province", "city", "district", "town"):
            v = comps.get(k)
            if v and (not parts or parts[-1] != v):
                parts.append(v)
        if c.precision == "intersection":
            parts.append(f"{comps.get('road') or ''}与{c.detail.get('roads', ['', ''])[1]}交叉口")
            return "".join(parts)
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
        if c.source == "memory":
            return min(1.0, c.score + 0.05)  # 同一写法被确认过，精度以确认点为准
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
