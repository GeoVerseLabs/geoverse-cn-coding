"""MCP：HTTP（/mcp）与 stdio 两种传输，工具按角色开放。"""

import io
import json

import pytest
from fastapi.testclient import TestClient

from cngeocoder.api import create_app
from cngeocoder.auth import Principal
from cngeocoder.engine import Settings
from cngeocoder.mcp_server import PROTOCOL_VERSIONS, McpServer, serve_stdio

from .conftest import ROAD_LAT0, ROAD_LNG

READ_KEY = "cng_mcp_read_key_000001"
ADMIN_KEY = "cng_mcp_admin_key_00001"


@pytest.fixture
def client(editable_engine):
    s = editable_engine.settings
    editable_engine.settings = Settings(db_path=s.db_path, editable=True, review_log=s.review_log,
                                        feedback_log=s.feedback_log,
                                        api_keys=f"agent:read:{READ_KEY},maint:admin:{ADMIN_KEY}")
    with TestClient(create_app(engine=editable_engine)) as c:
        yield c


def rpc(c, method, params=None, key=ADMIN_KEY, mid=1, **headers):
    msg = {"jsonrpc": "2.0", "method": method, **({"id": mid} if mid is not None else {})}
    if params is not None:
        msg["params"] = params
    return c.post("/mcp", json=msg, headers={"Authorization": f"Bearer {key}", **headers})


