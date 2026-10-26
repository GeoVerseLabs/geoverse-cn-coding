"""鉴权：密钥配置解析、角色、匿名访问、限流、错误密钥节流。"""

import json

import pytest
from fastapi.testclient import TestClient

from cngeocoder import auth
from cngeocoder.api import create_app
from cngeocoder.auth import KeyRing, hash_key, load_keys
from cngeocoder.engine import Settings

READ_KEY = "cng_read_key_for_tests_0001"
WRITE_KEY = "cng_write_key_for_tests_0001"
ADMIN_KEY = "cng_admin_key_for_tests_0001"
KEYS = f"app:read:{READ_KEY},svc:write:sha256:{hash_key(WRITE_KEY)},ops:admin:{ADMIN_KEY}"
GEO = {"address": "北京市海淀区测试路35号"}


def _client(engine, **kw):
    engine.settings = Settings(db_path=engine.settings.db_path, feedback_log=kw.pop("feedback_log", None), **kw)
    return TestClient(create_app(engine=engine))


@pytest.fixture
def secured(engine, tmp_path):
    old = engine.settings
    with _client(engine, api_keys=KEYS, feedback_log=str(tmp_path / "fb.jsonl")) as c:
        yield c
    engine.settings = old


def h(key):
    return {"Authorization": f"Bearer {key}"}


def test_load_keys_formats(tmp_path):
    keys = load_keys(KEYS, None)
    assert [(k.name, k.role) for k in keys] == [("app", "read"), ("svc", "write"), ("ops", "admin")]
    assert keys[1].key_sha256 == hash_key(WRITE_KEY)

    j = tmp_path / "keys.json"
    j.write_text(json.dumps([{"name": "a", "role": "read", "key_sha256": hash_key(READ_KEY), "rpm": 5},
                             {"name": "b", "role": "admin", "key": ADMIN_KEY}]), encoding="utf-8")
    ks = load_keys(None, str(j))
    assert ks[0].rpm == 5 and ks[1].key_sha256 == hash_key(ADMIN_KEY)

    t = tmp_path / "keys.txt"
    t.write_text(f"# 注释\nx:write:{WRITE_KEY}\n\n", encoding="utf-8")
    assert load_keys(None, str(t))[0].name == "x"

    for bad, msg in [("a:root:" + READ_KEY, "未知角色"), ("a:read:short", "太短"), ("a:read", "名称:角色:密钥"),
                     ("a:read:sha256:abc", "64 位"), (f"a:read:{READ_KEY},a:admin:{ADMIN_KEY}", "重复")]:
        with pytest.raises(ValueError, match=msg):
            load_keys(bad, None)


def test_defaults_without_keys():
    ring = KeyRing.from_settings(Settings())
    assert not ring.enabled and ring.anonymous_role == "write"
    ring = KeyRing.from_settings(Settings(api_keys=f"a:read:{READ_KEY}"))
    assert ring.enabled and ring.anonymous_role == "none"
    with pytest.raises(ValueError):
        KeyRing.from_settings(Settings(anonymous_role="admin"))


def test_roles(secured):
    c = secured
    assert c.get("/health").status_code == 200
    r = c.get("/v1/geocode", params=GEO)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert c.get("/v1/geocode", params=GEO, headers=h(READ_KEY)).status_code == 200
    assert c.get("/v1/geocode", params=GEO, headers={"X-API-Key": READ_KEY}).status_code == 200
    assert c.get("/v1/geocode", params={**GEO, "key": READ_KEY}).status_code == 401  # 默认不接受 ?key=
    fb = {"address": "测试路红门院子", "location": "116.300,39.958"}
    assert c.post("/v1/feedback", json=fb, headers=h(READ_KEY)).status_code == 403
    assert c.post("/v1/feedback", json=fb, headers=h(WRITE_KEY)).status_code == 202
    assert c.get("/v1/admin/places", headers=h(WRITE_KEY)).status_code == 403
    assert c.get("/v1/admin/places", headers=h(ADMIN_KEY)).status_code == 200
    me = c.get("/v1/auth/whoami", headers=h(ADMIN_KEY)).json()
    assert me == {"name": "ops", "role": "admin", "anonymous": False, "auth_enabled": True}
    assert c.get("/v1/auth/whoami").json()["role"] == "none"
    usage = c.get("/v1/admin/auth", headers=h(ADMIN_KEY)).json()
    assert {k["name"]: k["requests"] for k in usage["keys"]}["app"] >= 2
    assert all("key" not in k and "key_sha256" not in k for k in usage["keys"])


def test_invalid_key_and_bruteforce(secured, monkeypatch):
    monkeypatch.setattr(auth, "FAILED_PER_MINUTE", 3)
    codes = [secured.get("/v1/geocode", params=GEO, headers=h("cng_wrong_key_000000000")).status_code
             for _ in range(5)]
    assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]


def test_rate_limit(engine):
    old = engine.settings
    try:
        with _client(engine, api_keys=f"app:read:{READ_KEY}", rate_limit=2) as c:
            codes = [c.get("/v1/geocode", params=GEO, headers=h(READ_KEY)).status_code for _ in range(3)]
            assert codes == [200, 200, 429]
            r = c.get("/v1/geocode", params=GEO, headers=h(READ_KEY))
            assert 1 <= int(r.headers["retry-after"]) <= 60
    finally:
        engine.settings = old


def test_anonymous_read_and_query_key(engine):
    old = engine.settings
    try:
        with _client(engine, api_keys=f"app:write:{WRITE_KEY}", anonymous_role="read", allow_query_key=True) as c:
            assert c.get("/v1/geocode", params=GEO).status_code == 200
            assert c.post("/v1/feedback", json={"address": "x", "location": "116,39"}).status_code == 401
            # ?key= 开启后可用；反馈未配置日志 → 404 说明已过鉴权
            r = c.post("/v1/feedback", params={"key": WRITE_KEY}, json={"address": "x", "location": "116,39"})
            assert r.status_code == 404
    finally:
        engine.settings = old


def test_no_keys_keeps_open_api_but_locks_admin(engine):
    old = engine.settings
    try:
        with _client(engine) as c:
            assert c.get("/v1/geocode", params=GEO).status_code == 200
            assert c.get("/v1/admin/places").status_code == 401
            assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 200
    finally:
        engine.settings = old


def test_docs_toggle(engine):
    old = engine.settings
    try:
        with _client(engine, public_docs=False) as c:
            assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
        with _client(engine) as c:
            spec = c.get("/openapi.json").json()
            assert {"HTTPBearer", "APIKeyHeader"} <= set(spec["components"]["securitySchemes"])
    finally:
        engine.settings = old


def test_keygen_cli(capsys):
    from cngeocoder.cli import main

    assert main(["keygen", "--name", "ops", "--role", "admin"]) == 0
    out = capsys.readouterr().out.splitlines()
    key = out[0].split(": ", 1)[1]
    env = out[1].split(": ", 1)[1]
    assert key.startswith("cng_") and env == f"ops:admin:sha256:{hash_key(key)}"
    assert load_keys(env, None)[0].role == "admin"
