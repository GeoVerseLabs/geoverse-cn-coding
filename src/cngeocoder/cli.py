"""命令行：建库、解析、正向/逆向编码、评测、启动服务、MCP（stdio）、密钥生成、变更日志导出。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _engine(args, editable: bool | None = None):
    from .engine import Engine, Settings

    s = Settings.from_env()
    if args.db:
        s.db_path = args.db
    if editable is not None:
        s.editable = editable
    return Engine(s)


def cmd_build(args) -> int:
    from .build import BuildInputs, build

    stats = build(BuildInputs(
        admin_dir=args.admin_dir, out=args.out, aliases=tuple(args.aliases or ()),
        boundaries=tuple(args.boundaries or ()), centroids=tuple(args.centroids or ()),
        places=tuple(args.places or ()), roads=tuple(args.roads or ()), memory=tuple(args.memory or ()),
        edits=tuple(args.edits or ()), coordsys=args.coordsys,
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
    from .coords import output

    eng = _engine(args)
    for a in args.address:
        _dump(output(eng.geocoder.geocode(a, city=args.city, limit=args.limit), args.coordsys))
    return 0


def cmd_reverse(args) -> int:
    from .coords import output, to_wgs84

    eng = _engine(args)
    lng, lat = to_wgs84(args.lng, args.lat, args.coordsys)
    _dump(output(eng.reverse.reverse(lng, lat, radius=args.radius), args.coordsys))
    return 0


def cmd_eval(args) -> int:
    from .evaluate import evaluate

    eng = _engine(args)
    rep = evaluate(eng.geocoder, args.gold, use_fallback=args.fallback, threshold=args.threshold)
    _dump(rep)
    acc = rep["admin_accuracy"].get("district")
    if args.min_district_acc and (acc is None or acc < args.min_district_acc):
        print(f"区县准确率 {acc} 低于阈值 {args.min_district_acc}", file=sys.stderr)
        return 1
    cw = rep["calibration"]["confident_wrong"]
    if args.max_confident_wrong is not None and cw > args.max_confident_wrong:
        print(f"高置信度错误 {cw} 条，超过上限 {args.max_confident_wrong}", file=sys.stderr)
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


def _stdio_principal(args):
    """stdio 调用方。设置了 GEOCODER_API_KEY 时按密钥配置确定角色、数据范围和兜底权限（与 HTTP 一致），
    否则按 --role（信任边界是启动它的操作系统用户）。"""
    from .auth import KeyRing, Principal
    from .engine import Settings

    token = os.environ.get("GEOCODER_API_KEY")
    if not token:
        return Principal(name=args.name, role=args.role)
    ring = KeyRing.from_settings(Settings.from_env())
    key = ring.verify(token)
    if key is None:
        raise SystemExit("GEOCODER_API_KEY 不在 GEOCODER_API_KEYS / GEOCODER_API_KEYS_FILE 配置的密钥里")
    p = ring.principal_for(key)
    if p.role == "write":
        p.role = "read"  # MCP 没有反馈工具，write 等同 read
    return p


def cmd_mcp(args) -> int:
    from .mcp_server import McpServer, serve_stdio

    p = _stdio_principal(args)
    eng = _engine(args, editable=True if p.role == "admin" and args.editable else None)
    print(f"MCP stdio: {p.name}（{p.role}{'，范围 ' + ','.join(p.regions) if p.regions else ''}）", file=sys.stderr)
    serve_stdio(McpServer(eng), p)
    return 0


def cmd_keygen(args) -> int:
    from .auth import keygen_line

    k = keygen_line(args.name, args.role)
    print(f"# 密钥只显示这一次，请交给调用方保存（{k['name']}，角色 {k['role']}）", file=sys.stderr)
    print(f"key: {k['key']}")
    print(f"GEOCODER_API_KEYS 条目: {k['env']}")
    print(f"GEOCODER_API_KEYS_FILE（JSON）条目: {json.dumps(k['json'], ensure_ascii=False)}")
    return 0


def cmd_edits_export(args) -> int:
    from .edit import _change
    from .engine import Settings
    from .store import Store

    s = Settings.from_env()
    # 不加载整个引擎，直接读变更日志；在线服务可能正在写，不用 immutable
    store = Store(args.db or s.db_path, immutable=False)

    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    n = 0
    try:
        for r in store.conn().execute("SELECT seq, ts, actor, op, target, data, before FROM change_log ORDER BY seq"):
            out.write(json.dumps(_change(r), ensure_ascii=False) + "\n")
            n += 1
    finally:
        if args.out:
            out.close()
    print(f"导出 {n} 条变更", file=sys.stderr)
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
    b.add_argument("--memory", nargs="*", help="历史确认结果 CSV/JSONL（address,lng,lat[,coordsys,count]），含 /v1/feedback 日志")
    b.add_argument("--edits", nargs="*", help="在线编辑的变更日志 JSONL（cngeocoder edits export 导出），建库最后按顺序重放")
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
    e.add_argument("--threshold", type=float, default=0.6, help="视为“可直接采用”的置信度阈值")
    e.add_argument("--max-confident-wrong", type=int, default=None, help="可直接采用但错误的条数上限")
    e.set_defaults(func=cmd_eval)

    s = sub.add_parser("serve", help="启动 HTTP 服务")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--workers", type=int, default=1)
    s.set_defaults(func=cmd_serve)

    m = sub.add_parser("mcp", help="以 stdio 方式提供 MCP 服务（供本机 agent 以子进程启动）")
    m.add_argument("--role", choices=["read", "admin"], default="read",
                   help="agent 的角色：read 只读查询，admin 可维护数据（设置了 GEOCODER_API_KEY 时以密钥配置为准）")
    m.add_argument("--name", default="mcp-stdio", help="变更日志里记录的操作人")
    m.add_argument("--editable", action=argparse.BooleanOptionalAction, default=True,
                   help="admin 角色时以可写方式打开数据文件（默认开启）")
    m.set_defaults(func=cmd_mcp)

    k = sub.add_parser("keygen", help="生成 API Key 及其配置条目")
    k.add_argument("--name", required=True, help="调用方名称，如 order-service")
    k.add_argument("--role", choices=["read", "write", "admin"], default="read")
    k.set_defaults(func=cmd_keygen)

    ed = sub.add_parser("edits", help="在线编辑的变更日志")
    ed_sub = ed.add_subparsers(dest="edits_cmd", required=True)
    ex = ed_sub.add_parser("export", help="导出全部变更（JSONL），下次建库时用 build --edits 重放")
    ex.add_argument("--out", help="输出文件，默认标准输出")
    ex.set_defaults(func=cmd_edits_export)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
