"""HTTP API（FastAPI）。

所有接口都支持 ``coordsys`` 参数（wgs84 / gcj02 / bd09 / cgcs2000），只在出入口转换，内部统一 WGS84。
接口是同步函数，FastAPI 放到线程池执行；SQLite 每线程一个只读连接。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import __version__, coords
from .engine import Engine, Settings

log = logging.getLogger(__name__)
MAX_BATCH = 1000


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
    if cs.base is coords.CoordSys.WGS84:
        return obj
    if isinstance(obj, dict):
        if obj.keys() == {"lng", "lat"}:
            lng, lat = coords.from_wgs84(obj["lng"], obj["lat"], cs)
            return {"lng": round(lng, 7), "lat": round(lat, 7)}
        return {k: _out(v, cs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_out(v, cs) for v in obj]
    return obj


class GeocodeItem(BaseModel):
    id: str | int | None = None
    address: str = Field(..., min_length=1, max_length=500)
    city: str | None = None


class GeocodeBatch(BaseModel):
    items: list[GeocodeItem] = Field(..., max_length=MAX_BATCH)
    coordsys: str | None = None
    limit: int = Field(1, ge=1, le=10)


class ReverseItem(BaseModel):
    id: str | int | None = None
    location: str


class ReverseBatch(BaseModel):
    items: list[ReverseItem] = Field(..., max_length=MAX_BATCH)
    coordsys: str | None = None
    radius: float = Field(500, ge=10, le=3000)
    poi_limit: int = Field(5, ge=0, le=50)


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    holder: dict[str, Engine] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        holder["engine"] = engine or Engine(settings or Settings.from_env())
        yield

    app = FastAPI(title="GeoVerse 中国地址编码服务", version=__version__, lifespan=lifespan)

    def eng() -> Engine:
        return holder["engine"]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/meta")
    def meta() -> dict:
        return eng().info()

    @app.get("/v1/parse")
    def parse(address: str = Query(..., min_length=1, max_length=500), city: str | None = None) -> dict:
        p = eng().parser.parse(address, city=city)
        d = p.to_dict()
        d["alternatives"] = [a.to_dict()["adcode"] for a in p.alternatives]
        return d

    @app.get("/v1/geocode")
    def geocode(address: str = Query(..., min_length=1, max_length=500), city: str | None = None,
                coordsys: str | None = None, focus: str | None = None, limit: int = Query(1, ge=1, le=10),
                fallback: bool = True) -> dict:
        cs = _cs(coordsys)
        f = None
        if focus:
            f = coords.to_wgs84(*_location(focus), cs)
        res = eng().geocoder.geocode(address, city=city, focus=f, limit=limit, use_fallback=fallback)
        return {**_out(res, cs), "coordsys": cs.value}

    @app.post("/v1/geocode/batch")
    def geocode_batch(body: GeocodeBatch) -> dict:
        cs = _cs(body.coordsys)
        g = eng().geocoder
        out = []
        for it in body.items:
            r = g.geocode(it.address, city=it.city, limit=body.limit)
            out.append({"id": it.id, **_out(r, cs)})
        return {"coordsys": cs.value, "count": len(out), "items": out}

    @app.get("/v1/reverse")
    def reverse(location: str, coordsys: str | None = None, radius: float = Query(500, ge=10, le=3000),
                poi_limit: int = Query(5, ge=0, le=50)) -> dict:
        cs = _cs(coordsys)
        lng, lat = coords.to_wgs84(*_location(location), cs)
        res = eng().reverse.reverse(lng, lat, radius=radius, poi_limit=poi_limit)
        return {**_out(res, cs), "coordsys": cs.value}

    @app.post("/v1/reverse/batch")
    def reverse_batch(body: ReverseBatch) -> dict:
        cs = _cs(body.coordsys)
        rv = eng().reverse
        out = []
        for it in body.items:
            lng, lat = coords.to_wgs84(*_location(it.location), cs)
            out.append({"id": it.id, **_out(rv.reverse(lng, lat, body.radius, body.poi_limit), cs)})
        return {"coordsys": cs.value, "count": len(out), "items": out}

    @app.get("/v1/convert")
    def convert(location: str, src: str = Query("wgs84", alias="from"), dst: str = Query("gcj02", alias="to")) -> dict:
        s, d = _cs(src), _cs(dst)
        lng, lat = coords.convert(*_location(location), s, d)
        return {"location": {"lng": round(lng, 7), "lat": round(lat, 7)}, "from": s.value, "to": d.value}

    return app


def app_factory() -> FastAPI:
    """uvicorn --factory 入口，配置从环境变量读取。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return create_app()
