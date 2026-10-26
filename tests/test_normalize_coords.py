import math

import pytest

from cngeocoder import coords
from cngeocoder.geo import bearing, direction_cn, haversine
from cngeocoder.normalize import cn_to_int, normalize, normalize_numbers
from cngeocoder.tokenizer import match_expr, name_similarity, ngrams


@pytest.mark.parametrize("s,n", [("十八", 18), ("一百零一", 101), ("一零一", 101), ("二十", 20), ("两", 2), ("三千五百", 3500)])
def test_cn_to_int(s, n):
    assert cn_to_int(s) == n


def test_normalize_fullwidth_phone_and_spaces():
    s = normalize("收货人：张三 13800138000 北京市 朝阳区（望京ＳＯＨＯ） T1 1201")
    assert "13800138000" not in s
    assert "收货人" not in s
    assert "望京SOHO" in s
    assert s.endswith("T1-1201")


def test_normalize_hash_means_number():
    assert normalize("中山路18#") == "中山路18号"


def test_numbers_only_before_quantifiers():
    assert normalize_numbers("三号楼二单元五零一室") == "3号楼2单元501室"
    assert normalize_numbers("人民路三段十八号") == "人民路3段18号"
    # 地名里的数字不动
    assert normalize_numbers("三里屯路") == "三里屯路"
    assert normalize_numbers("五道口") == "五道口"


def test_coord_roundtrip():
    lng, lat = 116.397477, 39.908692
    w = coords.gcj02_to_wgs84(lng, lat)
    g = coords.wgs84_to_gcj02(*w)
    assert abs(g[0] - lng) < 1e-7 and abs(g[1] - lat) < 1e-7
    # 偏移量在几百米量级
    assert 300 < haversine(lng, lat, *w) < 800
    b = coords.convert(lng, lat, "gcj02", "bd09")
    back = coords.convert(*b, "bd09", "gcj02")
    assert haversine(lng, lat, *back) < 1.0
    assert coords.convert(*coords.convert(lng, lat, "wgs84", "bd09"), "bd09", "wgs84") == pytest.approx((lng, lat), abs=1e-5)


def test_out_of_china_untouched():
    assert coords.wgs84_to_gcj02(2.35, 48.85) == (2.35, 48.85)


def test_coordsys_aliases():
    assert coords.CoordSys.parse("GCJ-02") is coords.CoordSys.GCJ02
    assert coords.CoordSys.parse("baidu") is coords.CoordSys.BD09
    assert coords.CoordSys.parse("cgcs2000").base is coords.CoordSys.WGS84
    with pytest.raises(ValueError):
        coords.CoordSys.parse("xyz")


def test_array_conversion_matches_scalar():
    import numpy as np

    pts = np.array([[116.397477, 39.908692], [121.4998, 31.2397], [2.35, 48.85]])
    out = coords.gcj02_to_wgs84_array(pts)
    for (x, y), (ox, oy) in zip(pts, out, strict=True):
        sx, sy = coords.gcj02_to_wgs84(x, y)
        assert math.isclose(sx, ox, abs_tol=1e-7) and math.isclose(sy, oy, abs_tol=1e-7)


def test_geo_helpers():
    assert direction_cn(bearing(116.0, 39.0, 116.01, 38.99)) == "东南"
    assert 1100 < haversine(116.0, 39.0, 116.0, 39.01) < 1120


def test_tokenizer():
    assert ngrams("望京SOHO") == ["望京", "soho"]
    assert ngrams("故") == ["故"]
    assert match_expr("国贸大厦") == '"国贸" OR "贸大" OR "大厦"'
    assert name_similarity("国贸大厦", "国贸大厦") == 1.0
    assert name_similarity("望京SOHO", "望京SOHO T1") > 0.8
    assert name_similarity("北京站", "上海站") < 0.5


def test_cache_drops_results_computed_before_clear():
    from cngeocoder.cache import LRUCache

    c = LRUCache(10)
    gen = c.generation  # 请求开始时的代号
    c.clear()  # 计算过程中数据被修改
    c.put("k", "stale", gen)
    assert c.get("k") is None
    c.put("k", "fresh", c.generation)
    assert c.get("k") == "fresh"
