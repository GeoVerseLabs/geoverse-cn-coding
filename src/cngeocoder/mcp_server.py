"""MCP（Model Context Protocol）服务端：让 agent 直接调用地址编码与数据维护能力。

两种接入方式，工具完全相同：

- HTTP：``POST /mcp``（Streamable HTTP 传输，无状态，响应为 application/json），与 HTTP API 共用 API Key 鉴权；
- stdio：``cngeocoder mcp --role admin``，本机 agent（Claude Code、Claude Desktop 等）以子进程方式启动。

工具按角色开放：``read`` 角色只看到查询类工具；``admin`` 角色额外看到数据维护工具。
协议层（JSON-RPC 2.0：initialize / ping / tools/list / tools/call）在这里直接实现，不依赖 MCP SDK。
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, TYPE_CHECKING, Any

from . import __version__, coords
from .auth import Principal
from .edit import EditError

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "cngeocoder"

INSTRUCTIONS = """中国地址编码服务（cngeocoder）。
- 坐标默认 WGS84；从高德/腾讯拾取的坐标传 coordsys="gcj02"，百度传 "bd09"，结果按同一坐标系返回。
- 查询：parse_address（结构化解析）、geocode（地址→坐标）、reverse_geocode（坐标→地址）、convert_coordinates、search_regions。
- 维护（需要 admin 密钥且服务开启在线编辑）：先 search_places 确认是否已有要素，避免重复；
  用 search_regions 找区划编码（adcode）；新增 / 修改后用 geocode 验证效果；
  “某种写法应该落到某个坐标”用 add_memory（历史确认结果），而不是新建要素；
  待审核队列（兜底结果、业务反馈）用 list_review / resolve_review 处理。
