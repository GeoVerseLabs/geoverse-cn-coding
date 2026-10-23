import pytest


def comps(parser, text, **kw):
    p = parser.parse(text, **kw)
    return {k: v for k, v in p.components().items() if v}, p


@pytest.mark.parametrize("text,expect", [
    ("北京市朝阳区阜通东大街6号望京SOHO T3 1201室",
     dict(province="北京市", city="北京市", district="朝阳区", road="阜通东大街", road_no="6号", poi="望京SOHO",
          building="T3", room="1201室")),
    ("广东深圳南山科技园腾讯滨海大厦", dict(province="广东省", city="深圳市", district="南山区")),
    ("海淀区中关村大街27号中关村大厦", dict(district="海淀区", road="中关村大街", road_no="27号", poi="中关村大厦")),
    ("上海市徐汇区漕溪北路100弄5号301室", dict(district="徐汇区", road="漕溪北路", road_no="100弄", building="5号", room="301室")),
    ("朝阳区建国路93号院万达广场8号楼2单元1201",
     dict(district="朝阳区", road="建国路", road_no="93号院", poi="万达广场", building="8号楼", unit="2单元", room="1201")),
    ("江苏省苏州市姑苏区人民路三段十八号", dict(district="姑苏区", road="人民路3段", road_no="18号")),
    ("黔东南凯里", dict(city="黔东南苗族侗族自治州", district="凯里市")),
    ("北京市朝阳区朝阳北路101号", dict(district="朝阳区", road="朝阳北路", road_no="101号")),
    ("广东省东莞市东城街道东纵大道", dict(city="东莞市", town="东城街道", road="东纵大道")),
    ("河南省济源市沁园街道", dict(province="河南省", district="济源市", town="沁园街道")),
    ("北京市海淀区中关村软件园二期西北旺东路10号院",
     dict(road="西北旺东路", road_no="10号院", poi="中关村软件园2期")),
    ("河北省廊坊市大厂回族自治县夏垫镇东小庄村3组15号",
     dict(district="大厂回族自治县", town="夏垫镇", community="东小庄村3组", road_no="15号")),
    ("山东省青岛市黄岛区长江中路与井冈山路交叉口", dict(district="黄岛区", road="长江中路")),
    ("北京西城区金融街购物中心", dict(province="北京市", district="西城区")),
    ("小区3-2-501", dict(poi="小区", building="3号楼", unit="2单元", room="501室")),
    ("北京市东城区景山前街4号故宫博物院东门", dict(road="景山前街", road_no="4号", poi="故宫博物院")),
])
def test_parse_components(parser, text, expect):
    got, _ = comps(parser, text)
    for k, v in expect.items():
        assert got.get(k) == v, (text, k, got)


@pytest.mark.parametrize("text,code", [
    ("崇文区天坛路", "110101"),          # 旧区名
    ("襄樊市樊城区", "420606"),          # 旧市名
    ("密云县鼓楼街道", "110118"),        # 县改区，自动生成别名
    ("吉林省长春市朝阳区红旗街", "220104"),
    ("湖北省恩施州恩施市", "422801"),
    ("内蒙呼和浩特新城区", "150102"),
])
def test_admin_aliases_and_hierarchy(parser, text, code):
    _, p = comps(parser, text)
    assert p.adcodes()["district"] == code


def test_ambiguous_district_returns_alternatives(parser):
    _, p = comps(parser, "朝阳区建国路93号")
    assert p.adcodes()["district"] == "110105"  # 直辖市先验
    alts = {a.adcodes()["district"] or a.adcodes()["city"] for a in p.alternatives}
    assert "220104" in alts


def test_city_hint(parser):
    _, p = comps(parser, "朝阳区红旗街", city="长春")
    assert p.adcodes()["district"] == "220104"
    _, p = comps(parser, "望京SOHO", city="110105")
    assert p.adcodes()["district"] == "110105" and p.poi == "望京SOHO"


def test_road_name_not_taken_as_admin(parser):
    got, p = comps(parser, "江苏省南京市中山东路18号")
    assert got["city"] == "南京市" and got["road"] == "中山东路"
    assert p.district is None


def test_municipality_city_fields(parser):
    _, p = comps(parser, "上海浦东新区世纪大道100号")
    assert p.adcodes() == {"province": "310000", "city": "310100", "district": "310115", "town": None}
    assert p.formatted() == "上海市浦东新区世纪大道100号"


def test_noise_prefix_and_relative_tail(parser):
    got, p = comps(parser, "收件人：王五 13911112222 北京市海淀区颐和园路5号北京大学西门对面")
    assert got["road"] == "颐和园路" and got["poi"] == "北京大学"
    assert "对面" in p.extra and "西门" in p.extra


def test_formatted_separates_alnum_parts(parser):
    p = parser.parse("北京朝阳望京SOHO T3 1201室")
    assert p.formatted() == "北京市朝阳区望京SOHO-T3-1201室"
