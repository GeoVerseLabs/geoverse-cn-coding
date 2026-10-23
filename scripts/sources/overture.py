"""从 Overture Maps 抽取中国范围的数据（按 GeoParquet 行组统计做范围裁剪，只读需要的部分）。

子命令：
  admin       行政区划点（divisions/division，ODbL）→ 匹配区划编码 → data/admin/centroids.csv
  boundaries  行政区划面（divisions/division_area，ODbL）→ 带 adcode 的 GeoJSON（不入库，自行评估合规后使用）
  roads       道路（transportation/segment，ODbL）→ 带名称的道路 GeoJSON
  places      POI（places/place，CDLA-Permissive-2.0 等）→ POI CSV

依赖：pip install -e ".[sources]"（pyarrow、fsspec、aiohttp）。网络需能访问
https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com（匿名公开桶）。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
from collections import defaultdict
from collections.abc import Iterator

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

S3 = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com"
CHINA_BBOX = (73.4, 3.8, 135.1, 53.6)
ISO_TO_GB = {"BJ": "11", "TJ": "12", "HE": "13", "SX": "14", "NM": "15", "LN": "21", "JL": "22", "HL": "23",
             "SH": "31", "JS": "32", "ZJ": "33", "AH": "34", "FJ": "35", "JX": "36", "SD": "37", "HA": "41",
             "HB": "42", "HN": "43", "GD": "44", "GX": "45", "HI": "46", "CQ": "50", "SC": "51", "GZ": "52",
             "YN": "53", "XZ": "54", "SN": "61", "GS": "62", "QH": "63", "NX": "64", "XJ": "65", "TW": "71",
             "HK": "81", "MO": "82"}
PLACEHOLDER = {"市辖区", "县", "省直辖县级行政区划", "自治区直辖县级行政区划"}
_CJK = re.compile(r"[㐀-鿿·]+")


def _fs():
    import fsspec

    return fsspec.filesystem("https", client_kwargs={"trust_env": True})


def latest_release(fs) -> str:
    x = fs.cat(f"{S3}/?list-type=2&prefix=release/&delimiter=/").decode()
    rels = sorted(re.findall(r"<Prefix>release/([^<]+)/</Prefix>", x))
    if not rels:
        raise SystemExit("无法列出 Overture 发布版本")
    return rels[-1]


def list_files(fs, release: str, theme: str, typ: str) -> list[str]:
    keys, token = [], None
    while True:
        url = f"{S3}/?list-type=2&prefix=release/{release}/theme={theme}/type={typ}/"
        if token:
            from urllib.parse import quote

            url += f"&continuation-token={quote(token)}"
        x = fs.cat(url).decode()
        keys += re.findall(r"<Key>([^<]+\.parquet)</Key>", x)
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", x)
        if not m:
            return keys
        token = m.group(1)


def iter_tables(fs, keys: list[str], bbox: tuple[float, float, float, float], columns: list[str],
                country: str | None = None) -> Iterator:
    """逐行组读取与 bbox 相交的数据，再按要素 bbox（及 country）精确过滤。"""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    xmin, ymin, xmax, ymax = bbox
    cols = list(dict.fromkeys(columns + ["bbox"] + (["country"] if country else [])))
    for key in keys:
        pf = pq.ParquetFile(fs.open(f"{S3}/{key}", block_size=8 << 20))
        md = pf.metadata
        idx = {md.schema.column(i).path: i for i in range(md.num_columns)}
        for rg in range(md.num_row_groups):
            r = md.row_group(rg)

            def st(path, r=r, idx=idx):
                return r.column(idx[path]).statistics

            if st("bbox.xmax").max < xmin or st("bbox.xmin").min > xmax or \
                    st("bbox.ymax").max < ymin or st("bbox.ymin").min > ymax:
                continue
            tb = pf.read_row_group(rg, columns=cols)
            b = tb["bbox"]
            mask = pc.and_(
                pc.and_(pc.less_equal(pc.struct_field(b, "xmin"), xmax), pc.greater_equal(pc.struct_field(b, "xmax"), xmin)),
                pc.and_(pc.less_equal(pc.struct_field(b, "ymin"), ymax), pc.greater_equal(pc.struct_field(b, "ymax"), ymin)))
            if country:
                mask = pc.and_(mask, pc.equal(tb["country"], country))
            tb = tb.filter(mask)
            if tb.num_rows:
                yield tb


def clean(name: str | None) -> str | None:
    if not name:
        return None
    m = _CJK.search(name)
    return m.group() if m else None


def name_variants(name: str | None) -> list[str]:
    """“武汉经济技术开发区（汉南区）”→ [武汉经济技术开发区, 汉南区]；“拉萨市 ལྷ་ས”→ [拉萨市]。"""
    out = [n for n in [clean(name)] if n]
    for inner in re.findall(r"[（(]([^）)]+)[）)]", name or ""):
        c = clean(inner)
        if c and c not in out:
            out.append(c)
    return out


def _point(wkb: bytes) -> tuple[float, float]:
    import shapely

    g = shapely.from_wkb(wkb)
    if g.geom_type != "Point":
        g = g.representative_point()
    return round(g.x, 6), round(g.y, 6)


def _dist_km(a, b) -> float:
    dx = (a[0] - b[0]) * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot(dx, a[1] - b[1]) * 111.2


# --------------------------------------------------------------------------- admin
def cmd_admin(args) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from cngeocoder.admin import LEVEL_CITY, LEVEL_DISTRICT, LEVEL_PROVINCE, LEVEL_TOWN, load_admin_csvs

    fs = _fs()
    release = args.release or latest_release(fs)
    os.makedirs(args.cache, exist_ok=True)
    cache = os.path.join(args.cache, f"division_cn_{release}.parquet")
    if not os.path.exists(cache):
        t0 = time.time()
        keys = list_files(fs, release, "divisions", "division")
        cols = ["id", "geometry", "subtype", "names", "local_type", "region", "hierarchies", "capital_division_ids",
                "wikidata"]
        tables = list(iter_tables(fs, keys, CHINA_BBOX, cols, country="CN"))
        pq.write_table(pa.concat_tables(tables), cache)
        print(f"下载 Overture {release} 中国区划点: {time.time() - t0:.0f}s → {cache}")
    rows = pq.read_table(cache).to_pylist()
    byid = {r["id"]: r for r in rows}

    def name_of(r):
        return clean((r["names"] or {}).get("primary"))

    def hier(r):
        h = (r["hierarchies"] or [[]])[0]
        return [(e["division_id"], e["subtype"], clean(e["name"])) for e in h]

    def seat(r):
        """区划驻地：capital_division_ids 指向的城镇点；没有时取区划自身的点。"""
        for cid in r["capital_division_ids"] or []:
            c = byid.get(cid)
            if c is not None and c["geometry"]:
                return _point(c["geometry"]), "seat"
        return _point(r["geometry"]), "label"

    regions, prefs, counties = {}, defaultdict(list), defaultdict(list)
    towns_by_county = defaultdict(lambda: defaultdict(list))
    for r in rows:
        st, nm = r["subtype"], name_of(r)
        if not nm:
            continue
        reg = (r["region"] or "").removeprefix("CN-") or None  # 少数要素缺 region，按名称全国唯一兜底
        if st == "region":
            regions[ISO_TO_GB.get(reg)] = r
        elif st == "county":
            for v in name_variants((r["names"] or {}).get("primary")):
                prefs[(reg, v)].append(r)
        elif st == "localadmin":
            for v in name_variants((r["names"] or {}).get("primary")):
                counties[(reg, v)].append(r)
        elif st == "locality":
            # 挂到所属区县（localadmin）；东莞、中山这类不设区的市直接挂在地级（county）下
            anc = hier(r)[:-1]
            owner = next((d for d, s, _ in anc if s == "localadmin"), None) or \
                next((d for d, s, _ in reversed(anc) if s == "county"), None)
            if owner:
                towns_by_county[owner][nm].append(r)

    admins = load_admin_csvs(args.admin_dir)
    by_code = {a.code: a for a in admins}
    out: dict[str, tuple] = {}
    ov_of: dict[str, str] = {}
    stats = defaultdict(lambda: [0, 0])

    def put(a, pt, method, r):
        out[a.code] = (a.code, a.name, pt[0], pt[1], method)
        ov_of[a.code] = r["id"]

    gb_to_iso = {v: k for k, v in ISO_TO_GB.items()}
    for a in sorted(admins, key=lambda a: (a.level, a.code)):
        if a.virtual:
            continue
        stats[a.level][1] += 1
        if a.level == LEVEL_PROVINCE:
            r = regions.get(a.code)
            if r is not None:
                # 北京市人民政府 2019 年迁至通州副中心，“北京市”按惯例取市中心（区划标注点在天安门一带）
                pt, m = (_point(r["geometry"]), "label") if a.code == "11" else seat(r)
                put(a, pt, m, r)
                stats[a.level][0] += 1
            continue
        iso = gb_to_iso.get(a.code[:2])
        if a.level == LEVEL_CITY:
            cands = prefs.get((iso, a.name), []) or prefs.get((None, a.name), [])
            if len(cands) == 1:
                pt, m = seat(cands[0])
                put(a, pt, m, cands[0])
                stats[a.level][0] += 1
            continue
        if a.level == LEVEL_DISTRICT:
            cands = counties.get((iso, a.name), []) or counties.get((None, a.name), [])
            if len(cands) > 1:
                pref = by_code.get(a.code[:4])
                pname = None if pref is None or pref.virtual else pref.name
                cands = [c for c in cands if pname in {n for _, s, n in hier(c) if s == "county"}] if pname else \
                    [c for c in cands if not any(s == "county" for _, s, _ in hier(c))]
            if len(cands) == 1:
                c = cands[0]
                pt, m = seat(c)
                if m == "label":  # 驻地缺失时找同名城镇点
                    same = towns_by_county.get(c["id"], {}).get(a.name, [])
                    if len(same) == 1:
                        pt, m = _point(same[0]["geometry"]), "seat"
                put(a, pt, m, c)
                stats[a.level][0] += 1
            elif a.code[:4] in out and by_code[a.code[:4]].name == a.name:
                # 不设区的地级市（东莞、中山、儋州、嘉峪关）：同名“区县”沿用市的中心点
                out[a.code] = (a.code, a.name, *out[a.code[:4]][2:4], "seat")
                ov_of[a.code] = ov_of[a.code[:4]]
                stats[a.level][0] += 1
            continue
        if a.level == LEVEL_TOWN:
            cid = ov_of.get(a.parent or "")
            if not cid:
                continue
            pool = towns_by_county.get(cid, {})
            variants = [a.name]
            if a.name.endswith("办事处"):
                variants.append(a.name[:-3])
            if a.name.endswith("地区办事处"):
                variants.append(a.name[:-5] + "地区")
            for v in variants:
                cands = pool.get(v, [])
                if len(cands) > 1:  # 同名多个点时取乡镇级（town）而非村
                    cands = [c for c in cands if dict(c["local_type"] or []).get("en") in ("town", "city")] or cands
                if len(cands) == 1:
                    put(a, _point(cands[0]["geometry"]), "locality", cands[0])
                    stats[a.level][0] += 1
                    break

    # 合理性检查：子级中心离上级中心过远的视为误匹配
    # 与直接上级比较；省级跨度大（内蒙古东西约 2400 km），地级只做宽松检查
    vast = {"15", "51", "54", "62", "63", "65"}
    dropped = []
    for code in sorted(out, key=len):
        a = by_code[code]
        parent = next((out[p] for p in (code[:6], code[:4], code[:2]) if p != code and p in out), None)
        if parent and a.level > LEVEL_PROVINCE:
            limit = {LEVEL_CITY: 2000,
                     LEVEL_DISTRICT: (800 if code[:2] in vast else 400) if len(parent[0]) == 4 else 2000,
                     LEVEL_TOWN: 300 if code[:2] in vast else 150}[a.level]
            d = _dist_km(out[code][2:4], parent[2:4])
            if d > limit:
                dropped.append((code, a.name, "too_far", round(d), parent[0]))
                del out[code]
                stats[a.level][0] -= 1
    if args.report:
        with open(args.report, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["adcode", "name", "status", "distance_km", "parent"])
            w.writerows(dropped)
            for a in admins:
                if not a.virtual and a.code not in out and a.code not in {d[0] for d in dropped}:
                    w.writerow([a.code, a.name, "unmatched", "", ""])

    with open(args.out, "w", encoding="utf-8", newline="") as f:
        f.write(f"# 行政区划中心点（WGS84）。来源：Overture Maps Foundation divisions 主题 {release}，\n"
                "# 其数据主要来自 OpenStreetMap。本文件为派生数据库，按 ODbL 1.0 许可：\n"
                "# © OpenStreetMap contributors, Overture Maps Foundation。\n"
                "# method: seat=区划驻地（capital/同名城镇点）, label=区划标注点, locality=乡镇同名城镇点。\n"
                "# 由 scripts/sources/overture.py admin 生成，请勿手改。\n")
        w = csv.writer(f)
        w.writerow(["adcode", "name", "lng", "lat", "coordsys", "method"])
        for code in sorted(out):
            c, n, x, y, m = out[code]
            w.writerow([c, n, f"{x:.6f}", f"{y:.6f}", "wgs84", m])
    names = {1: "省级", 2: "地级", 3: "县级", 4: "乡级"}
    for lv in sorted(stats):
        ok, total = stats[lv]
        print(f"{names[lv]}: {ok}/{total} ({ok / max(total, 1):.1%})")
    print(f"合理性检查剔除 {len(dropped)} 条；写入 {len(out)} 条 → {args.out}")
    if args.mapping:
        with open(args.mapping, "w", encoding="utf-8") as f:
            json.dump(ov_of, f)
    return 0


# --------------------------------------------------------------------------- boundaries
def cmd_boundaries(args) -> int:
    import shapely

    fs = _fs()
    release = args.release or latest_release(fs)
    with open(args.mapping, encoding="utf-8") as f:
        code_of = {v: k for k, v in json.load(f).items() if len(k) <= args.max_code_len}
    keys = list_files(fs, release, "divisions", "division_area")
    feats = []
    for tb in iter_tables(fs, keys, CHINA_BBOX, ["division_id", "geometry", "subtype", "class"], country="CN"):
        for r in tb.to_pylist():
            code = code_of.get(r["division_id"])
            if code is None or r["class"] not in (None, "land"):
                continue
            g = shapely.from_wkb(r["geometry"])
            feats.append({"type": "Feature", "properties": {"adcode": code, "coordsys": "wgs84"},
                          "geometry": json.loads(shapely.to_geojson(g.simplify(args.simplify)))})
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection",
                   "name": f"Overture {release} divisions (ODbL, © OpenStreetMap contributors)",
                   "features": feats}, f, ensure_ascii=False)
    print(f"写入 {len(feats)} 个区划面 → {args.out}")
    return 0


# --------------------------------------------------------------------------- roads
ROAD_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "living_street",
                "unclassified", "service", "pedestrian"}


def cmd_roads(args) -> int:
    import shapely

    fs = _fs()
    release = args.release or latest_release(fs)
    bbox = tuple(map(float, args.bbox.split(",")))
    keys = list_files(fs, release, "transportation", "segment")
    n = 0
    with open(args.out, "w", encoding="utf-8") as f:
        name = f"Overture {release} transportation (ODbL, © OpenStreetMap contributors)"
        f.write('{"type":"FeatureCollection","name":' + json.dumps(name, ensure_ascii=False) + ',"features":[\n')
        first = True
        for tb in iter_tables(fs, keys, bbox, ["geometry", "subtype", "class", "names"]):
            for r in tb.to_pylist():
                name = clean((r["names"] or {}).get("primary"))
                if r["subtype"] != "road" or r["class"] not in ROAD_CLASSES or not name or len(name) < 2:
                    continue
                geom = json.loads(shapely.to_geojson(shapely.from_wkb(r["geometry"])))
                feat = {"type": "Feature", "properties": {"name": name, "class": r["class"], "coordsys": "wgs84"},
                        "geometry": geom}
                f.write(("" if first else ",\n") + json.dumps(feat, ensure_ascii=False))
                first = False
                n += 1
        f.write("\n]}\n")
    print(f"写入 {n} 条命名道路段 → {args.out}")
    return 0


# --------------------------------------------------------------------------- places
def cmd_places(args) -> int:
    fs = _fs()
    release = args.release or latest_release(fs)
    bbox = tuple(map(float, args.bbox.split(",")))
    keys = list_files(fs, release, "places", "place")
    n = skipped = 0
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        f.write(f"# Overture Maps places {release}（各条许可见 license 列，主要为 CDLA-Permissive-2.0 / Apache-2.0）\n")
        w = csv.writer(f)
        w.writerow(["name", "kind", "address", "lng", "lat", "coordsys", "weight", "category", "license"])
        for tb in iter_tables(fs, keys, bbox, ["geometry", "names", "addresses", "confidence", "basic_category",
                                                "sources"], country=None):
            for r in tb.to_pylist():
                raw = (r["names"] or {}).get("primary") or ""
                name = clean(raw) if args.cjk_only else raw.strip()
                if not name or (r["confidence"] or 0) < args.min_confidence:
                    skipped += 1
                    continue
                x, y = _point(r["geometry"])
                addr = ((r["addresses"] or [{}])[0] or {}).get("freeform") or ""
                lic = ";".join(sorted({s["license"] for s in r["sources"] or [] if s.get("license")}))
                w.writerow([name, "poi", addr, x, y, "wgs84", round((r["confidence"] or 0) * 5, 2),
                            r["basic_category"] or "", lic])
                n += 1
    print(f"写入 {n} 条 POI（跳过 {skipped} 条）→ {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Overture Maps 中国数据抽取")
    ap.add_argument("--release", help="发布版本（默认最新），如 2026-09-23.1")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("admin", help="区划点 → 区划中心点 CSV")
    a.add_argument("--admin-dir", default=os.path.join(ROOT, "data", "admin"))
    a.add_argument("--out", default=os.path.join(ROOT, "data", "admin", "centroids.csv"))
    a.add_argument("--cache", default=os.path.join(ROOT, "var", "sources"))
    a.add_argument("--report", default=os.path.join(ROOT, "var", "sources", "admin_match_report.csv"),
                   help="未匹配与被剔除条目清单，供人工核查")
    a.add_argument("--mapping", default=os.path.join(ROOT, "var", "sources", "overture_admin_ids.json"),
                   help="输出 区划编码 → Overture division id 映射，供 boundaries 使用")
    a.set_defaults(func=cmd_admin)

    b = sub.add_parser("boundaries", help="区划面 → GeoJSON（需先运行 admin）")
    b.add_argument("--mapping", default=os.path.join(ROOT, "var", "sources", "overture_admin_ids.json"))
    b.add_argument("--max-code-len", type=int, default=6, help="6=到区县，9=含乡镇（乡镇面覆盖不全）")
    b.add_argument("--simplify", type=float, default=0.0005)
    b.add_argument("--out", default=os.path.join(ROOT, "var", "sources", "boundaries_overture.geojson"))
    b.set_defaults(func=cmd_boundaries)

    r = sub.add_parser("roads", help="命名道路 → GeoJSON")
    r.add_argument("--bbox", required=True, help="min_lng,min_lat,max_lng,max_lat")
    r.add_argument("--out", default=os.path.join(ROOT, "var", "sources", "roads_overture.geojson"))
    r.set_defaults(func=cmd_roads)

    p = sub.add_parser("places", help="POI → CSV")
    p.add_argument("--bbox", required=True)
    p.add_argument("--cjk-only", action="store_true", help="只保留中文名称（截取名称中的中文部分）")
    p.add_argument("--min-confidence", type=float, default=0.6)
    p.add_argument("--out", default=os.path.join(ROOT, "var", "sources", "places_overture.csv"))
    p.set_defaults(func=cmd_places)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
