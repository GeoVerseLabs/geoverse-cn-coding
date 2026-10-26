"""鉴权覆盖面：每个接口都声明了角色、鉴权先于参数校验、批量限流、兜底配额、元数据脱敏、密钥热加载、审计日志、数据范围。"""

import json
import logging

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from cngeocoder import auth
from cngeocoder.api import PUBLIC_ROUTES, create_app
from cngeocoder.auth import hash_key
from cngeocoder.engine import Settings

from .conftest import ROAD_LAT0, ROAD_LNG

READ = "cng_cov_read_key_000001"
ADMIN = "cng_cov_admin_key_00001"
HAIDIAN = "cng_cov_haidian_admin_01"   # 只授权海淀区（110108）的区域管理员
NOFB = "cng_cov_no_fallback_0001"


def H(key):
    return {"Authorization": f"Bearer {key}"}


def _keys_file(tmp_path, extra=()):
    f = tmp_path / "keys.json"
    f.write_text(json.dumps([
        {"name": "app", "role": "read", "key_sha256": hash_key(READ)},
        {"name": "ops", "role": "admin", "key": ADMIN},
        {"name": "haidian", "role": "admin", "key_sha256": hash_key(HAIDIAN), "regions": ["110108"]},
        {"name": "nofb", "role": "read", "key": NOFB, "fallback": False, "rpm": 5},
        *extra,
    ]), encoding="utf-8")
    return f


@pytest.fixture
def client(editable_engine, tmp_path):
    s = editable_engine.settings
    editable_engine.settings = Settings(db_path=s.db_path, editable=True, review_log=s.review_log,
                                        feedback_log=s.feedback_log, api_keys_file=str(_keys_file(tmp_path)))
    with TestClient(create_app(engine=editable_engine)) as c:
        yield c


def test_every_route_declares_a_role(client):
    """新增接口忘了声明角色时这里会失败：要么用 need() 声明，要么显式加进 PUBLIC_ROUTES。"""
    missing = []
    for rt in client.app.routes:
        if not isinstance(rt, APIRoute):
            continue  # /docs、/openapi.json 由 GEOCODER_PUBLIC_DOCS 控制
        for m in rt.methods - {"HEAD"}:
            if rt.auth is None and (rt.path, m) not in PUBLIC_ROUTES:
                missing.append((m, rt.path))
    assert not missing
    spec = client.get("/openapi.json").json()["paths"]
    assert spec["/v1/geocode"]["get"]["x-required-role"] == "read"
    assert spec["/v1/admin/changes/export"]["get"]["x-global-admin-only"] is True


def test_auth_runs_before_validation(client):
    # 未授权时先 401，不返回 422 这类泄露接口细节的校验信息，也不解析请求体
    assert client.post("/v1/feedback", content=b"{bad", headers={"content-type": "application/json"}).status_code == 401
    assert client.get("/v1/geocode").status_code == 401
    assert client.post("/v1/admin/places", json={"kind": "bogus"}, headers=H(READ)).status_code == 403
    assert client.get("/v1/geocode", headers=H(READ)).status_code == 422  # 授权后才校验参数


def test_batch_counts_items(client):
    body = {"items": [{"address": "北京市海淀区测试路1号"}] * 4}
    assert client.post("/v1/geocode/batch", json=body, headers=H(NOFB)).status_code == 200  # 计 4 次
    assert client.post("/v1/geocode/batch", json=body, headers=H(NOFB)).status_code == 429  # 4 + 4 > 5，不计数
    assert client.get("/v1/geocode", params={"address": "x"}, headers=H(NOFB)).status_code == 200  # 第 5 次
    assert client.get("/v1/geocode", params={"address": "x"}, headers=H(NOFB)).status_code == 429


class _CountingFallback:
    def __init__(self):
        self.calls = []

    def geocode(self, address):
        self.calls.append(address)
        return None

    def stats(self):
        return {}


def test_fallback_permission(client, editable_engine):
    fb = _CountingFallback()
    editable_engine.geocoder.fallback = fb
    q = {"address": "北京市海淀区完全不存在的某某某商店"}
    r = client.get("/v1/geocode", params=q, headers=H(NOFB)).json()
    assert r["fallback_permitted"] is False and fb.calls == []
    client.post("/v1/geocode/batch", json={"items": [q]}, headers=H(NOFB))
    assert fb.calls == []
    r = client.get("/v1/geocode", params=q, headers=H(READ)).json()
    assert "fallback_permitted" not in r and len(fb.calls) == 1


def test_meta_hides_server_path(client):
    assert "db" not in client.get("/v1/meta", headers=H(READ)).json()
    assert "db" not in client.get("/v1/meta", headers=H(HAIDIAN)).json()
    assert "db" in client.get("/v1/meta", headers=H(ADMIN)).json()


def test_keys_file_hot_reload(client, tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "RELOAD_INTERVAL", 0)
    assert client.get("/v1/meta", headers=H(READ)).status_code == 200
    import os
    import time

    f = tmp_path / "keys.json"
    keys = [k for k in json.loads(f.read_text(encoding="utf-8")) if k["name"] != "app"]  # 吊销 app
    f.write_text(json.dumps(keys), encoding="utf-8")
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert client.get("/v1/meta", headers=H(READ)).status_code == 401
    f.write_text("[{broken", encoding="utf-8")  # 写坏了：保留上一次的配置，服务不受影响
    os.utime(f, (time.time() + 10, time.time() + 10))
    assert client.get("/v1/meta", headers=H(ADMIN)).status_code == 200


