"""金标准评测：每次改解析规则、换数据都跑一遍，防止准确率回退。

金标准 CSV 字段：address, city, expect_adcode, expect_lng, expect_lat, coordsys
（expect_* 可留空；坐标默认 WGS84）。
"""

from __future__ import annotations

import csv
import statistics
import time
from collections import Counter

from . import coords
from .geo import haversine
from .geocoder import Geocoder


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(q * (len(xs) - 1)))))
    return round(xs[k], 1)


def evaluate(geocoder: Geocoder, gold_path: str, use_fallback: bool = False) -> dict:
    rows = list(csv.DictReader(open(gold_path, encoding="utf-8-sig")))
    n = len(rows)
    found = 0
    level_ok = Counter()
    level_total = Counter()
    errors: list[float] = []
    lat_ms: list[float] = []
    precision = Counter()
    failures: list[dict] = []
    for r in rows:
        t0 = time.perf_counter()
        res = geocoder.geocode(r["address"], city=r.get("city") or None, use_fallback=use_fallback)
        lat_ms.append((time.perf_counter() - t0) * 1000)
        best = res["results"][0] if res["results"] else None
        if best:
            found += 1
            precision[best["precision"]] += 1
        exp = (r.get("expect_adcode") or "").strip()
        if exp:
            got = (best or {}).get("adcode") or res["parsed"]["adcode"]
            for key, ln in (("province", 2), ("city", 4), ("district", 6)):
                if len(exp) < ln or exp[:ln].endswith("00") and ln > 2:
                    continue
                level_total[key] += 1
                g = got.get(key) or ""
                if g[:ln] == exp[:ln]:
                    level_ok[key] += 1
                elif key == "district":
                    failures.append({"address": r["address"], "expect": exp, "got": g})
        if best and r.get("expect_lng") and r.get("expect_lat"):
            elng, elat = coords.to_wgs84(float(r["expect_lng"]), float(r["expect_lat"]), r.get("coordsys") or "wgs84")
            d = haversine(elng, elat, best["location"]["lng"], best["location"]["lat"])
            errors.append(d)
            if d > 1000:
                failures.append({"address": r["address"], "error_m": round(d), "precision": best["precision"]})
    return {
        "total": n,
        "found_rate": round(found / n, 4) if n else None,
        "admin_accuracy": {k: round(level_ok[k] / level_total[k], 4) for k in level_total},
        "distance_m": {"p50": _pct(errors, 0.5), "p90": _pct(errors, 0.9), "p95": _pct(errors, 0.95),
                       "within_100m": round(sum(e <= 100 for e in errors) / len(errors), 4) if errors else None,
                       "within_500m": round(sum(e <= 500 for e in errors) / len(errors), 4) if errors else None,
                       "within_1km": round(sum(e <= 1000 for e in errors) / len(errors), 4) if errors else None},
        "precision": dict(precision),
        "latency_ms": {"mean": round(statistics.fmean(lat_ms), 3) if lat_ms else None, "p95": _pct(lat_ms, 0.95)},
        "failures": failures[:50],
    }
