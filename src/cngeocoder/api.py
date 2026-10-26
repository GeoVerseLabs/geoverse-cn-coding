"""HTTP API（FastAPI）。

所有接口都支持 ``coordsys`` 参数（wgs84 / gcj02 / bd09 / cgcs2000），只在出入口转换，内部统一 WGS84。
接口是同步函数，FastAPI 放到线程池执行；SQLite 每线程一个只读连接。

- ``/v1/*``：查询（read 角色）、反馈（write 角色）；
- ``/v1/admin/*``：数据管理（admin 角色），读接口在只读模式下也可用，写接口需要 ``GEOCODER_EDITABLE=1``；
- ``/admin``：数据管理界面（静态页面，数据都走 ``/v1/admin/*``，用 admin 密钥登录）；
- ``/mcp``：MCP 服务（Streamable HTTP），与 HTTP API 共用鉴权。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.security import APIKeyHeader, HTTPBearer
from pydantic import BaseModel, Field

from . import __version__, coords
from .auth import AuthError, KeyRing, Principal, token_from
from .edit import EditError
from .engine import Engine, Settings
from .mcp_server import PROTOCOL_VERSIONS, McpServer

log = logging.getLogger(__name__)
MAX_BATCH = 1000
_feedback_lock = threading.Lock()
_STATIC = Path(__file__).parent / "static"

# 只用于在 /docs 里显示“Authorize”按钮；实际取密钥在 _principal 里统一处理
_bearer = HTTPBearer(auto_error=False, description="API Key：Authorization: Bearer <key>")
_api_key = APIKeyHeader(name="X-API-Key", auto_error=False, description="API Key：X-API-Key: <key>")


# 不需要鉴权的接口（路径, 方法）。其余每个接口都必须声明角色，tests/test_auth.py 会逐个检查
PUBLIC_ROUTES = {("/health", "GET"), ("/admin", "GET"), ("/admin/", "GET"), ("/mcp", "GET"), ("/mcp", "DELETE")}


def _requirement(dependant) -> tuple[str, bool] | None:
    """在接口的依赖树里找 need() 声明的角色要求。"""
    for d in dependant.dependencies:
        req = getattr(d.call, "_auth", None) or _requirement(d)
        if req:
            return req
    return None


class AuthRoute(APIRoute):
    """先鉴权、再解析参数和请求体：未授权的请求一律先得到 401 / 403 / 429，拿不到 422 之类的校验信息，
    也不消耗请求体解析。角色写进 OpenAPI 的 x-required-role。"""

    def __init__(self, path: str, endpoint, **kw):
        super().__init__(path, endpoint, **kw)
        self.auth = _requirement(self.dependant)
        if self.auth:
            role, global_only = self.auth
            self.openapi_extra = {**(self.openapi_extra or {}), "x-required-role": role,
                                  **({"x-global-admin-only": True} if global_only else {})}

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            if self.auth is not None:
                request.app.state.authorize(request, *self.auth)
            return await handler(request)

        return guarded


def _cs(value: str | None) -> coords.CoordSys:
    try:
        return coords.CoordSys.parse(value)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


def _location(value: str) -> tuple[float, float]:
    try:
        lng_s, lat_s = value.replace("，", ",").split(",")
        lng, lat = float(lng_s), float(lat_s)
    except ValueError as e:
        raise HTTPException(400, "location 格式应为 '经度,纬度'") from e
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        raise HTTPException(400, "经纬度超出范围")
    return lng, lat


def _out(obj: Any, cs: coords.CoordSys) -> Any:
    """把结果里所有 {lng, lat} 从 WGS84 转到目标坐标系（返回新对象，不改缓存）。"""
    return coords.output(obj, cs)


class GeocodeItem(BaseModel):
    id: str | int | None = None
    address: str = Field(..., min_length=1, max_length=500)
    city: str | None = None


class GeocodeBatch(BaseModel):
    items: list[GeocodeItem] = Field(..., max_length=MAX_BATCH)
    coordsys: str | None = None
    limit: int = Field(1, ge=1, le=10)


class Feedback(BaseModel):
    address: str = Field(..., min_length=1, max_length=500)
    location: str = Field(..., description="确认后的坐标 '经度,纬度'")
    coordsys: str | None = None
    source: str | None = Field(None, max_length=50, description="确认来源：签收、人工核验等")


class ReverseItem(BaseModel):
    id: str | int | None = None
    location: str


class ReverseBatch(BaseModel):
    items: list[ReverseItem] = Field(..., max_length=MAX_BATCH)
    coordsys: str | None = None
    radius: float = Field(500, ge=10, le=3000)
    poi_limit: int = Field(5, ge=0, le=50)


class PlaceIn(BaseModel):
    """要素输入。修改时只传要改的字段；字段传 null 表示清空（adcode 清空后重新推断）。"""

    uid: str | None = Field(None, max_length=64, description="新增时可自定义，默认自动生成")
    kind: Literal["poi", "address", "road"] | None = None
    name: str | None = Field(None, max_length=200)
    alias: list[str] | str | None = Field(None, description="别名，数组或 '|' 分隔")
    address: str | None = Field(None, max_length=500)
    road: str | None = Field(None, max_length=100)
    road_no: str | None = Field(None, max_length=50)
    adcode: str | None = Field(None, max_length=12, description="区划编码；留空按地址与坐标推断")
    location: str | None = Field(None, description="'经度,纬度'（与 lng/lat 二选一）")
    lng: float | None = Field(None, ge=-180, le=180)
    lat: float | None = Field(None, ge=-90, le=90)
    coordsys: str | None = None
    weight: float | None = None
    geometry: dict | None = Field(None, description="道路几何（GeoJSON LineString / MultiLineString）")

    def to_data(self, partial: bool) -> dict:
        d = self.model_dump(exclude_unset=partial, exclude_none=not partial)
        loc = d.pop("location", None)
        if loc:
            d["lng"], d["lat"] = _location(loc)
        if isinstance(d.get("alias"), list):
            d["alias"] = "|".join(a for a in d["alias"] if a)
        if d.get("coordsys") is not None:
            d["coordsys"] = _cs(d["coordsys"]).value
        return d


class MemoryIn(BaseModel):
    address: str = Field(..., min_length=1, max_length=500)
    location: str = Field(..., description="确认坐标 '经度,纬度'")
    coordsys: str | None = None
    count: int = Field(1, ge=1, le=100000, description="确认次数，越多越可信")
    spread: float = Field(0, ge=0, le=300, description="多次确认的离散程度（米）")


class ReviewAction(BaseModel):
    action: Literal["approve", "dismiss"]
    location: str | None = Field(None, description="审核通过时修正后的坐标 '经度,纬度'，可选")
    coordsys: str | None = None


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    cfg = settings or (engine.settings if engine is not None else Settings.from_env())
    ring = KeyRing.from_settings(cfg)  # 配置错误时启动即失败
    holder: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        e = engine or Engine(cfg)
        holder["engine"] = e
        holder["mcp"] = McpServer(e)
        if not ring.enabled:
            log.warning("未配置 API Key：查询与反馈接口无需鉴权，数据管理与 MCP 维护工具不可用")
        yield

    docs = cfg.public_docs
    app = FastAPI(title="GeoVerse 中国地址编码服务", version=__version__, lifespan=lifespan,
                  docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None,
                  openapi_url="/openapi.json" if docs else None)
    app.router.route_class = AuthRoute
    app.state.keyring = ring

    @app.exception_handler(AuthError)
    async def _auth_error(_req: Request, e: AuthError):
        return JSONResponse({"detail": e.detail}, status_code=e.status, headers=e.headers)

    @app.exception_handler(EditError)
    async def _edit_error(_req: Request, e: EditError):
        return JSONResponse({"detail": str(e)}, status_code=e.status)

    def eng() -> Engine:
        e: Engine = holder["engine"]
        e.sync()
        return e

    def _principal(request: Request, role: str, global_only: bool = False) -> Principal:
        token = token_from(request.headers)
        if token is None and ring.allow_query_key:
            token = request.query_params.get("key") or None
        client = request.client.host if request.client else "unknown"
        p = ring.check(token, client, role, global_only)
        request.state.principal = p
        return p

    app.state.authorize = _principal

    def need(role: str, global_only: bool = False):
        """声明接口需要的角色。实际检查在 AuthRoute 里、解析请求之前完成，这里只取出结果。"""

        def dep(request: Request, _b=Depends(_bearer), _k=Depends(_api_key)) -> Principal:
            p = getattr(request.state, "principal", None)
            return p if p is not None else _principal(request, role, global_only)

        dep._auth = (role, global_only)
        return Depends(dep)

    read, write, admin, root = need("read"), need("write"), need("admin"), need("admin", global_only=True)

    def charge(p: Principal, n: int) -> None:
        """批量接口按条数计入限流。"""
        ring.charge(p, n)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/auth/whoami", tags=["auth"])
    def whoami(p: Principal = need("none")) -> dict:
        """当前调用方及其角色。管理界面登录时用来校验密钥。"""
        return {**p.public(), "auth_enabled": ring.enabled}

    @app.get("/v1/meta")
    def meta(p: Principal = read) -> dict:
        info = eng().info()
        if not (p.allows("admin") and not p.scoped):
            info.pop("db", None)  # 服务器上的文件路径只给全局管理员看
        return info

    @app.get("/v1/parse")
    def parse(address: str = Query(..., min_length=1, max_length=500), city: str | None = None,
              _p: Principal = read) -> dict:
        p = eng().parser.parse(address, city=city)
        d = p.to_dict()
        d["alternatives"] = [a.to_dict()["adcode"] for a in p.alternatives]
        return d

    @app.get("/v1/geocode")
    def geocode(address: str = Query(..., min_length=1, max_length=500), city: str | None = None,
                coordsys: str | None = None, focus: str | None = None, limit: int = Query(1, ge=1, le=10),
                fallback: bool = True, p: Principal = read) -> dict:
        cs = _cs(coordsys)
        f = None
        if focus:
            f = coords.to_wgs84(*_location(focus), cs)
        res = eng().geocoder.geocode(address, city=city, focus=f, limit=limit, use_fallback=fallback and p.fallback)
        out = {**_out(res, cs), "coordsys": cs.value}
        if fallback and not p.fallback:
            out["fallback_permitted"] = False  # 该密钥（或匿名调用方）不允许消耗兜底服务配额
        return out

    @app.post("/v1/geocode/batch")
    def geocode_batch(body: GeocodeBatch, p: Principal = read) -> dict:
        charge(p, len(body.items))
        cs = _cs(body.coordsys)
        g = eng().geocoder
        out = []
        for it in body.items:
            r = g.geocode(it.address, city=it.city, limit=body.limit, use_fallback=p.fallback)
            out.append({"id": it.id, **_out(r, cs)})
        return {"coordsys": cs.value, "count": len(out), "items": out}

    @app.get("/v1/reverse")
    def reverse(location: str, coordsys: str | None = None, radius: float = Query(500, ge=10, le=3000),
                poi_limit: int = Query(5, ge=0, le=50), _p: Principal = read) -> dict:
        cs = _cs(coordsys)
        lng, lat = coords.to_wgs84(*_location(location), cs)
        res = eng().reverse.reverse(lng, lat, radius=radius, poi_limit=poi_limit)
        return {**_out(res, cs), "coordsys": cs.value}

    @app.post("/v1/reverse/batch")
    def reverse_batch(body: ReverseBatch, p: Principal = read) -> dict:
        charge(p, len(body.items))
        cs = _cs(body.coordsys)
        rv = eng().reverse
        out = []
        for it in body.items:
            lng, lat = coords.to_wgs84(*_location(it.location), cs)
            out.append({"id": it.id, **_out(rv.reverse(lng, lat, body.radius, body.poi_limit), cs)})
        return {"coordsys": cs.value, "count": len(out), "items": out}

    @app.post("/v1/feedback", status_code=202)
    def feedback(body: Feedback, p: Principal = write) -> dict:
        """记录一条“地址写法 → 确认坐标”（签收点、人工核验）。进入管理界面的待审核队列，审核通过后立即生效；
        也可在下次建库时用 --memory 导入。只追加日志，不直接改在线数据。"""
        path = eng().settings.feedback_log
        if not path:
            raise HTTPException(404, "未配置 GEOCODER_FEEDBACK_LOG，反馈功能未开启")
        cs = _cs(body.coordsys)
        lng, lat = coords.to_wgs84(*_location(body.location), cs)
        line = json.dumps({"address": body.address, "lng": round(lng, 7), "lat": round(lat, 7), "coordsys": "wgs84",
                           "source": body.source, "by": p.name if not p.anonymous else None,
                           "ts": int(time.time())}, ensure_ascii=False)
        with _feedback_lock, open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return {"status": "accepted"}

    @app.get("/v1/convert")
    def convert(location: str, src: str = Query("wgs84", alias="from"), dst: str = Query("gcj02", alias="to"),
                _p: Principal = read) -> dict:
        s, d = _cs(src), _cs(dst)
        lng, lat = coords.convert(*_location(location), s, d)
        return {"location": {"lng": round(lng, 7), "lat": round(lat, 7)}, "from": s.value, "to": d.value}

    # ------------------------------------------------------------------ 数据管理（admin）
    def mgr():
        return eng().manager

    @app.get("/v1/admin/overview", tags=["admin"])
    def admin_overview(p: Principal = admin) -> dict:
        e = eng()
        info = e.info()
        info["review"] = {k: bool(v) for k, v in
                          (("review", e.settings.review_log), ("feedback", e.settings.feedback_log))}
        info["review"]["pending"] = e.manager.review_items(limit=0, regions=p.regions)["total"]
        if p.scoped:
            info.pop("db", None)
        return {**info, "whoami": p.public(), "auth": None if p.scoped else ring.describe(),
                "mcp": {"path": "/mcp", "protocol_versions": PROTOCOL_VERSIONS}}

    @app.get("/v1/admin/places", tags=["admin"])
    def admin_places(q: str | None = Query(None, max_length=100), kind: str | None = None,
                     adcode: str | None = None, source: str | None = None, offset: int = Query(0, ge=0),
                     limit: int = Query(20, ge=1, le=200), coordsys: str | None = None,
                     p: Principal = admin) -> dict:
        cs = _cs(coordsys)
        res = mgr().list_places(q, kind, adcode, source, offset, limit, regions=p.regions)
        return {**_out(res, cs), "coordsys": cs.value}

    @app.get("/v1/admin/places/{uid}", tags=["admin"])
    def admin_place(uid: str, coordsys: str | None = None, p: Principal = admin) -> dict:
        cs = _cs(coordsys)
        return {**_out(mgr().get_place(uid, regions=p.regions), cs), "coordsys": cs.value}

    @app.post("/v1/admin/places", status_code=201, tags=["admin"])
    def admin_create_place(body: PlaceIn, coordsys: str | None = None, p: Principal = admin) -> dict:
        cs = _cs(body.coordsys or coordsys)
        data = {"coordsys": cs.value, **body.to_data(partial=False)}  # 查询参数里的 coordsys 也作用于输入
        return {**_out(mgr().create_place(data, actor=p.name, regions=p.regions), cs), "coordsys": cs.value}

    @app.patch("/v1/admin/places/{uid}", tags=["admin"])
    def admin_update_place(uid: str, body: PlaceIn, coordsys: str | None = None, p: Principal = admin) -> dict:
        cs = _cs(body.coordsys or coordsys)
        data = body.to_data(partial=True)
        data.pop("uid", None)
        data.pop("coordsys", None)
        if not data:
            raise HTTPException(400, "没有要修改的字段")
        data["coordsys"] = cs.value
        return {**_out(mgr().update_place(uid, data, actor=p.name, regions=p.regions), cs), "coordsys": cs.value}

    @app.delete("/v1/admin/places/{uid}", tags=["admin"])
    def admin_delete_place(uid: str, p: Principal = admin) -> dict:
        return {"deleted": mgr().delete_place(uid, actor=p.name, regions=p.regions)}

    @app.get("/v1/admin/memory", tags=["admin"])
    def admin_memory(q: str | None = Query(None, max_length=200), offset: int = Query(0, ge=0),
                     limit: int = Query(20, ge=1, le=200), coordsys: str | None = None,
                     p: Principal = admin) -> dict:
        cs = _cs(coordsys)
        return {**_out(mgr().list_memory(q, offset, limit, regions=p.regions), cs), "coordsys": cs.value}

    @app.post("/v1/admin/memory", status_code=201, tags=["admin"])
    def admin_add_memory(body: MemoryIn, p: Principal = admin) -> dict:
        cs = _cs(body.coordsys)
        lng, lat = _location(body.location)
        rows = mgr().add_memory(body.address, lng, lat, coordsys=cs.value, count=body.count, spread=body.spread,
                                actor=p.name, regions=p.regions)
        return {"items": _out(rows, cs), "coordsys": cs.value}

    @app.delete("/v1/admin/memory", tags=["admin"])
    def admin_delete_memory(key: str = Query(..., min_length=1), p: Principal = admin) -> dict:
        return {"deleted": mgr().delete_memory(key, actor=p.name, regions=p.regions)}

    @app.get("/v1/admin/review", tags=["admin"])
    def admin_review(source: Literal["review", "feedback"] | None = None,
                     status: Literal["pending", "approved", "dismissed", "all"] = "pending",
                     offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=200),
                     coordsys: str | None = None, p: Principal = admin) -> dict:
        cs = _cs(coordsys)
        res = mgr().review_items(source, status, offset, limit, regions=p.regions)
        return {**_out(res, cs), "coordsys": cs.value}

    @app.post("/v1/admin/review/{item_id}", tags=["admin"])
    def admin_resolve(item_id: str, body: ReviewAction, p: Principal = admin) -> dict:
        cs = _cs(body.coordsys)
        lng = lat = None
        if body.location:
            lng, lat = _location(body.location)
        return _out(mgr().resolve_review(item_id, body.action, lng, lat, coordsys=cs.value, actor=p.name,
                                            regions=p.regions), cs)

    @app.get("/v1/admin/regions", tags=["admin"])
    def admin_regions(q: str = Query(..., min_length=1, max_length=50), limit: int = Query(20, ge=1, le=100),
                      _p: Principal = read) -> dict:
        return {"items": mgr().search_regions(q, limit)}

    @app.get("/v1/admin/changes", tags=["admin"])
    def admin_changes(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500), op: str | None = None,
                      p: Principal = admin) -> dict:
        """区域管理员只能看到自己的修改。"""
        return mgr().list_changes(offset, limit, op, actor=p.name if p.scoped else None)

    @app.get("/v1/admin/changes/export", tags=["admin"])
    def admin_export(_p: Principal = root) -> StreamingResponse:
        """全部变更日志（JSONL），下次建库时 cngeocoder build ... --edits <文件> 重放。"""
        name = f"edits-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        return StreamingResponse(mgr().export_changes(), media_type="application/x-ndjson",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/v1/admin/auth", tags=["admin"])
    def admin_auth(_p: Principal = root) -> dict:
        """密钥配置概况与调用计数（不含密钥本身）。"""
        return ring.describe()

    # ------------------------------------------------------------------ 管理界面
    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_ui() -> HTMLResponse:
        # 页面本身不含数据，所有数据请求都带 admin 密钥走 /v1/admin/*
        return HTMLResponse((_STATIC / "admin.html").read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-cache", "X-Frame-Options": "DENY",
                                     "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                                "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"})

    # ------------------------------------------------------------------ MCP
    allowed_origins = {o.strip() for o in (cfg.mcp_origins or "").split(",") if o.strip()}

    @app.post("/mcp", tags=["mcp"])
    async def mcp_post(request: Request, p: Principal = read) -> Response:
        """MCP Streamable HTTP 入口（JSON-RPC 2.0）。工具列表按调用方角色过滤。"""
        origin = request.headers.get("origin")
        if origin and "*" not in allowed_origins and origin not in allowed_origins \
                and urlparse(origin).netloc != request.headers.get("host"):
            # 防 DNS 重绑定：浏览器发起的跨源请求默认拒绝
            raise HTTPException(403, f"Origin 不在允许列表: {origin}（GEOCODER_MCP_ORIGINS）")
        try:
            payload = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON 解析失败"}},
                                status_code=400)
        version = request.headers.get("mcp-protocol-version")
        if version and version not in PROTOCOL_VERSIONS:
            # 较新的客户端会先用新版本试探；按 -32022 回复支持的版本，客户端随即改用 initialize 握手
            mid = payload.get("id") if isinstance(payload, dict) else None
            return JSONResponse({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32022, "message": f"不支持的 MCP 协议版本: {version}",
                "data": {"supported": list(PROTOCOL_VERSIONS), "requested": version}}}, status_code=400)
        resp = await run_in_threadpool(holder["mcp"].handle_payload, payload, p)
        if resp is None:
            return Response(status_code=202)
        return JSONResponse(resp)

    @app.get("/mcp", include_in_schema=False)
    @app.delete("/mcp", include_in_schema=False)
    def mcp_other() -> Response:
        # 无状态服务：不提供服务端推送流，也没有会话可删除
        return Response(status_code=405, headers={"Allow": "POST"})

    return app


def app_factory() -> FastAPI:
    """uvicorn --factory 入口，配置从环境变量读取。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return create_app()
