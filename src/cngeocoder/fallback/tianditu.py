"""天地图地理编码兜底（http://lbs.tianditu.gov.cn/server/geocodinginterface.html）。

天地图坐标为 CGCS2000，与 WGS84 的差异可忽略，直接按 WGS84 使用。
调用带每日配额计数与熔断：连续失败达到阈值后在冷却期内直接跳过，避免第三方限流拖垮主链路。
只用标准库 urllib，不增加依赖。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

# 天地图返回的 level 文本 → 本服务精度等级
_LEVEL_MAP = {
    "门址": "door", "门牌": "door", "兴趣点": "poi", "POI": "poi", "道路": "road", "道路交叉口": "road",
    "乡镇": "town", "村庄": "community", "区县": "district", "地级市": "city", "城市": "city", "省": "province",
}
_TZ = dt.timezone(dt.timedelta(hours=8))


@dataclass
class FallbackResult:
    lng: float
    lat: float
    precision: str
    confidence: float
    source: str
    raw: dict


class CircuitBreaker:
    def __init__(self, threshold: int = 5, cooldown: float = 60.0):
        self.threshold, self.cooldown = threshold, cooldown
        self.failures = 0
        self.opened_at: float | None = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self.opened_at is None:
                return True
            if time.monotonic() - self.opened_at >= self.cooldown:
                return True  # 半开：放一个请求试探
            return False

    def success(self) -> None:
        with self._lock:
            self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.monotonic()

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half-open" if time.monotonic() - self.opened_at >= self.cooldown else "open"


class DailyQuota:
    def __init__(self, limit: int):
        self.limit = limit
        self.day = dt.datetime.now(_TZ).date()
        self.used = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            today = dt.datetime.now(_TZ).date()
            if today != self.day:
                self.day, self.used = today, 0
            if self.used >= self.limit:
                return False
            self.used += 1
            return True


class TiandituGeocoder:
    source = "tianditu"

    def __init__(self, tk: str, base_url: str = "https://api.tianditu.gov.cn/geocoder", timeout: float = 2.0,
                 daily_quota: int = 10000, breaker: CircuitBreaker | None = None, opener=None):
        self.tk, self.base_url, self.timeout = tk, base_url, timeout
        self.quota = DailyQuota(daily_quota)
        self.breaker = breaker or CircuitBreaker()
        self._open = opener or urllib.request.urlopen

    def geocode(self, address: str) -> FallbackResult | None:
        if not self.breaker.allow() or not self.quota.take():
            return None
        ds = json.dumps({"keyWord": address}, ensure_ascii=False)
        url = f"{self.base_url}?ds={urllib.parse.quote(ds)}&tk={urllib.parse.quote(self.tk)}"
        try:
            with self._open(url, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            self.breaker.failure()
            log.warning("tianditu request failed: %s", e)
            return None
        if str(data.get("status")) != "0" or not data.get("location"):
            # 业务上“查不到”不算服务故障，不计入熔断
            self.breaker.success()
            return None
        self.breaker.success()
        loc = data["location"]
        try:
            lng, lat = float(loc["lon"]), float(loc["lat"])
        except (KeyError, TypeError, ValueError):
            return None
        score = float(loc.get("score") or 60)
        precision = _LEVEL_MAP.get(str(loc.get("level") or ""), "poi")
        return FallbackResult(lng, lat, precision, round(min(1.0, score / 100.0) * 0.9, 3), self.source,
                              {"level": loc.get("level"), "score": loc.get("score"), "keyWord": loc.get("keyWord")})

    def stats(self) -> dict:
        return {"provider": self.source, "breaker": self.breaker.state, "quota_used": self.quota.used,
                "quota_limit": self.quota.limit}