def test_denials_are_logged(client, caplog):
    with caplog.at_level(logging.WARNING, logger="cngeocoder.auth"):
        client.get("/v1/meta", headers=H("cng_wrong_key_0000000000"))
        client.get("/v1/admin/places", headers=H(READ))
    text = caplog.text
    assert "invalid key" in text and "app (role read) needs admin" in text


# ---------------------------------------------------------------------- 数据范围
def test_scoped_admin(client, editable_engine):
    h = H(HAIDIAN)
    who = client.get("/v1/auth/whoami", headers=h).json()
    assert who["regions"] == ["110108"]

    # 只看得到海淀区的数据
    items = client.get("/v1/admin/places", headers=h, params={"limit": 200}).json()["items"]
    assert items and all(p["adcode"].startswith("110108") for p in items)
    other = client.get("/v1/admin/places", headers=H(ADMIN), params={"q": "晨曦中心"}).json()["items"][0]
    assert client.get(f"/v1/admin/places/{other['uid']}", headers=h).status_code == 404
    assert client.delete(f"/v1/admin/places/{other['uid']}", headers=h).status_code == 404

    # 范围内可以新增、修改；搬到范围外被拒绝且不落库
    ok = client.post("/v1/admin/places", headers=h, json={"name": "海淀区域点", "adcode": "110108",
                                                          "location": f"{ROAD_LNG},{ROAD_LAT0}"})
    assert ok.status_code == 201
    uid = ok.json()["uid"]
    r = client.patch(f"/v1/admin/places/{uid}", headers=h, json={"adcode": "110105"})
    assert r.status_code == 403 and "数据范围" in r.json()["detail"]
    assert client.get(f"/v1/admin/places/{uid}", headers=h).json()["adcode"] == "110108"  # 已回滚
    r = client.post("/v1/admin/places", headers=h, json={"name": "朝阳点", "adcode": "110105",
                                                         "location": "116.47,39.92"})
    assert r.status_code == 403

    # 确认结果
    assert client.post("/v1/admin/memory", headers=h, json={"address": "北京市朝阳区某小区南门",
                                                            "location": "116.47,39.92"}).status_code == 403
    assert client.post("/v1/admin/memory", headers=h, json={"address": "北京市海淀区测试路某小区南门",
                                                            "location": f"{ROAD_LNG},{ROAD_LAT0}"}).status_code == 201
    mem = client.get("/v1/admin/memory", headers=h, params={"limit": 200}).json()["items"]
    assert mem and all((m["adcode"] or "").startswith("110108") for m in mem)

    # 待审核：只看到范围内的条目
    with open(editable_engine.settings.feedback_log, "w", encoding="utf-8") as f:
        for a, x, y in (("北京市海淀区测试路某号院", ROAD_LNG, ROAD_LAT0), ("北京市朝阳区某号院", 116.47, 39.92)):
            f.write(json.dumps({"address": a, "lng": x, "lat": y}, ensure_ascii=False) + "\n")
    q = client.get("/v1/admin/review", headers=h).json()
    assert [i["address"] for i in q["items"]] == ["北京市海淀区测试路某号院"]
    assert client.get("/v1/admin/review", headers=H(ADMIN)).json()["total"] == 2

    # 全局能力不对区域管理员开放；变更记录只看自己的
    client.post("/v1/admin/places", headers=H(ADMIN), json={"name": "总部点", "location": "116.47,39.92"})
    assert client.get("/v1/admin/changes/export", headers=h).status_code == 403
    assert client.get("/v1/admin/auth", headers=h).status_code == 403
    assert {c["actor"] for c in client.get("/v1/admin/changes", headers=h).json()["items"]} == {"haidian"}
    ov = client.get("/v1/admin/overview", headers=h).json()
    assert ov["auth"] is None and "db" not in ov


def test_scoped_admin_via_mcp(client):
    def call(name, args):
        r = client.post("/mcp", headers=H(HAIDIAN), json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                          "params": {"name": name, "arguments": args}})
        return r.json()["result"]

    bad = call("add_place", {"name": "朝阳点", "adcode": "110105", "lng": 116.47, "lat": 39.92})
    assert bad["isError"] and "数据范围" in bad["content"][0]["text"]
    good = call("add_place", {"name": "海淀点", "adcode": "110108", "lng": ROAD_LNG, "lat": ROAD_LAT0})
    assert not good["isError"]
    info = call("get_service_info", {})["structuredContent"]
    assert info["caller"]["regions"] == ["110108"]


def test_invalid_regions_rejected(tmp_path):
    from cngeocoder.auth import load_keys

    f = _keys_file(tmp_path, [{"name": "x", "role": "admin", "key": ADMIN + "x", "regions": ["北京"]}])
    with pytest.raises(ValueError, match="不是区划编码"):
        load_keys(None, str(f))
    f = _keys_file(tmp_path, [{"name": "y", "role": "admin", "key": ADMIN + "y", "scope": "11"}])
    with pytest.raises(ValueError, match="未知字段"):
        load_keys(None, str(f))
