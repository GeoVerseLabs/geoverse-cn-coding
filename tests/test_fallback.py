import io
import json

from cngeocoder.fallback import CircuitBreaker, DailyQuota, TiandituGeocoder


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(payload):
    calls = []

    def _open(url, timeout):
        calls.append(url)
        return FakeResp(json.dumps(payload, ensure_ascii=False).encode())

    _open.calls = calls
    return _open


def test_tianditu_parse_success():
    op = opener_returning({"status": "0", "msg": "ok",
                           "location": {"lon": 116.39, "lat": 39.9, "score": 90, "level": "门址", "keyWord": "x"}})
    g = TiandituGeocoder("tk", opener=op)
    r = g.geocode("北京市东城区东长安街1号")
    assert r.lng == 116.39 and r.precision == "door" and 0.8 < r.confidence <= 0.9
    assert "tk=tk" in op.calls[0] and "%E5%8C%97%E4%BA%AC" in op.calls[0]


def test_tianditu_not_found_is_not_failure():
    g = TiandituGeocoder("tk", opener=opener_returning({"status": "101", "msg": "no result"}))
    assert g.geocode("x") is None
    assert g.breaker.state == "closed"


def test_circuit_breaker_opens_after_failures():
    def boom(url, timeout):
        raise TimeoutError("timeout")

    g = TiandituGeocoder("tk", opener=boom, breaker=CircuitBreaker(threshold=2, cooldown=60))
    assert g.geocode("a") is None and g.geocode("b") is None
    assert g.breaker.state == "open"
    used = g.quota.used
    assert g.geocode("c") is None and g.quota.used == used  # 熔断期间不消耗配额也不发请求


def test_daily_quota():
    q = DailyQuota(2)
    assert q.take() and q.take() and not q.take()
