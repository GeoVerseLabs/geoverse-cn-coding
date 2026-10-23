import pytest

from cngeocoder import coords
from cngeocoder.geo import haversine

from .conftest import ROAD_LAT0, ROAD_LNG, STEP


def best(engine, address, **kw):
    res = engine.geocoder.geocode(address, **kw)
    assert res["status"] == "ok", res
    return res["results"][0]


def test_door_exact(engine):
    r = best(engine, "北京市海淀区测试路35号")
    assert r["precision"] == "door"
    assert r["location"]["lat"] == pytest.approx(ROAD_LAT0 + 35 * STEP, abs=1e-7)
    assert r["confidence"] >= 0.9
    assert r["adcode"]["district"] == "110108"


def test_door_prefers_named_poi_when_poi_given(engine):
    r = best(engine, "海淀区测试路21号测试大厦")
    assert r["precision"] == "door" and r["matched"]["name"] == "测试大厦"


def test_door_interpolation(engine):
    r = best(engine, "北京市海淀区测试路40号")  # 只有单号数据，按 39/41 插值
    assert r["precision"] == "interpolated"
    assert r["location"]["lat"] == pytest.approx(ROAD_LAT0 + 40 * STEP, abs=2 * STEP)
    assert r["confidence"] >= 0.6


def test_door_beyond_range_falls_back_to_road(engine):
    r = best(engine, "北京市海淀区测试路500号")
    assert r["precision"] == "road"
    assert r["location"]["lng"] == pytest.approx(ROAD_LNG, abs=1e-6)


def test_poi_fuzzy_and_alias(engine):
    r = best(engine, "海淀区测试大厦")
    assert r["precision"] == "poi" and r["matched"]["name"] == "测试大厦"
    r = best(engine, "测大", city="北京")
    assert r["matched"]["name"] == "测试大厦"


def test_poi_without_admin_gets_admin_from_data(engine):
    r = best(engine, "晨曦中心")
    assert r["adcode"]["district"] == "310115"
    assert r["components"]["province"] == "上海市"


def test_same_name_district_resolved_by_data(engine):
    # “朝阳区”默认解释为北京，但数据里星光广场在长春朝阳区
    r = best(engine, "朝阳区星光广场")
    assert r["adcode"]["district"] == "220104"
    assert r["components"]["city"] == "长春市"
    r = best(engine, "朝阳区月光花园")
    assert r["adcode"]["district"] == "110105"


def test_admin_only_returns_centroid_with_high_confidence(engine):
    r = best(engine, "北京市东城区")
    assert r["precision"] == "district"
    assert r["confidence"] >= 0.9


def test_coarse_match_has_low_confidence(engine):
    r = best(engine, "北京市东城区不存在的某某大厦")
    assert r["precision"] in ("district", "province")
    assert r["confidence"] < 0.6


def test_gcj02_input_converted_on_build(engine):
    r = best(engine, "火星坐标点")
    w = coords.gcj02_to_wgs84(116.40, 39.91)
    assert haversine(r["location"]["lng"], r["location"]["lat"], *w) < 0.5


def test_focus_biases_results(engine):
    res = engine.geocoder.geocode("星光广场", focus=(125.28, 43.86), limit=3)
    assert res["results"][0]["adcode"]["district"] == "220104"


def test_not_found(engine):
    res = engine.geocoder.geocode("完全无法识别的文本xyz")
    assert res["status"] == "not_found" or res["results"][0]["confidence"] < 0.5


def test_cache_hit(engine):
    engine.geocoder.geocode("北京市海淀区测试路35号")
    before = engine.geocoder.cache.hits
    engine.geocoder.geocode("北京市海淀区测试路35号")
    assert engine.geocoder.cache.hits == before + 1


class FakeFallback:
    def __init__(self):
        self.calls = 0

    def geocode(self, address):
        from cngeocoder.fallback import FallbackResult

        self.calls += 1
        return FallbackResult(116.5, 39.95, "poi", 0.85, "fake", {})


def test_fallback_only_when_low_confidence(engine, tmp_path):
    fb = FakeFallback()
    g = engine.geocoder
    old = (g.fallback, g.review_log)
    g.fallback, g.review_log = fb, str(tmp_path / "review.jsonl")
    g.cache = type(g.cache)(100)
    try:
        r = g.geocode("北京市海淀区测试路35号")["results"][0]
        assert r["source"] == "local" and fb.calls == 0
        r = g.geocode("北京市东城区不存在的某某大厦")["results"][0]
        assert r["source"] == "fake" and fb.calls == 1
        assert (tmp_path / "review.jsonl").read_text(encoding="utf-8").count("\n") == 1
        r = g.geocode("北京市东城区")["results"][0]  # 只要区县级，本地已满足，不走兜底
        assert r["source"] == "local" and fb.calls == 1
    finally:
        g.fallback, g.review_log = old


def test_building_detail_does_not_lower_poi_confidence(engine):
    r = best(engine, "海淀区测试大厦B座1201室")
    assert r["precision"] == "poi" and r["confidence"] >= 0.8
    assert r["formatted_address"].endswith("测试大厦B座1201室")


def test_builtin_centroids_cover_whole_country(engine):
    # 内置 data/admin/centroids.csv（Overture/OSM 派生）让任意区县、乡镇都能给出坐标
    r = best(engine, "云南省普洱市孟连傣族拉祜族佤族自治县")
    assert r["precision"] == "district" and r["confidence"] >= 0.9
    assert r["location"]["lng"] == pytest.approx(99.59, abs=0.05)
    r = best(engine, "广东省东莞市虎门镇")
    assert r["precision"] == "town" and r["adcode"]["town"].startswith("441900")
