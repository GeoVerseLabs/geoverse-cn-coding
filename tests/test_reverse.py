from .conftest import ROAD_LAT0, ROAD_LNG, STEP


def test_reverse_with_boundary(engine):
    r = engine.reverse.reverse(ROAD_LNG + 0.0003, ROAD_LAT0 + 21 * STEP)
    assert r["admin_source"].startswith("boundary")
    assert r["adcode"]["district"] == "110108"
    assert r["components"]["road"] == "测试路"
    assert r["address"]["road_no"] in ("21号", "19号", "23号")
    assert r["road"]["name"] == "测试路" and r["road"]["distance"] < 30
    assert r["pois"][0]["name"] == "测试大厦"
    assert r["description"].startswith("测试大厦")
    assert r["formatted_address"].startswith("北京市海淀区测试路")


def test_reverse_direction_text(engine):
    r = engine.reverse.reverse(ROAD_LNG + 0.0012, ROAD_LAT0 + 21 * STEP - 0.0005)
    assert r["description"].startswith("测试大厦东南")


def test_reverse_without_boundary_votes_by_nearby(engine_no_boundary):
    r = engine_no_boundary.reverse.reverse(ROAD_LNG + 0.001, ROAD_LAT0 + 0.003)
    assert r["admin_source"] == "nearby"
    assert r["adcode"]["district"] == "110108"


def test_reverse_falls_back_to_centroid(engine_no_boundary):
    r = engine_no_boundary.reverse.reverse(125.30, 43.84)  # 3 km 内无数据，最近中心点是长春朝阳区
    assert r["admin_source"] in ("centroid", "nearby")
    assert r["adcode"]["district"] == "220104"


def test_reverse_nothing_far_away(engine):
    r = engine.reverse.reverse(90.0, 30.0)
    assert r["status"] == "not_found"
