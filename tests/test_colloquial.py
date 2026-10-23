"""口语化、非标准输入：归一化、方位描述、纠错、交叉口、简称、歧义、历史确认结果。"""

import pytest

from cngeocoder.colloquial import abbreviations, clean_colloquial, is_generic, to_simplified
from cngeocoder.geo import haversine
from cngeocoder.normalize import normalize, normalize_ex

from .conftest import ROAD_LAT0, ROAD_LNG, STEP


def best(engine, address, **kw):
    res = engine.geocoder.geocode(address, use_fallback=False, **kw)
    assert res["status"] == "ok", res
    return res["results"][0]


# ---------------------------------------------------------------- 归一化
def test_traditional_to_simplified():
    assert to_simplified("上海市浦東新區世紀大道100號") == "上海市浦东新区世纪大道100号"
    n = normalize_ex("臺北市信義區")
    assert n.traditional and n.text == "台北市信义区"
    assert to_simplified("乾县") == "乾县"  # 简体地名里的“乾”不改


@pytest.mark.parametrize("raw,clean", [
    ("麻烦送到北京西站南广场谢谢", "北京西站南广场"),
    ("放菜鸟驿站上海中心大厦", "上海中心大厦"),
    ("到了打电话外滩", "外滩"),
    ("白天鹅宾馆", "白天鹅宾馆"),  # “白天”只有接“送货”才算嘱咐语
    ("南锣鼓巷儿8号院儿负一楼", "南锣鼓巷8号院B1层"),
])
def test_clean_colloquial(raw, clean):
    assert clean_colloquial(raw)[0] == clean


def test_emoji_and_numbers_in_distance():
    assert normalize("国贸🏢附近") == "国贸附近"


def test_abbreviation_rules():
    pre = ("北京", "北京市")
    assert abbreviations("北京大学", pre) == ["北大"]
    assert abbreviations("中国人民大学", pre) == ["人大"]
    assert "北师大" in abbreviations("北京师范大学", pre)
    assert "工体" in abbreviations("北京工人体育场", pre)
    assert "首都机场" in abbreviations("北京首都国际机场", pre)
    assert "师范大学" not in abbreviations("北京师范大学", pre)  # 只剩类别词的不要
    assert is_generic("火车站") and is_generic("中心大厦") and not is_generic("环球金融中心")


# ---------------------------------------------------------------- 解析
def test_relation_parsing(parser):
    p = parser.parse("北京市海淀区测试大厦往东五百米")
    assert p.poi == "测试大厦"
    assert p.relation["type"] == "offset" and p.relation["bearing"] == 90 and p.relation["distance_m"] == 500
    p = parser.parse("北京站斜对面")
    assert p.poi == "北京站" and p.relation["type"] == "opposite"
    p = parser.parse("望京SOHO对面的星巴克")
    assert p.poi == "望京SOHO" and p.near_target == "星巴克"
    p = parser.parse("海淀区测试路与交叉路交叉口")
    assert p.road == "测试路" and p.cross_road == "交叉路" and p.target_level == "intersection"


def test_admin_typo_correction(parser):
    p = parser.parse("北京市海定区测试路35号")
    assert p.adcodes()["district"] == "110108"
    assert p.corrections == [{"from": "海定区", "to": "海淀区", "adcode": "110108", "weak": False}]
    # 本身是合法名称、或没有上级约束且有歧义时不纠
    assert parser.parse("北京市朝阳区").corrections == []
    assert parser.parse("海定区中关村").corrections == []


def test_city_nicknames(parser):
    assert parser.parse("魔都东方明珠").adcodes()["province"] == "310000"
    assert parser.parse("羊城天河区").adcodes()["district"] == "440106"
    # 别称夹在中间、紧跟路名用字时不当行政区
    assert parser.parse("深圳鹏城实验室").adcodes()["city"] == "440300"


# ---------------------------------------------------------------- 检索
def test_traditional_input_geocodes(engine):
    r = best(engine, "北京市海澱區測試路35號")
    assert r["precision"] == "door" and r["confidence"] >= 0.9
    assert r["signals"]["traditional"] is True


