"""在线编辑：要素增删改、确认结果、待审核队列、变更日志重放、多进程一致性。"""

import json

import pytest

from cngeocoder.build import BuildInputs, build
from cngeocoder.edit import EditError, NotFound, ReadOnly
from cngeocoder.engine import Engine, Settings
from cngeocoder.geo import haversine

from .conftest import ADMIN_DIR, ROAD_LAT0, ROAD_LNG, STEP


def _best(eng, address, **kw):
    r = eng.geocoder.geocode(address, use_fallback=False, **kw)
    return r["results"][0] if r["results"] else None


def test_create_update_delete_place(editable_engine):
    eng, m = editable_engine, editable_engine.manager
    lng, lat = ROAD_LNG + 0.0004, ROAD_LAT0 + 61 * STEP
    assert _best(eng, "北京市海淀区新建科创中心")["precision"] != "poi"  # 先查一次，结果进缓存

    p = m.create_place({"name": "新建科创中心", "address": "北京市海淀区测试路61号", "lng": lng, "lat": lat},
                       actor="tester")
    assert p["uid"].startswith("m-") and p["source"] == "manual"
    assert p["adcode"].startswith("110108") and p["road"] == "测试路" and p["road_no"] == "61号"
    r = _best(eng, "北京市海淀区新建科创中心")  # 写入后缓存已清空，立即生效
    assert r["precision"] == "poi" and r["matched"]["name"] == "新建科创中心"

    p2 = m.update_place(p["uid"], {"name": "科创大厦", "alias": "科创|KC大厦"}, actor="tester")
    assert p2["uid"] == p["uid"] and p2["id"] != p["id"]  # 修改换新 id，uid 不变
    assert p2["alias"] == ["科创", "KC大厦"] and p2["road_no"] == "61号"
    assert _best(eng, "北京市海淀区科创大厦")["matched"]["name"] == "科创大厦"
    old = _best(eng, "北京市海淀区新建科创中心")
    assert not (old["precision"] == "poi" and old["matched"]["name"] == "科创大厦" and old["confidence"] > 0.8)

    m.delete_place(p["uid"], actor="tester")
    with pytest.raises(NotFound):
        m.get_place(p["uid"])
    assert _best(eng, "北京市海淀区科创大厦")["precision"] != "poi"
    ops = [c["op"] for c in m.list_changes()["items"]]
    assert ops == ["place.delete", "place.upsert", "place.upsert"]


def test_moving_place_rederives_region(editable_engine):
    m = editable_engine.manager
    p = m.create_place({"name": "搬家测试点", "lng": ROAD_LNG, "lat": ROAD_LAT0 + 0.003})
    assert p["adcode"] == "110108"  # 无地址，靠边界点落面
    moved = m.update_place(p["uid"], {"lng": 116.47, "lat": 39.92})  # 朝阳区边界内
    assert moved["adcode"] == "110105" and moved["region"].endswith("朝阳区")
    pinned = m.update_place(p["uid"], {"adcode": "110108"})  # 显式编码优先
    assert pinned["adcode"] == "110108"


def test_place_validation(editable_engine):
    m = editable_engine.manager
    with pytest.raises(EditError, match="名称"):
        m.create_place({"lng": ROAD_LNG, "lat": ROAD_LAT0})
    with pytest.raises(EditError, match="坐标"):
        m.create_place({"name": "没坐标"})
    with pytest.raises(EditError, match="区划编码"):
        m.create_place({"name": "错编码", "adcode": "999999", "lng": ROAD_LNG, "lat": ROAD_LAT0})
    with pytest.raises(EditError, match="坐标系"):
        m.create_place({"name": "错坐标系", "lng": ROAD_LNG, "lat": ROAD_LAT0, "coordsys": "mars"})
    with pytest.raises(NotFound):
        m.update_place("nope", {"name": "x"})
    p = m.create_place({"uid": "my-1", "name": "自定义", "lng": ROAD_LNG, "lat": ROAD_LAT0})
    assert p["uid"] == "my-1"
    with pytest.raises(EditError, match="已存在"):
        m.create_place({"uid": "my-1", "name": "重复", "lng": ROAD_LNG, "lat": ROAD_LAT0})


def test_gcj_input_stored_as_wgs84(editable_engine):
    from cngeocoder import coords

    m = editable_engine.manager
    glng, glat = coords.from_wgs84(ROAD_LNG, ROAD_LAT0, "gcj02")
    p = m.create_place({"name": "火星输入点", "lng": glng, "lat": glat, "coordsys": "gcj02"})
    assert haversine(p["location"]["lng"], p["location"]["lat"], ROAD_LNG, ROAD_LAT0) < 1


