"""国内常见坐标系互转：WGS84 / GCJ-02 / BD-09。

内部统一存 WGS84（CGCS2000 与 WGS84 的差异在米级以下，按同一坐标系处理），
只在 API 出入口做转换。GCJ-02 → WGS84 没有解析逆，用迭代逼近，收敛误差远小于 1 米。
"""

from __future__ import annotations

import math
from enum import Enum

import numpy as np

_A = 6378245.0
_EE = 0.00669342162296594323
_X_PI = math.pi * 3000.0 / 180.0


class CoordSys(str, Enum):
    WGS84 = "wgs84"
    GCJ02 = "gcj02"
    BD09 = "bd09"
    CGCS2000 = "cgcs2000"

    @classmethod
    def parse(cls, value: str | CoordSys | None) -> CoordSys:
        if value is None or value == "":
            return cls.WGS84
        if isinstance(value, CoordSys):
            return value
        v = value.strip().lower().replace("-", "").replace("_", "")
        aliases = {"wgs84": cls.WGS84, "gps": cls.WGS84, "gcj02": cls.GCJ02, "amap": cls.GCJ02,
                   "gaode": cls.GCJ02, "tencent": cls.GCJ02, "bd09": cls.BD09, "baidu": cls.BD09,
                   "bd09ll": cls.BD09, "cgcs2000": cls.CGCS2000, "tianditu": cls.CGCS2000}
        if v not in aliases:
            raise ValueError(f"不支持的坐标系: {value}")
        return aliases[v]

    @property
    def base(self) -> CoordSys:
        return CoordSys.WGS84 if self is CoordSys.CGCS2000 else self


def out_of_china(lng: float, lat: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _delta(lng, lat):
    """GCJ-02 偏移量，lng/lat 可以是标量或 numpy 数组。"""
    x, y = lng - 105.0, lat - 35.0
    sqrt_abs_x = np.sqrt(np.abs(x))
    dlat = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * sqrt_abs_x
            + (20.0 * np.sin(6.0 * x * np.pi) + 20.0 * np.sin(2.0 * x * np.pi)) * 2.0 / 3.0
            + (20.0 * np.sin(y * np.pi) + 40.0 * np.sin(y / 3.0 * np.pi)) * 2.0 / 3.0
            + (160.0 * np.sin(y / 12.0 * np.pi) + 320 * np.sin(y * np.pi / 30.0)) * 2.0 / 3.0)
    dlng = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * sqrt_abs_x
            + (20.0 * np.sin(6.0 * x * np.pi) + 20.0 * np.sin(2.0 * x * np.pi)) * 2.0 / 3.0
            + (20.0 * np.sin(x * np.pi) + 40.0 * np.sin(x / 3.0 * np.pi)) * 2.0 / 3.0
            + (150.0 * np.sin(x / 12.0 * np.pi) + 300.0 * np.sin(x / 30.0 * np.pi)) * 2.0 / 3.0)
    radlat = lat / 180.0 * np.pi
    magic = 1 - _EE * np.sin(radlat) ** 2
    sqrtmagic = np.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * np.pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * np.cos(radlat) * np.pi)
    return dlng, dlat


def _delta_scalar(lng: float, lat: float) -> tuple[float, float]:
    """``_delta`` 的标量版本，用 math 避免 numpy 标量开销（在线请求走这里）。"""
    x, y = lng - 105.0, lat - 35.0
    sqrt_abs_x = math.sqrt(abs(x))
    pi = math.pi
    common = (20.0 * math.sin(6.0 * x * pi) + 20.0 * math.sin(2.0 * x * pi)) * 2.0 / 3.0
    dlat = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * sqrt_abs_x + common
            + (20.0 * math.sin(y * pi) + 40.0 * math.sin(y / 3.0 * pi)) * 2.0 / 3.0
            + (160.0 * math.sin(y / 12.0 * pi) + 320 * math.sin(y * pi / 30.0)) * 2.0 / 3.0)
    dlng = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * sqrt_abs_x + common
            + (20.0 * math.sin(x * pi) + 40.0 * math.sin(x / 3.0 * pi)) * 2.0 / 3.0
            + (150.0 * math.sin(x / 12.0 * pi) + 300.0 * math.sin(x / 30.0 * pi)) * 2.0 / 3.0)
    radlat = lat / 180.0 * pi
    magic = 1 - _EE * math.sin(radlat) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * pi)
    return dlng, dlat


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    if out_of_china(lng, lat):
        return lng, lat
    dlng, dlat = _delta_scalar(lng, lat)
    return lng + dlng, lat + dlat


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    if out_of_china(lng, lat):
        return lng, lat
    wlng, wlat = lng, lat
    for _ in range(20):
        glng, glat = wgs84_to_gcj02(wlng, wlat)
        dlng, dlat = glng - lng, glat - lat
        wlng, wlat = wlng - dlng, wlat - dlat
        if abs(dlng) < 1e-9 and abs(dlat) < 1e-9:
            break
    return wlng, wlat