- 每次修改都记入变更日志（list_changes），带操作人，可追溯。
- 密钥可能只授权了部分区域（get_service_info 的 caller.regions），超出范围的修改会被拒绝。"""

_COORDSYS = {"type": "string", "enum": ["wgs84", "gcj02", "bd09", "cgcs2000"],
             "description": "坐标系，默认 wgs84（高德/腾讯为 gcj02，百度为 bd09）"}
_LNG = {"type": "number", "minimum": -180, "maximum": 180, "description": "经度"}
_LAT = {"type": "number", "minimum": -90, "maximum": 90, "description": "纬度"}
_PAGE = {"offset": {"type": "integer", "minimum": 0, "default": 0},
         "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}
_PLACE_PROPS = {
    "kind": {"type": "string", "enum": ["poi", "address", "road"],
             "description": "poi（有名称的地点）、address（纯门牌点）、road（道路，需要 geometry）"},
    "name": {"type": "string", "description": "名称，如“望京SOHO”"},
    "alias": {"type": "array", "items": {"type": "string"}, "description": "别名 / 简称，如 [\"北大\"]"},
    "address": {"type": "string", "description": "完整地址；没给 adcode / 路名时据此解析"},
    "road": {"type": "string", "description": "路名"},
    "road_no": {"type": "string", "description": "门牌号，如“18号”“甲13号”“100弄”"},
    "adcode": {"type": "string", "description": "区划编码（6 位区县或 9 位乡镇）；留空按地址和坐标推断"},
    "lng": _LNG, "lat": _LAT, "coordsys": _COORDSYS,
    "weight": {"type": "number", "description": "热度，同名候选排序加分，默认 0"},
    "geometry": {"type": "object", "description": "道路几何，GeoJSON LineString / MultiLineString"},
}


@dataclass
class Tool:
    name: str
    title: str
    description: str
    properties: dict
    handler: Callable[[dict, Principal], Any]
    required: tuple[str, ...] = ()
    role: str = "read"
    annotations: dict = field(default_factory=dict)

    def spec(self) -> dict:
        schema = {"type": "object", "properties": self.properties, "additionalProperties": False}
        if self.required:
            schema["required"] = list(self.required)
        return {"name": self.name, "title": self.title, "description": self.description, "inputSchema": schema,
                "annotations": {"title": self.title, **self.annotations}}


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class McpServer:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.tools = {t.name: t for t in self._build_tools()}

    # ------------------------------------------------------------ 协议
    def tools_for(self, p: Principal) -> list[Tool]:
        return [t for t in self.tools.values() if p.allows(t.role)]

    def handle_payload(self, payload: Any, p: Principal) -> Any:
        """处理一条消息或一个批量数组；只有通知 / 响应时返回 None。"""
        if isinstance(payload, list):
            if not payload:
                return _error(None, -32600, "空的批量请求")
            out = [r for r in (self.handle(m, p) for m in payload) if r is not None]
            return out or None
        return self.handle(payload, p)

    def handle(self, msg: Any, p: Principal) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "不是合法的 JSON-RPC 2.0 消息")
        if "method" not in msg:
            return None  # 客户端发来的响应（本服务不发请求），忽略
        mid = msg.get("id")
        is_request = "id" in msg
        try:
            result = self._dispatch(msg["method"], msg.get("params") or {}, p)
        except RpcError as e:
            return _error(mid, e.code, e.message) if is_request else None
        except Exception as e:  # noqa: BLE001 协议层兜住，避免连接中断
            log.exception("mcp %s failed", msg.get("method"))
            return _error(mid, -32603, f"内部错误: {e}") if is_request else None
        if not is_request:
            return None
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _dispatch(self, method: str, params: dict, p: Principal) -> Any:
        if method == "initialize":
            want = params.get("protocolVersion")
            version = want if want in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "title": "GeoVerse 中国地址编码服务", "version": __version__},
                    "instructions": INSTRUCTIONS}
        if method == "ping":
            return {}
        if method.startswith("notifications/"):
            return None
        if method == "tools/list":
            return {"tools": [t.spec() for t in self.tools_for(p)]}
        if method == "tools/call":
            return self._call(params, p)
        raise RpcError(-32601, f"不支持的方法: {method}")

    def _call(self, params: dict, p: Principal) -> dict:
        name = params.get("name")
        tool = self.tools.get(name)
        if tool is None:
            raise RpcError(-32602, f"未知工具: {name}")
        if not p.allows(tool.role):
            return _tool_error(f"权限不足：{name} 需要 {tool.role} 角色，当前为 {p.role}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise RpcError(-32602, "arguments 应为对象")
        missing = [k for k in tool.required if args.get(k) in (None, "")]
        if missing:
            return _tool_error(f"缺少参数: {', '.join(missing)}")
        unknown = [k for k in args if k not in tool.properties]
        if unknown:
            return _tool_error(f"未知参数: {', '.join(unknown)}")
        self.engine.sync()
        try:
            result = tool.handler(args, p)
        except (EditError, ValueError) as e:
            return _tool_error(str(e))
        if not isinstance(result, dict):
            result = {"items": result}
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "structuredContent": result, "isError": False}

    # ------------------------------------------------------------ 工具
    def _build_tools(self) -> list[Tool]:
        eng = self.engine
        m = eng.manager
        ro = {"readOnlyHint": True, "openWorldHint": False}
        write = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}

        def cs(a: dict) -> str:
            return coords.CoordSys.parse(a.get("coordsys")).value

        def out(obj, a: dict):
            return {**coords.output(obj, cs(a)), "coordsys": cs(a)} if isinstance(obj, dict) else obj

        def parse(a, _p):
            pa = eng.parser.parse(a["address"], city=a.get("city"))
            d = pa.to_dict()
            d["alternatives"] = [x.to_dict()["adcode"] for x in pa.alternatives]
            return d

        def geocode(a, p):
            focus = None
            if a.get("focus_lng") is not None and a.get("focus_lat") is not None:
                focus = coords.to_wgs84(a["focus_lng"], a["focus_lat"], cs(a))
            r = eng.geocoder.geocode(a["address"], city=a.get("city"), focus=focus, limit=int(a.get("limit") or 1),
                                     use_fallback=bool(a.get("fallback", False)) and p.fallback)
            return out(r, a)

        def reverse(a, _p):
            lng, lat = coords.to_wgs84(a["lng"], a["lat"], cs(a))
            r = eng.reverse.reverse(lng, lat, radius=float(a.get("radius") or 500),
                                    poi_limit=int(a.get("poi_limit") if a.get("poi_limit") is not None else 5))
            return out(r, a)

        def convert(a, _p):
            src, dst = coords.CoordSys.parse(a.get("from")), coords.CoordSys.parse(a.get("to") or "gcj02")
            lng, lat = coords.convert(a["lng"], a["lat"], src, dst)
            return {"location": {"lng": round(lng, 7), "lat": round(lat, 7)}, "from": src.value, "to": dst.value}

        def info(_a, p):
            d = eng.info()
            d["caller"] = p.public()
            d.pop("db", None)  # 不向 agent 暴露服务器路径
            return d

        def regions(a, _p):
            return {"items": m.search_regions(a["q"], int(a.get("limit") or 20))}

        def place_input(a: dict) -> dict:
            d = {k: v for k, v in a.items() if k in _PLACE_PROPS or k == "uid"}
            if isinstance(d.get("alias"), list):
                d["alias"] = "|".join(d["alias"])
            return d

        def search_places(a, p):
            r = m.list_places(a.get("q"), a.get("kind"), a.get("adcode"), a.get("source"),
                              int(a.get("offset") or 0), int(a.get("limit") or 20), regions=p.regions)
            return out(r, a)

        def get_place(a, p):
            return out(m.get_place(a["uid"], regions=p.regions), a)

        def add_place(a, p):
            return out(m.create_place(place_input(a), actor=p.name, regions=p.regions), a)

        def update_place(a, p):
            d = place_input(a)
            uid = d.pop("uid")
            if "lng" in a or "lat" in a or "geometry" in a:
                d["coordsys"] = cs(a)
            return out(m.update_place(uid, d, actor=p.name, regions=p.regions), a)

        def delete_place(a, p):
            return {"deleted": m.delete_place(a["uid"], actor=p.name, regions=p.regions)}

        def list_memory(a, p):
            return out(m.list_memory(a.get("q"), int(a.get("offset") or 0), int(a.get("limit") or 20),
                                     regions=p.regions), a)

        def add_memory(a, p):
            rows = m.add_memory(a["address"], a["lng"], a["lat"], coordsys=cs(a), count=int(a.get("count") or 1),
                                spread=float(a.get("spread") or 0), actor=p.name, regions=p.regions)
            return out({"items": rows}, a)

        def delete_memory(a, p):
            return {"deleted": m.delete_memory(a["key"], actor=p.name, regions=p.regions)}

        def list_review(a, p):
            return out(m.review_items(a.get("source"), a.get("status") or "pending", int(a.get("offset") or 0),
                                      int(a.get("limit") or 20), regions=p.regions), a)

        def resolve_review(a, p):
            return out(m.resolve_review(a["id"], a["action"], a.get("lng"), a.get("lat"), coordsys=cs(a),
                                        actor=p.name, regions=p.regions), a)

        def list_changes(a, p):
            return m.list_changes(int(a.get("offset") or 0), int(a.get("limit") or 20), a.get("op"),
                                  actor=p.name if p.scoped else None)

        place_update_props = {"uid": {"type": "string", "description": "要素 uid（search_places 结果里的 uid）"},
                              **_PLACE_PROPS}
        return [
            # ---------------- 查询（read）
            Tool("parse_address", "地址结构化解析",
                 "把一条中文地址拆成省 / 市 / 区县 / 乡镇 / 道路 / 门牌 / POI / 楼栋单元室，并给出各级区划编码。不需要坐标数据。",
                 {"address": {"type": "string", "description": "地址原文"},
                  "city": {"type": "string", "description": "城市提示（名称或区划编码），可选"}},
                 parse, ("address",), annotations=ro),
            Tool("geocode", "正向地理编码",
                 "地址 → 坐标。结果含 precision（door / poi / road / district …）、confidence、signals（置信度来源）。"
                 "confidence < 0.6 表示不宜直接采用。",
                 {"address": {"type": "string"}, "city": {"type": "string", "description": "城市提示，可选"},
                  "coordsys": _COORDSYS, "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1},
                  "focus_lng": _LNG, "focus_lat": _LAT,
                  "fallback": {"type": "boolean", "default": False,
                               "description": "本地置信度不足时是否调用天地图兜底（消耗配额）"}},
                 geocode, ("address",), annotations=ro),
            Tool("reverse_geocode", "逆地理编码", "坐标 → 行政区、最近门牌与道路、周边 POI、方位描述。",
                 {"lng": _LNG, "lat": _LAT, "coordsys": _COORDSYS,
                  "radius": {"type": "number", "minimum": 10, "maximum": 3000, "default": 500},
                  "poi_limit": {"type": "integer", "minimum": 0, "maximum": 50, "default": 5}},
                 reverse, ("lng", "lat"), annotations=ro),
            Tool("convert_coordinates", "坐标系转换", "WGS84 / GCJ-02 / BD-09 / CGCS2000 互转。",
                 {"lng": _LNG, "lat": _LAT, "from": _COORDSYS, "to": {**_COORDSYS, "description": "目标坐标系，默认 gcj02"}},
                 convert, ("lng", "lat"), annotations=ro),
            Tool("search_regions", "查找行政区", "按名称片段（如“海淀”）或编码前缀查行政区及其区划编码（adcode）。",
                 {"q": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}},
                 regions, ("q",), annotations=ro),
            Tool("get_service_info", "服务与数据状态", "数据版本、各类要素数量、是否允许在线编辑、缓存与兜底状态。",
                 {}, info, annotations=ro),
            # ---------------- 数据维护（admin）
            Tool("search_places", "查找要素", "按关键字（名称 / 地址 / 路名 / 别名 / uid）、类型、区划编码前缀、来源查 POI、门牌和道路。",
                 {"q": {"type": "string"}, "kind": _PLACE_PROPS["kind"],
                  "adcode": {"type": "string", "description": "区划编码前缀，如 110108"},
                  "source": {"type": "string", "description": "来源文件名，在线新增的为 manual"},
                  "coordsys": _COORDSYS, **_PAGE},
                 search_places, role="admin", annotations=ro),
            Tool("get_place", "查看要素", "按 uid 取一个要素的全部字段。",
                 {"uid": {"type": "string"}, "coordsys": _COORDSYS}, get_place, ("uid",), role="admin",
                 annotations=ro),
            Tool("add_place", "新增要素",
                 "新增 POI / 门牌点（需要 lng、lat，以及 name 或 road_no）或道路（kind=road + geometry）。"
                 "立即生效，并记入变更日志。",
                 {**_PLACE_PROPS, "uid": {"type": "string", "description": "自定义 uid，可选；默认自动生成"}},
                 add_place, role="admin", annotations=write),
            Tool("update_place", "修改要素", "按 uid 修改要素，只改传入的字段；改了地址或坐标而没传 adcode 时自动重新推断行政区。",
                 place_update_props, update_place, ("uid",), role="admin",
                 annotations={**write, "idempotentHint": True}),
            Tool("delete_place", "删除要素", "按 uid 删除要素，立即生效。变更日志保留删除前的内容。",
                 {"uid": {"type": "string"}}, delete_place, ("uid",), role="admin",
                 annotations={**write, "destructiveHint": True, "idempotentHint": True}),
            Tool("list_memory", "查看确认结果", "历史确认结果：地址写法 → 已确认坐标（签收点、人工核验、审核通过的兜底结果）。",
                 {"q": {"type": "string"}, "coordsys": _COORDSYS, **_PAGE}, list_memory, role="admin", annotations=ro),
            Tool("add_memory", "新增确认结果",
                 "记录“这种写法就是这个坐标”。同样的写法（含口语化写法）再来时直接命中，优先于检索结果。",
                 {"address": {"type": "string"}, "lng": _LNG, "lat": _LAT, "coordsys": _COORDSYS,
                  "count": {"type": "integer", "minimum": 1, "default": 1, "description": "确认次数，越多越可信"},
                  "spread": {"type": "number", "minimum": 0, "maximum": 300, "default": 0,
                             "description": "多次确认的离散程度（米）"}},
                 add_memory, ("address", "lng", "lat"), role="admin", annotations=write),
            Tool("delete_memory", "删除确认结果", "按 key（list_memory 结果里的 key）删除一条确认结果。",
                 {"key": {"type": "string"}}, delete_memory, ("key",), role="admin",
                 annotations={**write, "destructiveHint": True, "idempotentHint": True}),
            Tool("list_review", "待审核队列", "兜底服务返回的结果和业务反馈，审核通过后写入确认结果。",
                 {"source": {"type": "string", "enum": ["review", "feedback"]},
                  "status": {"type": "string", "enum": ["pending", "approved", "dismissed", "all"], "default": "pending"},
                  "coordsys": _COORDSYS, **_PAGE},
                 list_review, role="admin", annotations=ro),
            Tool("resolve_review", "处理待审核条目", "approve：写入确认结果（可传 lng / lat 修正坐标）；dismiss：驳回。",
                 {"id": {"type": "string"}, "action": {"type": "string", "enum": ["approve", "dismiss"]},
                  "lng": _LNG, "lat": _LAT, "coordsys": _COORDSYS},
                 resolve_review, ("id", "action"), role="admin", annotations=write),
            Tool("list_changes", "变更日志", "在线编辑的变更记录（新的在前），含操作人、修改前后内容。",
                 {"op": {"type": "string", "enum": ["place.upsert", "place.delete", "memory.upsert", "memory.delete",
                                                    "review.resolve"]}, **_PAGE},
                 list_changes, role="admin", annotations=ro),
        ]


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def serve_stdio(server: McpServer, principal: Principal, stdin: IO[str] | None = None,
                stdout: IO[str] | None = None) -> None:
    """stdio 传输：每行一条 JSON-RPC 消息。日志只写 stderr。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            resp: Any = _error(None, -32700, "JSON 解析失败")
        else:
            resp = server.handle_payload(payload, principal)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