def test_road_edit(editable_engine):
    eng, m = editable_engine, editable_engine.manager
    geom = {"type": "LineString", "coordinates": [[ROAD_LNG + 0.002, ROAD_LAT0], [ROAD_LNG + 0.002, ROAD_LAT0 + 0.006]]}
    r = m.create_place({"kind": "road", "name": "新修路", "geometry": geom})
    assert r["kind"] == "road" and r["adcode"] == "110108" and r["geometry"]["type"] == "LineString"
    assert "新修路" in eng.parser.road_lexicon
    res = _best(eng, "北京市海淀区新修路")
    assert res["precision"] == "road"
    near = eng.reverse.reverse(ROAD_LNG + 0.0021, ROAD_LAT0 + 0.003, radius=100)
    assert near["road"]["name"] == "新修路"
    r2 = m.update_place(r["uid"], {"name": "新修北路"})
    assert r2["geometry"] == r["geometry"]


def test_memory(editable_engine):
    eng, m = editable_engine, editable_engine.manager
    addr = "北京市海淀区测试路那个蓝色铁门"
    tlng, tlat = ROAD_LNG + 0.0007, ROAD_LAT0 + 0.0042
    rows = m.add_memory(addr, tlng, tlat, count=3, actor="tester")
    assert rows and all(r["count"] == 3 for r in rows)
    r = _best(eng, addr)
    assert r["source"] == "memory" and haversine(r["location"]["lng"], r["location"]["lat"], tlng, tlat) < 1
    assert m.list_memory("蓝色铁门")["total"] == len(rows)
    for row in rows:
        m.delete_memory(row["key"])
    assert _best(eng, addr)["source"] != "memory"
    with pytest.raises(NotFound):
        m.delete_memory(rows[0]["key"])
    with pytest.raises(EditError):
        m.add_memory(addr, tlng, tlat, spread=500)


def test_review_queue(editable_engine):
    eng, m = editable_engine, editable_engine.manager
    with open(eng.settings.review_log, "w", encoding="utf-8") as f:
        f.write(json.dumps({"address": "海淀区测试路尽头的小卖部", "lng": ROAD_LNG + 0.0001, "lat": ROAD_LAT0 + 0.01,
                            "precision": "poi", "source": "tianditu", "adcode": "110108"}, ensure_ascii=False) + "\n")
    with open(eng.settings.feedback_log, "w", encoding="utf-8") as f:
        f.write(json.dumps({"address": "海淀区测试路老槐树下", "lng": ROAD_LNG, "lat": ROAD_LAT0 + 0.002,
                            "coordsys": "wgs84", "ts": 1}, ensure_ascii=False) + "\n")
        f.write("not json\n")
    q = m.review_items()
    assert q["total"] == 2 and {i["source"] for i in q["items"]} == {"review", "feedback"}
    fb = next(i for i in q["items"] if i["source"] == "feedback")
    rv = next(i for i in q["items"] if i["source"] == "review")

    out = m.resolve_review(fb["id"], "approve", ROAD_LNG + 0.0002, ROAD_LAT0 + 0.002, actor="reviewer")
    assert out["status"] == "approved" and out["memory"]
    r = _best(eng, "海淀区测试路老槐树下")
    assert r["source"] == "memory" and r["location"]["lng"] == pytest.approx(ROAD_LNG + 0.0002, abs=1e-6)
    m.resolve_review(rv["id"], "dismiss", actor="reviewer")
    assert m.review_items()["total"] == 0
    done = m.review_items(status="all")["items"]
    assert {i["status"] for i in done} == {"approved", "dismissed"} and done[0]["resolved_by"] == "reviewer"
    with pytest.raises(NotFound):
        m.resolve_review("missing", "approve")


def test_readonly_engine_rejects_writes(engine):
    with pytest.raises(ReadOnly):
        engine.manager.create_place({"name": "x", "lng": ROAD_LNG, "lat": ROAD_LAT0})
    assert engine.manager.list_places(kind="address")["total"] == 50  # 读接口照常可用


def test_other_worker_sees_edits(editable_engine):
    """模拟多 worker：另一个进程内引擎打开同一个文件，版本变化后清空缓存。"""
    other = Engine(Settings(db_path=editable_engine.settings.db_path, editable=True))
    assert _best(other, "北京市海淀区远程同步中心")["precision"] != "poi"
    editable_engine.manager.create_place({"name": "远程同步中心", "adcode": "110108",
                                          "lng": ROAD_LNG, "lat": ROAD_LAT0 + 0.004})
    other._checked = 0  # 跳过 1 秒的检查间隔
    other.sync()
    assert _best(other, "北京市海淀区远程同步中心")["precision"] == "poi"


