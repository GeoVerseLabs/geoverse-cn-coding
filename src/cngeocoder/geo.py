"""距离、方位和局部平面投影等几何小工具（全部基于 WGS84）。"""

from __future__ import annotations

import math

EARTH_RADIUS = 6371008.8
_DIRECTIONS = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]


def haversine(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """两点球面距离（米）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS * math.asin(min(1.0, math.sqrt(h)))


def bearing(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """从点 1 指向点 2 的方位角（度，正北为 0，顺时针）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def direction_cn(deg: float) -> str:
    return _DIRECTIONS[int(((deg % 360) + 22.5) // 45) % 8]


def bbox_around(lng: float, lat: float, radius_m: float) -> tuple[float, float, float, float]:
    """以点为中心、半径为 radius_m 的外接矩形 (min_lng, min_lat, max_lng, max_lat)。"""
    dlat = math.degrees(radius_m / EARTH_RADIUS)
    dlng = math.degrees(radius_m / (EARTH_RADIUS * max(math.cos(math.radians(lat)), 1e-6)))
    return lng - dlng, lat - dlat, lng + dlng, lat + dlat


class LocalProjection:
    """以某点为原点的等距矩形投影，几公里范围内误差可忽略，用来算点到线的米制距离。"""

    def __init__(self, lng0: float, lat0: float):
        self.lng0, self.lat0 = lng0, lat0
        self.kx = math.radians(1) * EARTH_RADIUS * math.cos(math.radians(lat0))
        self.ky = math.radians(1) * EARTH_RADIUS

    def forward(self, lng, lat):
        return (lng - self.lng0) * self.kx, (lat - self.lat0) * self.ky

    def inverse(self, x, y):
        return self.lng0 + x / self.kx, self.lat0 + y / self.ky