def call(c, name, args, key=ADMIN_KEY):
    r = rpc(c, "tools/call", {"name": name, "arguments": args}, key=key)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_handshake(client):
    r = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18" and "tools" in res["capabilities"]
    assert res["serverInfo"]["name"] == "cngeocoder" and res["instructions"]
    r = rpc(client, "initialize", {"protocolVersion": "1999-01-01"})
    assert r.json()["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]
    assert rpc(client, "notifications/initialized", mid=None).status_code == 202
    assert rpc(client, "ping").json()["result"] == {}
    assert rpc(client, "resources/list").json()["error"]["code"] == -32601


def test_transport_errors(client):
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    assert client.get("/mcp").status_code == 405
    bad = client.post("/mcp", content=b"{oops", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == -32700
    assert rpc(client, "ping", **{"Origin": "http://evil.example"}).status_code == 403
    assert rpc(client, "ping", **{"Origin": "http://testserver"}).status_code == 200
    r = rpc(client, "ping", **{"MCP-Protocol-Version": "2099-01-01"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32022
    assert r.json()["error"]["data"]["supported"] == list(PROTOCOL_VERSIONS)
    batch = client.post("/mcp", headers={"Authorization": f"Bearer {ADMIN_KEY}"},
                        json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                              {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert [m["id"] for m in batch.json()] == [1]


def test_tools_filtered_by_role(client):
    ro = {t["name"] for t in rpc(client, "tools/list", key=READ_KEY).json()["result"]["tools"]}
    rw = {t["name"] for t in rpc(client, "tools/list").json()["result"]["tools"]}
    assert {"geocode", "reverse_geocode", "parse_address", "search_regions"} <= ro
    assert not ro & {"add_place", "delete_place", "search_places"}
    assert {"add_place", "update_place", "delete_place", "add_memory", "resolve_review"} <= rw
    spec = next(t for t in rpc(client, "tools/list").json()["result"]["tools"] if t["name"] == "delete_place")
    assert spec["annotations"]["destructiveHint"] is True and spec["inputSchema"]["required"] == ["uid"]
    denied = call(client, "add_place", {"name": "x", "lng": 116, "lat": 39}, key=READ_KEY)
    assert denied["isError"] and "admin" in denied["content"][0]["text"]


def test_query_tools(client):
    g = call(client, "geocode", {"address": "北京市海淀区测试路35号", "coordsys": "gcj02"}, key=READ_KEY)
    assert not g["isError"] and g["structuredContent"]["coordsys"] == "gcj02"
    assert g["structuredContent"]["results"][0]["precision"] == "door"
    assert json.loads(g["content"][0]["text"])["query"] == "北京市海淀区测试路35号"
    r = call(client, "reverse_geocode", {"lng": ROAD_LNG, "lat": ROAD_LAT0}, key=READ_KEY)
    assert r["structuredContent"]["adcode"]["district"] == "110108"
    assert call(client, "search_regions", {"q": "海淀"}, key=READ_KEY)["structuredContent"]["items"][0]["adcode"] == "110108"
    info = call(client, "get_service_info", {}, key=READ_KEY)["structuredContent"]
    assert info["editable"] is True and "db" not in info
    assert call(client, "geocode", {}, key=READ_KEY)["isError"]
    assert call(client, "geocode", {"address": "x", "bogus": 1}, key=READ_KEY)["isError"]
    assert rpc(client, "tools/call", {"name": "nope", "arguments": {}}).json()["error"]["code"] == -32602


def test_agent_maintenance_flow(client):
    """agent 的典型维护流程：查重 → 新增 → 验证 → 修改 → 删除，全程记操作人。"""
    assert call(client, "search_places", {"q": "智能体驿站"})["structuredContent"]["total"] == 0
    added = call(client, "add_place", {"name": "智能体驿站", "alias": ["AI驿站"], "adcode": "110108",
                                       "lng": ROAD_LNG + 0.0003, "lat": ROAD_LAT0 + 0.0055})["structuredContent"]
    uid = added["uid"]
    g = call(client, "geocode", {"address": "海淀区AI驿站"})["structuredContent"]["results"][0]
    assert g["matched"]["name"] == "智能体驿站"
    upd = call(client, "update_place", {"uid": uid, "name": "智能体服务站"})["structuredContent"]
    assert upd["name"] == "智能体服务站" and upd["alias"] == ["AI驿站"]
    bad = call(client, "update_place", {"uid": uid, "adcode": "999999"})
    assert bad["isError"] and "区划编码" in bad["content"][0]["text"]
    mem = call(client, "add_memory", {"address": "海淀区测试路智能体门口", "lng": ROAD_LNG, "lat": ROAD_LAT0 + 0.005})
    assert mem["structuredContent"]["items"]
    assert call(client, "delete_place", {"uid": uid})["structuredContent"]["deleted"]["uid"] == uid
    changes = call(client, "list_changes", {"limit": 10})["structuredContent"]["items"]
    assert [c["op"] for c in changes] == ["place.delete", "memory.upsert", "place.upsert", "place.upsert"]
    assert {c["actor"] for c in changes} == {"maint"}


def test_stdio(editable_engine):
    server = McpServer(editable_engine)
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}),
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
             "not json",
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "add_place", "arguments": {"name": "本机工具点", "lng": ROAD_LNG,
                                                                       "lat": ROAD_LAT0 + 0.001}}})]
    out = io.StringIO()
    serve_stdio(server, Principal(name="local-agent", role="admin"), io.StringIO("\n".join(lines) + "\n"), out)
    msgs = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [m.get("id") for m in msgs] == [1, None, 2]
    assert msgs[0]["result"]["protocolVersion"] == "2025-03-26" and msgs[1]["error"]["code"] == -32700
    assert msgs[2]["result"]["structuredContent"]["name"] == "本机工具点"
    assert editable_engine.manager.list_changes()["items"][0]["actor"] == "local-agent"


def test_stdio_principal_from_key(monkeypatch, tmp_path):
    import argparse

    from cngeocoder.auth import hash_key
    from cngeocoder.cli import _stdio_principal

    keys = tmp_path / "k.json"
    keys.write_text(json.dumps([{"name": "bj-agent", "role": "admin", "key_sha256": hash_key(ADMIN_KEY),
                                 "regions": ["11"], "fallback": False}]), encoding="utf-8")
    args = argparse.Namespace(name="mcp-stdio", role="read")
    monkeypatch.delenv("GEOCODER_API_KEY", raising=False)
    assert _stdio_principal(args).role == "read"
    monkeypatch.setenv("GEOCODER_API_KEYS_FILE", str(keys))
    monkeypatch.setenv("GEOCODER_API_KEY", ADMIN_KEY)
    p = _stdio_principal(args)
    assert (p.name, p.role, p.regions, p.fallback) == ("bj-agent", "admin", ("11",), False)
    monkeypatch.setenv("GEOCODER_API_KEY", "cng_not_configured_000000")
    with pytest.raises(SystemExit):
        _stdio_principal(args)