def test_export_and_replay(editable_engine, fixture_dir, tmp_path):
    m = editable_engine.manager
    base = m.list_places(kind="address", q="测试路35号")["items"][0]
    m.update_place(base["uid"], {"name": "三十五号院"})
    gone = m.list_places(q="晨曦中心")["items"][0]
    m.delete_place(gone["uid"])
    new = m.create_place({"name": "重放验证点", "adcode": "110108", "lng": ROAD_LNG, "lat": ROAD_LAT0 + 0.006})
    m.add_memory("北京市海淀区重放写法", ROAD_LNG, ROAD_LAT0 + 0.007)
    edits = tmp_path / "edits.jsonl"
    edits.write_text("".join(m.export_changes()), encoding="utf-8")
    assert len(edits.read_text(encoding="utf-8").splitlines()) == 4

    def rebuild(name, files):
        out = tmp_path / name
        stats = build(BuildInputs(admin_dir=ADMIN_DIR, out=str(out),
                                  places=(str(fixture_dir / "places.csv"),), roads=(str(fixture_dir / "roads.geojson"),),
                                  boundaries=(str(fixture_dir / "bounds.geojson"),), edits=files))
        return Engine(Settings(db_path=str(out))), stats

    # 同一份日志重放两次，结果相同（操作按 uid / key 幂等）
    eng2, stats = rebuild("r.db", (str(edits), str(edits)))
    assert stats["edits"] == 7  # 第二遍的删除目标已不存在，跳过
    m2 = eng2.manager
    assert m2.get_place(base["uid"])["name"] == "三十五号院"  # 基础数据 uid 跨建库稳定
    with pytest.raises(NotFound):
        m2.get_place(gone["uid"])
    assert m2.get_place(new["uid"])["name"] == "重放验证点"
    assert _best(eng2, "北京市海淀区重放写法")["source"] == "memory"
    assert m2.list_changes()["total"] == 7  # 日志随数据带到新库，下次导出仍是完整历史
    assert int(eng2.store.meta()["place_seq"]) > max(r["id"] for r in m2.list_places(limit=200)["items"])


def test_uid_stable_across_builds(base_db, fixture_dir, tmp_path):
    other = tmp_path / "again.db"
    build(BuildInputs(admin_dir=ADMIN_DIR, out=str(other), places=(str(fixture_dir / "places.csv"),),
                      roads=(str(fixture_dir / "roads.geojson"),), boundaries=(str(fixture_dir / "bounds.geojson"),),
                      centroids=(str(fixture_dir / "centroids.csv"),),
                      memory=(str(fixture_dir / "memory.csv"), str(fixture_dir / "feedback.jsonl"))))
    a = Engine(Settings(db_path=base_db)).manager.list_places(limit=200)["items"]
    b = Engine(Settings(db_path=str(other))).manager.list_places(limit=200)["items"]
    assert sorted(p["uid"] for p in a) == sorted(p["uid"] for p in b)
    assert len({p["uid"] for p in a}) == len(a)


def test_search_regions(engine):
    hits = engine.manager.search_regions("海淀")
    assert hits[0]["adcode"] == "110108" and hits[0]["full_name"] == "北京市 海淀区"
    assert engine.manager.search_regions("110108")[0]["name"] == "海淀区"


def test_readonly_reopen_sees_edits(editable_engine):
    """编辑后切回只读（immutable）部署，修改都已落在主文件里。"""
    p = editable_engine.manager.create_place({"name": "切回只读验证点", "adcode": "110108",
                                              "lng": ROAD_LNG, "lat": ROAD_LAT0 + 0.002})
    ro = Engine(Settings(db_path=editable_engine.settings.db_path))
    assert ro.store.immutable and ro.manager.get_place(p["uid"])["name"] == "切回只读验证点"
    assert _best(ro, "北京市海淀区切回只读验证点")["precision"] == "poi"


def test_region_coordinate_warning(editable_engine):
    m = editable_engine.manager
    ok = m.create_place({"name": "一致点", "adcode": "110108", "lng": ROAD_LNG, "lat": ROAD_LAT0})
    assert "warnings" not in ok
    pip = m.create_place({"name": "边界矛盾点", "adcode": "110105", "lng": ROAD_LNG, "lat": ROAD_LAT0})
    assert "海淀区" in pip["warnings"][0]  # 有边界：按点落面判断
    far = m.create_place({"name": "同名选错点", "adcode": "2113", "lng": 116.48, "lat": 39.99})
    assert "公里" in far["warnings"][0]  # 选成了辽宁朝阳市


def test_duplicate_rows_get_suffixed_uids(tmp_path):
    rows = "name,lng,lat,adcode\n重复点,116.3,39.95,110108\n重复点,116.3,39.95,110108\n另一点,116.31,39.95,110108\n"
    (tmp_path / "dup.csv").write_text(rows, encoding="utf-8")
    out = tmp_path / "dup.db"
    build(BuildInputs(admin_dir=ADMIN_DIR, out=str(out), places=(str(tmp_path / "dup.csv"),)))
    uids = sorted(p["uid"] for p in Engine(Settings(db_path=str(out))).manager.list_places()["items"])
    dup = [u for u in uids if u.endswith("~2")]
    assert len(uids) == 3 and len(dup) == 1 and dup[0][:-2] in uids