def gcj02_to_bd09(lng: float, lat: float) -> tuple[float, float]:
    z = math.sqrt(lng * lng + lat * lat) + 0.00002 * math.sin(lat * _X_PI)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * _X_PI)
    return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


def bd09_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    x, y = lng - 0.0065, lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    return z * math.cos(theta), z * math.sin(theta)


def convert(lng: float, lat: float, src: str | CoordSys, dst: str | CoordSys) -> tuple[float, float]:
    """任意两种坐标系之间转换。"""
    s, d = CoordSys.parse(src).base, CoordSys.parse(dst).base
    if s is d:
        return lng, lat
    # 统一先转到 GCJ-02 再转出去
    if s is CoordSys.WGS84:
        g = wgs84_to_gcj02(lng, lat)
    elif s is CoordSys.BD09:
        g = bd09_to_gcj02(lng, lat)
    else:
        g = (lng, lat)
    if d is CoordSys.GCJ02:
        return g
    if d is CoordSys.WGS84:
        return gcj02_to_wgs84(*g)
    return gcj02_to_bd09(*g)


def to_wgs84(lng: float, lat: float, src: str | CoordSys) -> tuple[float, float]:
    return convert(lng, lat, src, CoordSys.WGS84)


def from_wgs84(lng: float, lat: float, dst: str | CoordSys) -> tuple[float, float]:
    return convert(lng, lat, CoordSys.WGS84, dst)


def gcj02_to_wgs84_array(coords: np.ndarray) -> np.ndarray:
    """批量 GCJ-02 → WGS84（N×2 数组），建库时转换整条边界用。"""
    lng, lat = coords[:, 0].astype(float), coords[:, 1].astype(float)
    inside = ~((lng <= 73.66) | (lng >= 135.05) | (lat <= 3.86) | (lat >= 53.55))
    wlng, wlat = lng.copy(), lat.copy()
    for _ in range(10):
        dlng, dlat = _delta(wlng, wlat)
        wlng = np.where(inside, wlng - (wlng + dlng - lng), wlng)
        wlat = np.where(inside, wlat - (wlat + dlat - lat), wlat)
    return np.column_stack([wlng, wlat])


def to_wgs84_array(coords: np.ndarray, src: str | CoordSys) -> np.ndarray:
    s = CoordSys.parse(src).base
    if s is CoordSys.WGS84:
        return coords
    if s is CoordSys.GCJ02:
        return gcj02_to_wgs84_array(coords)
    out = np.array([bd09_to_gcj02(x, y) for x, y in coords], dtype=float).reshape(-1, 2)
    return gcj02_to_wgs84_array(out)


def output(obj, cs: str | CoordSys):
    """把结果里所有 {lng, lat} 从 WGS84 转到目标坐标系（返回新对象，不改缓存）。"""
    cs = CoordSys.parse(cs)
    if cs.base is CoordSys.WGS84:
        return obj
    if isinstance(obj, dict):
        if obj.keys() == {"lng", "lat"} and obj["lng"] is not None and obj["lat"] is not None:
            lng, lat = from_wgs84(obj["lng"], obj["lat"], cs)
            return {"lng": round(lng, 7), "lat": round(lat, 7)}
        return {k: output(v, cs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [output(v, cs) for v in obj]
    return obj
