"""金标准评测：每次改解析规则、换数据都跑一遍，防止准确率回退。

金标准 CSV 字段：address, city, expect_adcode, expect_lng, expect_lat, coordsys, expect
（expect_* 可留空；坐标默认 WGS84）。expect=none 表示数据里没有该地点，期望服务给出低置信度或不返回，
用来检查口语化放宽匹配之后会不会“自信地错配”。

置信度校准指标（confidence ≥ threshold 视为“可直接采用”）：
- confident_correct：可直接采用且正确（距期望点 ≤ radius_m）的占比，越高越好；
- confident_wrong：可直接采用但错误的条数（含不该命中的负样本），越低越好；
- negatives_rejected：负样本被拒绝（无结果或低置信度）的占比。
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


def evaluate(geocoder: Geocoder, gold_path: str, use_fallback: bool = False, threshold: float = 0.6,
             radius_m: float = 1000.0) -> dict:
    rows = list(csv.DictReader(open(gold_path, encoding="utf-8-sig")))
    negatives = [r for r in rows if (r.get("expect") or "").strip().lower() == "none"]
    rows = [r for r in rows if r not in negatives]
    n = len(rows)
    found = 0
    level_ok = Counter()
    level_total = Counter()
    errors: list[float] = []
    lat_ms: list[float] = []
    calib: dict[str, list[float]] = {"correct": [], "wrong": []}
    confident_correct = confident_wrong = 0
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
            ok = d <= radius_m
            calib["correct" if ok else "wrong"].append(best["confidence"])
            if best["confidence"] >= threshold:
                confident_correct += ok
                confident_wrong += not ok
            if d > radius_m:
                failures.append({"address": r["address"], "error_m": round(d), "precision": best["precision"],
                                 "confidence": best["confidence"]})
        elif r.get("expect_lng"):
            calib["wrong"].append(0.0)
    rejected = 0
    for r in negatives:
        res = geocoder.geocode(r["address"], city=r.get("city") or None, use_fallback=use_fallback)
        best = res["results"][0] if res["results"] else None
        # 只落到区县及以上（没找到具体地点）也算拒绝
        if best is None or best["confidence"] < threshold or best["precision"] in ("district", "city", "province"):
            rejected += 1
        else:
            confident_wrong += 1
            failures.append({"address": r["address"], "expect": "none", "got": (best.get("matched") or {}).get("name"),
                             "confidence": best["confidence"]})
    positives_with_coord = len(calib["correct"]) + len(calib["wrong"])
    return {
        "total": n,
        "calibration": {
            "threshold": threshold,
            "radius_m": radius_m,
            "confident_correct": round(confident_correct / positives_with_coord, 4) if positives_with_coord else None,
            "confident_wrong": confident_wrong,
            "negatives": len(negatives),
            "negatives_rejected": round(rejected / len(negatives), 4) if negatives else None,
            "mean_confidence_correct": round(statistics.fmean(calib["correct"]), 3) if calib["correct"] else None,
            "mean_confidence_wrong": round(statistics.fmean(calib["wrong"]), 3) if calib["wrong"] else None,
        },
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
