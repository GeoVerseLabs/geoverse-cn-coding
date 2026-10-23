"""命令行：建库、解析、正向/逆向编码、评测、启动服务。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _engine(args):
    from .engine import Engine, Settings

    s = Settings.from_env()
    if args.db:
        s.db_path = args.db
    return Engine(s)


def cmd_build(args) -> int:
    from .build import BuildInputs, build

    stats = build(BuildInputs(
        admin_dir=args.admin_dir, out=args.out, aliases=tuple(args.aliases or ()),
        boundaries=tuple(args.boundaries or ()), centroids=tuple(args.centroids or ()),
        places=tuple(args.places or ()), roads=tuple(args.roads or ()), coordsys=args.coordsys,
        simplify=args.simplify,
    ))
    _dump(stats)
    return 0


def cmd_parse(args) -> int:
    if args.db:
        eng = _engine(args)
        parser = eng.parser
    else:  # 不需要建库，直接用内置区划数据
        from .admin import AdminIndex, load_admin_aliases, load_admin_csvs
        from .parser import AddressParser

        admin_dir = args.admin_dir
        parser = AddressParser(AdminIndex(load_admin_csvs(admin_dir),
                                          load_admin_aliases(admin_dir)))
    for a in args.address:
        _dump(parser.parse(a, city=args.city).to_dict())
    return 0


def cmd_geocode(args) -> int:
    from .api import _out
    from .coords import CoordSys

    eng = _engine(args)
    cs = CoordSys.parse(args.coordsys)
    for a in args.address:
        _dump(_out(eng.geocoder.geocode(a, city=args.city, limit=args.limit), cs))
    return 0


def cmd_reverse(args) -> int:
    from .api import _out
    from .coords import CoordSys, to_wgs84

    eng = _engine(args)
    cs = CoordSys.parse(args.coordsys)
    lng, lat = to_wgs84(args.lng, args.lat, cs)
    _dump(_out(eng.reverse.reverse(lng, lat, radius=args.radius), cs))
    return 0


def cmd_eval(args) -> int:
    from .evaluate import evaluate

    eng = _engine(args)
    rep = evaluate(eng.geocoder, args.gold, use_fallback=args.fallback)
    _dump(rep)
    acc = rep["admin_accuracy"].get("district")
    if args.min_district_acc and (acc is None or acc < args.min_district_acc):
        print(f"区县准确率 {acc} 低于阈值 {args.min_district_acc}", file=sys.stderr)
        return 1
    p90 = rep["distance_m"]["p90"]
    if args.max_p90 and p90 is not None and p90 > args.max_p90:
        print(f"P90 误差 {p90}m 超过阈值 {args.max_p90}m", file=sys.stderr)
        return 1
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    if args.db:
        os.environ["GEOCODER_DB"] = args.db
    uvicorn.run("cngeocoder.api:app_factory", factory=True, host=args.host, port=args.port,
                workers=args.workers, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    default_admin = os.environ.get("GEOCODER_ADMIN_DIR", "data/admin")
    ap = argparse.ArgumentParser(prog="cngeocoder", description="中国地址地理编码服务")
    ap.add_argument("--db", help="数据文件路径（默认取 GEOCODER_DB 环境变量）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="离线建库")
    b.add_argument("--admin-dir", default=default_admin)
    b.add_argument("--aliases", nargs="*", help="额外的区划别名 CSV（<admin-dir> 下的 aliases.csv、history_aliases.csv 自动加载）")
    b.add_argument("--boundaries", nargs="*", help="行政区边界 GeoJSON")
    b.add_argument("--centroids", nargs="*", help="额外的行政区中心点 CSV（adcode,lng,lat），覆盖 <admin-dir>/centroids.csv")
    b.add_argument("--places", nargs="*", help="POI/门牌 CSV 或 GeoJSON")
    b.add_argument("--roads", nargs="*", help="道路 GeoJSON")
    b.add_argument("--coordsys", default="wgs84", help="输入数据坐标系（行内 coordsys 列优先）")
    b.add_argument("--simplify", type=float, default=0.0001, help="边界简化容差（度）")
    b.add_argument("--out", default=os.environ.get("GEOCODER_DB", "var/geocoder.db"))
    b.set_defaults(func=cmd_build)

    p = sub.add_parser("parse", help="地址结构化解析")
    p.add_argument("address", nargs="+")
    p.add_argument("--city")
    p.add_argument("--admin-dir", default=default_admin)
    p.set_defaults(func=cmd_parse)

    g = sub.add_parser("geocode", help="正向地理编码")
    g.add_argument("address", nargs="+")
    g.add_argument("--city")
    g.add_argument("--coordsys", default="wgs84")
    g.add_argument("--limit", type=int, default=1)
    g.set_defaults(func=cmd_geocode)

    r = sub.add_parser("reverse", help="逆地理编码")
    r.add_argument("lng", type=float)
    r.add_argument("lat", type=float)
    r.add_argument("--coordsys", default="wgs84")
    r.add_argument("--radius", type=float, default=500)
    r.set_defaults(func=cmd_reverse)

    e = sub.add_parser("eval", help="金标准评测")
    e.add_argument("gold")
    e.add_argument("--fallback", action="store_true", help="评测时允许调用兜底服务")
    e.add_argument("--min-district-acc", type=float, default=None)
    e.add_argument("--max-p90", type=float, default=None)
    e.set_defaults(func=cmd_eval)

    s = sub.add_parser("serve", help="启动 HTTP 服务")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--workers", type=int, default=1)
    s.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
