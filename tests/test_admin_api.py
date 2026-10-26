"""数据管理 HTTP 接口与管理界面。"""

import json

import pytest
from fastapi.testclient import TestClient

from cngeocoder import coords
from cngeocoder.api import create_app
from cngeocoder.engine import Settings
from cngeocoder.geo import haversine

from .conftest import ROAD_LAT0, ROAD_LNG

ADMIN_KEY = "cng_admin_api_key_000001"
H = {"Authorization": f"Bearer {ADMIN_KEY}"}


def _settings(eng, **kw):
    s = eng.settings
    return Settings(db_path=s.db_path, editable=s.editable, review_log=s.review_log, feedback_log=s.feedback_log,
                    api_keys=f"ops:admin:{ADMIN_KEY}", **kw)


@pytest.fixture
def client(editable_engine):
    editable_engine.settings = _settings(editable_engine)
    with TestClient(create_app(engine=editable_engine)) as c:
        yield c


def test_place_crud(client):
    glng, glat = coords.from_wgs84(ROAD_LNG + 0.0005, ROAD_LAT0 + 0.0045, "gcj02")
    r = client.post("/v1/admin/places", headers=H, json={"name": "管理界面测试楼", "address": "北京市海淀区测试路45号",
                                                          "location": f"{glng},{glat}", "coordsys": "gcj02"})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["coordsys"] == "gcj02" and abs(p["location"]["lng"] - glng) < 1e-6  # 按输入坐标系返回
    uid = p["uid"]
    w = client.get(f"/v1/admin/places/{uid}", headers=H).json()
    assert w["coordsys"] == "wgs84" and haversine(w["location"]["lng"], w["location"]["lat"],
                                                   ROAD_LNG + 0.0005, ROAD_LAT0 + 0.0045) < 1

    r = client.patch(f"/v1/admin/places/{uid}", headers=H, json={"name": "管理界面测试大楼", "alias": ["测试大楼"]})
    assert r.status_code == 200 and r.json()["alias"] == ["测试大楼"]
    assert client.patch(f"/v1/admin/places/{uid}", headers=H, json={}).status_code == 400
    assert client.patch(f"/v1/admin/places/{uid}", headers=H, json={"lng": 116.1}).status_code == 400
    assert client.patch("/v1/admin/places/nope", headers=H, json={"name": "x"}).status_code == 404

    lst = client.get("/v1/admin/places", headers=H, params={"q": "管理界面", "kind": "poi"}).json()
    assert lst["total"] == 1 and lst["items"][0]["uid"] == uid
    assert client.get("/v1/admin/places", headers=H, params={"adcode": "110108", "limit": 5}).json()["total"] > 50
    g = client.get("/v1/geocode", headers=H, params={"address": "海淀区管理界面测试大楼"}).json()
    assert g["results"][0]["matched"]["name"] == "管理界面测试大楼"

    assert client.delete(f"/v1/admin/places/{uid}", headers=H).json()["deleted"]["uid"] == uid
    assert client.get(f"/v1/admin/places/{uid}", headers=H).status_code == 404


def test_memory_review_changes(client, editable_engine):
    r = client.post("/v1/admin/memory", headers=H, json={"address": "海淀区测试路西头的修车铺",
                                                         "location": f"{ROAD_LNG - 0.001},{ROAD_LAT0 + 0.003}"})
    assert r.status_code == 201 and r.json()["items"]
    key = r.json()["items"][0]["key"]
    assert client.get("/v1/admin/memory", headers=H, params={"q": "修车铺"}).json()["total"] >= 1
    assert client.delete("/v1/admin/memory", headers=H, params={"key": key}).status_code == 200

    with open(editable_engine.settings.feedback_log, "w", encoding="utf-8") as f:
        f.write(json.dumps({"address": "海淀区测试路电线杆旁", "lng": ROAD_LNG, "lat": ROAD_LAT0 + 0.004},
                           ensure_ascii=False) + "\n")
    q = client.get("/v1/admin/review", headers=H).json()
    assert q["total"] == 1 and q["enabled"] == {"review": True, "feedback": True}
    item = q["items"][0]
    r = client.post(f"/v1/admin/review/{item['id']}", headers=H, json={"action": "approve"})
    assert r.json()["status"] == "approved"
    ov = client.get("/v1/admin/overview", headers=H).json()
    assert ov["review"]["pending"] == 0 and ov["editable"] is True and ov["whoami"]["name"] == "ops"

    ch = client.get("/v1/admin/changes", headers=H).json()
    assert ch["total"] == 4 and ch["items"][0]["op"] == "review.resolve"
    ex = client.get("/v1/admin/changes/export", headers=H)
    assert ex.headers["content-type"].startswith("application/x-ndjson")
    assert "attachment" in ex.headers["content-disposition"]
    assert [json.loads(x)["seq"] for x in ex.text.splitlines()] == [1, 2, 3, 4]


def test_readonly_returns_409(engine):
    old = engine.settings
    engine.settings = _settings(engine)
    try:
        with TestClient(create_app(engine=engine)) as c:
            r = c.post("/v1/admin/places", headers=H, json={"name": "x", "location": "116.3,39.95"})
            assert r.status_code == 409 and "GEOCODER_EDITABLE" in r.json()["detail"]
            assert c.get("/v1/admin/places", headers=H).status_code == 200
    finally:
        engine.settings = old


def test_admin_page(client):
    r = client.get("/admin")
    assert r.status_code == 200 and "地址数据管理" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert client.get("/v1/admin/regions", params={"q": "海淀"}).status_code == 401  # 页面公开，数据要密钥


def test_edits_export_cli(editable_engine, tmp_path):
    from cngeocoder.cli import main

    editable_engine.manager.create_place({"name": "命令行导出点", "lng": ROAD_LNG, "lat": ROAD_LAT0})
    out = tmp_path / "e.jsonl"
    assert main(["--db", editable_engine.settings.db_path, "edits", "export", "--out", str(out)]) == 0
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["op"] == "place.upsert" and lines[0]["data"]["name"] == "命令行导出点"
