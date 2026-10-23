import pytest
from fastapi.testclient import TestClient

from cngeocoder.api import create_app
from cngeocoder.geo import haversine

from .conftest import ROAD_LAT0, ROAD_LNG, STEP


@pytest.fixture(scope="module")
def client(engine):
    with TestClient(create_app(engine=engine)) as c:
        yield c


def test_health_and_meta(client):
    assert client.get("/health").json() == {"status": "ok"}
    m = client.get("/v1/meta").json()
    assert m["counts"]["address"] == 50 and m["boundary_levels"] == [3]


def test_parse(client):
    d = client.get("/v1/parse", params={"address": "广东深圳南山科技园"}).json()
    assert d["adcode"]["district"] == "440305"


def test_geocode_coordsys(client):
    w = client.get("/v1/geocode", params={"address": "北京市海淀区测试路35号"}).json()
    g = client.get("/v1/geocode", params={"address": "北京市海淀区测试路35号", "coordsys": "gcj02"}).json()
    lw, lg = w["results"][0]["location"], g["results"][0]["location"]
    assert g["coordsys"] == "gcj02"
    assert 300 < haversine(lw["lng"], lw["lat"], lg["lng"], lg["lat"]) < 800


def test_geocode_batch(client):
    body = {"items": [{"id": 1, "address": "北京市海淀区测试路35号"}, {"id": "b", "address": "晨曦中心"}]}
    d = client.post("/v1/geocode/batch", json=body).json()
    assert d["count"] == 2 and d["items"][1]["id"] == "b"


def test_reverse_roundtrip_in_bd09(client):
    from cngeocoder import coords

    lng, lat = coords.from_wgs84(ROAD_LNG, ROAD_LAT0 + 21 * STEP, "bd09")
    d = client.get("/v1/reverse", params={"location": f"{lng},{lat}", "coordsys": "bd09"}).json()
    assert d["adcode"]["district"] == "110108" and d["coordsys"] == "bd09"
    assert d["location"]["lng"] == pytest.approx(lng, abs=1e-6)


def test_reverse_batch(client):
    d = client.post("/v1/reverse/batch", json={"items": [{"id": 1, "location": f"{ROAD_LNG},{ROAD_LAT0}"}]}).json()
    assert d["items"][0]["adcode"]["district"] == "110108"


def test_convert(client):
    d = client.get("/v1/convert", params={"location": "116.397477,39.908692", "from": "gcj02", "to": "wgs84"}).json()
    assert d["location"]["lng"] == pytest.approx(116.391236, abs=1e-5)


def test_bad_input(client):
    assert client.get("/v1/reverse", params={"location": "abc"}).status_code == 400
    assert client.get("/v1/geocode", params={"address": "x", "coordsys": "mars"}).status_code == 400


def test_feedback_endpoint(engine, tmp_path):
    from cngeocoder.engine import Settings

    log = tmp_path / "feedback.jsonl"
    old = engine.settings
    engine.settings = Settings(db_path=old.db_path, feedback_log=str(log))
    try:
        with TestClient(create_app(engine=engine)) as c:
            r = c.post("/v1/feedback", json={"address": "测试路红门院子", "location": "116.300,39.958",
                                             "coordsys": "gcj02", "source": "签收"})
            assert r.status_code == 202
        line = log.read_text(encoding="utf-8").strip()
        assert "测试路红门院子" in line and '"coordsys": "wgs84"' in line
    finally:
        engine.settings = old
    with TestClient(create_app(engine=engine)) as c:
        assert c.post("/v1/feedback", json={"address": "x", "location": "116,39"}).status_code == 404
