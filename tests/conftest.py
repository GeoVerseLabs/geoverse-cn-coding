"""测试夹具：用真实区划词典 + 合成的 POI/门牌/道路/边界建一个小库。

合成数据只用于验证算法行为（插值、消歧、点落面等），坐标不对应真实地物。
"""

from __future__ import annotations

import csv
import json
import os

import pytest

from cngeocoder.admin import AdminIndex, load_admin_aliases, load_admin_csvs
from cngeocoder.build import BuildInputs, build
from cngeocoder.engine import Engine, Settings
from cngeocoder.parser import AddressParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_DIR = os.path.join(ROOT, "data", "admin")

# 合成门牌所在道路：海淀区“测试路”，1~99 号单号沿经线方向排布（每号约 11 米）
ROAD_LNG, ROAD_LAT0, STEP = 116.30, 39.95, 0.0001


def _square(lng, lat, half):
    return {"type": "Polygon", "coordinates": [[[lng - half, lat - half], [lng + half, lat - half],
                                                 [lng + half, lat + half], [lng - half, lat + half],
                                                 [lng - half, lat - half]]]}


@pytest.fixture(scope="session")
def admin_index() -> AdminIndex:
    return AdminIndex(load_admin_csvs(ADMIN_DIR), load_admin_aliases(ADMIN_DIR))


@pytest.fixture(scope="session")
def parser(admin_index) -> AddressParser:
    return AddressParser(admin_index)


@pytest.fixture(scope="session")
def fixture_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("data")
    rows = []
    for n in range(1, 100, 2):
        rows.append({"kind": "address", "address": f"北京市海淀区测试路{n}号", "road": "测试路", "road_no": f"{n}号",
                     "lng": ROAD_LNG, "lat": ROAD_LAT0 + n * STEP, "adcode": "110108"})
    rows += [
        {"name": "测试大厦", "kind": "poi", "address": "北京市海淀区测试路21号", "road": "测试路", "road_no": "21号",
         "lng": ROAD_LNG + 0.0002, "lat": ROAD_LAT0 + 21 * STEP, "adcode": "110108", "alias": "测大"},
        # 同名 POI 分别在北京朝阳区和长春朝阳区，用来验证同名区县消歧
        {"name": "星光广场", "kind": "poi", "address": "吉林省长春市朝阳区星光路8号", "road": "星光路", "road_no": "8号",
         "lng": 125.28, "lat": 43.86},
        {"name": "月光花园", "kind": "poi", "address": "北京市朝阳区月光路1号", "road": "月光路", "road_no": "1号",
         "lng": 116.47, "lat": 39.92},
        # 没有给 adcode，需要靠地址解析补全
        {"name": "晨曦中心", "kind": "poi", "address": "上海市浦东新区晨曦路100号", "lng": 121.55, "lat": 31.22},
        # GCJ-02 输入，建库时转 WGS84
        {"name": "火星坐标点", "kind": "poi", "address": "北京市东城区火星路1号", "lng": 116.40, "lat": 39.91,
         "coordsys": "gcj02"},
        # 口语化测试：测试大厦马路对面（路西约 120 米）的便利店；另一城市的同名“星光广场”；用来生成简称的体育场
        {"name": "星光便利店", "kind": "poi", "address": "北京市海淀区测试路西侧", "lng": ROAD_LNG - 0.0012,
         "lat": ROAD_LAT0 + 21 * STEP, "adcode": "110108"},
        {"name": "星光广场", "kind": "poi", "address": "上海市浦东新区星光路2号", "lng": 121.52, "lat": 31.22},
        {"name": "北京晨光体育场", "kind": "poi", "address": "北京市海淀区晨光路1号", "lng": 116.32, "lat": 39.96,
         "adcode": "110108"},
    ]
    places = d / "places.csv"
    with open(places, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "kind", "address", "road", "road_no", "lng", "lat", "adcode",
                                          "coordsys", "alias"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    roads = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "测试路", "adcode": "110108"},
         "geometry": {"type": "LineString", "coordinates": [[ROAD_LNG, ROAD_LAT0], [ROAD_LNG, ROAD_LAT0 + 0.011]]}},
        # 与测试路在 (ROAD_LNG, ROAD_LAT0 + 0.005) 相交
        {"type": "Feature", "properties": {"name": "交叉路", "adcode": "110108"},
         "geometry": {"type": "LineString", "coordinates": [[ROAD_LNG - 0.005, ROAD_LAT0 + 0.005],
                                                            [ROAD_LNG + 0.005, ROAD_LAT0 + 0.005]]}},
    ]}
    (d / "roads.geojson").write_text(json.dumps(roads, ensure_ascii=False), encoding="utf-8")

    # 合成边界：海淀区 = 以测试路为中心的方块；其中北半部再切一个乡镇（海淀街道 110108001 之类）
    bounds = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"adcode": "110108"}, "geometry": _square(ROAD_LNG, ROAD_LAT0 + 0.005, 0.05)},
        {"type": "Feature", "properties": {"adcode": "110105"}, "geometry": _square(116.47, 39.92, 0.05)},
    ]}
    (d / "bounds.geojson").write_text(json.dumps(bounds), encoding="utf-8")

    # 历史确认结果：同一口语写法被确认 3 次（CSV），以及 /v1/feedback 日志格式（JSONL）
    with open(d / "memory.csv", "w", encoding="utf-8") as f:
        f.write("address,lng,lat,count\n")
        f.write(f"北京市海淀区测试路那个红色大门的院子,{ROAD_LNG + 0.0005},{ROAD_LAT0 + 0.0081},3\n")
    (d / "feedback.jsonl").write_text(
        json.dumps({"address": "海淀区测试大厦后面的停车场", "location": {"lng": ROAD_LNG + 0.0003, "lat": ROAD_LAT0 + 0.0025},
                    "coordsys": "wgs84"}, ensure_ascii=False) + "\n", encoding="utf-8")

    with open(d / "centroids.csv", "w", encoding="utf-8") as f:
        f.write("adcode,lng,lat\n110000,116.39,39.90\n220104,125.29,43.83\n110101,116.41,39.93\n")
    return d


def _build(fixture_dir, out, boundaries: bool) -> str:
    build(BuildInputs(
        admin_dir=ADMIN_DIR, out=str(out),
        places=(str(fixture_dir / "places.csv"),), roads=(str(fixture_dir / "roads.geojson"),),
        centroids=(str(fixture_dir / "centroids.csv"),),
        memory=(str(fixture_dir / "memory.csv"), str(fixture_dir / "feedback.jsonl")),
        boundaries=(str(fixture_dir / "bounds.geojson"),) if boundaries else (),
    ))
    return str(out)


@pytest.fixture(scope="session")
def engine(fixture_dir, tmp_path_factory) -> Engine:
    db = _build(fixture_dir, tmp_path_factory.mktemp("db") / "g.db", boundaries=True)
    return Engine(Settings(db_path=db))


@pytest.fixture(scope="session")
def engine_no_boundary(fixture_dir, tmp_path_factory) -> Engine:
    db = _build(fixture_dir, tmp_path_factory.mktemp("db2") / "g.db", boundaries=False)
    return Engine(Settings(db_path=db))