def test_typo_confirmed_by_data_keeps_confidence(engine):
    r = best(engine, "北京市海定区测试路35号")
    assert r["precision"] == "door" and r["confidence"] >= 0.9
    assert r["signals"]["corrections"][0]["to"] == "海淀区"


def test_offset_relation_moves_point(engine):
    r = best(engine, "海淀区测试大厦往北100米")
    anchor = r["relation"]["anchor_location"]
    d = haversine(anchor["lng"], anchor["lat"], r["location"]["lng"], r["location"]["lat"])
    assert 95 < d < 105 and r["location"]["lat"] > anchor["lat"]
    assert r["relation"]["anchor"] == "测试大厦"


def test_near_relation_lowers_confidence_slightly(engine):
    exact = best(engine, "海淀区测试大厦")["confidence"]
    near = best(engine, "海淀区测试大厦附近")
    assert near["relation"]["type"] == "near" and near["relation"]["radius_m"] == 300
    assert exact * 0.9 < near["confidence"] < exact


def test_anchor_then_target(engine):
    r = best(engine, "海淀区测试大厦对面的星光便利店")
    assert r["matched"]["name"] == "星光便利店"
    assert r["relation"]["anchor"] == "测试大厦" and r["relation"]["distance_to_anchor_m"] < 200
    miss = best(engine, "海淀区测试大厦对面的不存在的小卖部")
    assert miss["matched"]["name"] == "测试大厦" and miss["confidence"] <= 0.55
    assert miss["signals"]["target_not_found"] == "不存在的小卖部"


def test_intersection(engine):
    r = best(engine, "海淀区测试路与交叉路交叉口")
    assert r["precision"] == "intersection" and r["confidence"] >= 0.9
    assert r["location"]["lat"] == pytest.approx(ROAD_LAT0 + 0.005, abs=1e-6)
    assert r["location"]["lng"] == pytest.approx(ROAD_LNG, abs=1e-6)
    assert r["formatted_address"].endswith("测试路与交叉路交叉口")


def test_sublocation_and_gate(engine):
    r = best(engine, "海淀区测试大厦B出口")
    assert r["matched"]["name"] == "测试大厦" and "sublocation" in r["signals"]["matched_by"]


def test_generated_abbreviation(engine):
    r = best(engine, "晨体", city="北京")
    assert r["matched"]["name"] == "北京晨光体育场" and r["signals"]["matched_by"] == "abbreviation"


def test_ambiguous_poi_without_context(engine):
    r = best(engine, "星光广场")
    assert r["signals"]["competitors"] == 1 and len(r["alternatives"]) == 1
    assert r["confidence"] < 0.6
    resolved = best(engine, "星光广场", city="长春")
    assert "alternatives" not in resolved and resolved["confidence"] >= 0.8


def test_distinctive_word_missing_is_not_confident(engine):
    # 数据里只有“测试大厦”，“虹桥大厦”只共享类别词，不能高置信度命中
    res = engine.geocoder.geocode("海淀区虹桥大厦", use_fallback=False)
    b = res["results"][0]
    assert b["confidence"] < 0.6


def test_memory_hit_for_colloquial_text(engine):
    r = best(engine, "北京市海淀区测试路那个红色大门的院子")
    assert r["source"] == "memory" and r["confidence"] >= 0.9
    assert r["location"]["lat"] == pytest.approx(ROAD_LAT0 + 0.0081, abs=1e-6)
    assert r["adcode"]["district"] == "110108"
    fb = best(engine, "海淀区测试大厦后面的停车场")  # 来自 /v1/feedback 日志格式
    assert fb["source"] == "memory" and fb["location"]["lat"] == pytest.approx(ROAD_LAT0 + 0.0025, abs=1e-6)


def test_signals_absent_for_clean_input(engine):
    r = best(engine, "北京市海淀区测试路35号")
    assert r["signals"] is None or "corrections" not in r["signals"]
    assert ROAD_LAT0 + 35 * STEP == pytest.approx(r["location"]["lat"], abs=1e-7)
