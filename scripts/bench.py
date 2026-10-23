"""性能基准：合成 N 条 POI/门牌，建库后压测解析、正向、逆向的单线程延迟。

用法：python scripts/bench.py --n 1000000 --queries 5000
合成数据只用来测性能，名称和坐标都是随机的。
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import statistics
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from cngeocoder.admin import load_admin_csvs  # noqa: E402
from cngeocoder.build import BuildInputs, build  # noqa: E402
from cngeocoder.engine import Engine, Settings  # noqa: E402

CHARS = "东西南北中华新民安康泰和平兴盛龙凤金银玉明光阳春秋锦绣河山湖海星月云天文武德仁义礼智信福禄寿喜乐"
SUFFIX = ["大厦", "小区", "广场", "花园", "中心", "酒店", "医院", "学校", "公寓", "商场", "家园", "大楼"]
ROAD_SUFFIX = ["路", "街", "大道", "大街"]


def word(rng, a=2, b=3):
    return "".join(rng.choice(CHARS) for _ in range(rng.randint(a, b)))


def gen(path: str, n: int, seed: int = 7) -> list[tuple[str, str, str, int, float, float]]:
    rng = random.Random(seed)
    districts = [a for a in load_admin_csvs(os.path.join(ROOT, "data", "admin"))
                 if a.level == 3 and not a.virtual]
    centers = {d.code: (rng.uniform(100, 122), rng.uniform(22, 45)) for d in districts}
    roads = {d.code: [word(rng) + rng.choice(ROAD_SUFFIX) for _ in range(30)] for d in districts}
    samples = []
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "kind", "road", "road_no", "adcode", "lng", "lat"])
        for i in range(n):
            d = rng.choice(districts)
            cx, cy = centers[d.code]
            lng, lat = cx + rng.uniform(-0.1, 0.1), cy + rng.uniform(-0.1, 0.1)
            road = rng.choice(roads[d.code])
            no = rng.randint(1, 999)
            if i % 3 == 0:
                w.writerow(["", "address", road, f"{no}号", d.code, f"{lng:.6f}", f"{lat:.6f}"])
                name = ""
            else:
                name = word(rng) + rng.choice(SUFFIX)
                w.writerow([name, "poi", road, f"{no}号", d.code, f"{lng:.6f}", f"{lat:.6f}"])
            if i % max(1, n // 20000) == 0:
                samples.append((d.name, name, road, no, lng, lat))
    return samples


def stats(xs: list[float]) -> str:
    xs = sorted(xs)

    def q(p):
        return xs[min(len(xs) - 1, int(p * len(xs)))]

    return (f"mean {statistics.fmean(xs):.3f} ms | p50 {q(0.5):.3f} | p95 {q(0.95):.3f} | p99 {q(0.99):.3f} "
            f"| 单线程 ≈ {1000 / statistics.fmean(xs):,.0f} QPS")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200_000)
    ap.add_argument("--queries", type=int, default=3000)
    ap.add_argument("--workdir", default=None)
    args = ap.parse_args()
    wd = args.workdir or tempfile.mkdtemp(prefix="geobench")
    os.makedirs(wd, exist_ok=True)
    csv_path, db_path = os.path.join(wd, "places.csv"), os.path.join(wd, "bench.db")

    t0 = time.time()
    samples = gen(csv_path, args.n)
    print(f"生成 {args.n:,} 条合成数据: {time.time() - t0:.1f}s")
    t0 = time.time()
    build(BuildInputs(admin_dir=os.path.join(ROOT, "data", "admin"), out=db_path, places=(csv_path,)))
    print(f"建库: {time.time() - t0:.1f}s, 文件 {os.path.getsize(db_path) / 1e6:.0f} MB")

    t0 = time.time()
    eng = Engine(Settings(db_path=db_path, cache_size=0))
    print(f"服务加载: {time.time() - t0:.2f}s")
    try:
        import resource

        print(f"常驻内存峰值: {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024:.0f} MB（含建库过程）")
    except ImportError:  # pragma: no cover
        pass

    rng = random.Random(1)
    qs = [rng.choice(samples) for _ in range(args.queries)]
    lat_parse, lat_poi, lat_door, lat_rev = [], [], [], []
    hit_poi = hit_door = 0
    for dname, name, road, no, lng, lat in qs:
        addr_poi = f"{dname}{name}" if name else f"{dname}{road}"
        t = time.perf_counter()
        eng.parser.parse(addr_poi)
        lat_parse.append((time.perf_counter() - t) * 1000)
        if name:
            t = time.perf_counter()
            r = eng.geocoder.geocode(addr_poi, use_fallback=False)
            lat_poi.append((time.perf_counter() - t) * 1000)
            hit_poi += bool(r["results"]) and r["results"][0]["precision"] in ("poi", "door")
        t = time.perf_counter()
        r = eng.geocoder.geocode(f"{dname}{road}{no}号", use_fallback=False)
        lat_door.append((time.perf_counter() - t) * 1000)
        hit_door += bool(r["results"]) and r["results"][0]["precision"] in ("door", "interpolated")
        t = time.perf_counter()
        eng.reverse.reverse(lng + rng.uniform(-0.001, 0.001), lat + rng.uniform(-0.001, 0.001))
        lat_rev.append((time.perf_counter() - t) * 1000)

    print(f"地址解析      : {stats(lat_parse)}")
    print(f"正向·POI      : {stats(lat_poi)} | POI 命中率 {hit_poi / max(1, len(lat_poi)):.1%}")
    print(f"正向·门牌     : {stats(lat_door)} | 门牌/插值命中率 {hit_door / len(lat_door):.1%}")
    print(f"逆地理        : {stats(lat_rev)}")


if __name__ == "__main__":
    main()
