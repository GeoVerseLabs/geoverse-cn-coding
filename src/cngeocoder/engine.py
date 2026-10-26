"""装配：从一个数据文件加载解析器、正向/逆向编码器和兜底服务。"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

from .admin import Admin, AdminIndex
from .boundary import BoundaryIndex
from .edit import DataManager
from .fallback import TiandituGeocoder
from .geocoder import Geocoder
from .parser import AddressParser
from .reverse import ReverseGeocoder
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class Settings:
    db_path: str = "var/geocoder.db"
    tianditu_tk: str | None = None
    tianditu_url: str = "https://api.tianditu.gov.cn/geocoder"
    tianditu_daily_quota: int = 10000
    min_confidence: float = 0.6
    review_log: str | None = None
    feedback_log: str | None = None
    cache_size: int = 20000
    editable: bool = False              # 在线编辑（管理界面 / 管理 API / MCP 写操作）
    # 鉴权（见 auth.py）
    api_keys: str | None = None         # "名称:角色:密钥,..."，密钥可写成 sha256:<hex>
    api_keys_file: str | None = None    # JSON 或每行一条的密钥文件
    anonymous_role: str | None = None   # 未带密钥的请求的角色：none / read / write；默认见 auth.py
    allow_query_key: bool = False       # 是否接受 ?key= 传密钥（会进访问日志，默认关闭）
    rate_limit: int = 0                 # 每个调用方每分钟请求上限，0 不限
    public_docs: bool = True            # 是否开放 /docs、/openapi.json（关闭后不提供交互文档）
    mcp_origins: str | None = None      # /mcp 允许的浏览器 Origin，逗号分隔，* 为不限；默认只允许同源

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get

        def flag(name: str, default: bool) -> bool:
            v = env(name)
            return default if v in (None, "") else v.strip().lower() in ("1", "true", "yes", "on")

        return cls(
            db_path=env("GEOCODER_DB", cls.db_path),
            tianditu_tk=env("TIANDITU_TK") or None,
            tianditu_url=env("TIANDITU_URL", cls.tianditu_url),
            tianditu_daily_quota=int(env("TIANDITU_DAILY_QUOTA", cls.tianditu_daily_quota)),
            min_confidence=float(env("GEOCODER_MIN_CONFIDENCE", cls.min_confidence)),
            review_log=env("GEOCODER_REVIEW_LOG") or None,
            feedback_log=env("GEOCODER_FEEDBACK_LOG") or None,
            cache_size=int(env("GEOCODER_CACHE_SIZE", cls.cache_size)),
            editable=flag("GEOCODER_EDITABLE", False),
            api_keys=env("GEOCODER_API_KEYS") or None,
            api_keys_file=env("GEOCODER_API_KEYS_FILE") or None,
            anonymous_role=env("GEOCODER_ANONYMOUS_ROLE") or None,
            allow_query_key=flag("GEOCODER_ALLOW_QUERY_KEY", False),
            rate_limit=int(env("GEOCODER_RATE_LIMIT") or 0),
            public_docs=flag("GEOCODER_PUBLIC_DOCS", True),
            mcp_origins=env("GEOCODER_MCP_ORIGINS") or None,
        )


class Engine:
    def __init__(self, settings: Settings):
        t0 = time.time()
        self.settings = settings
        if not os.path.exists(settings.db_path):
            raise FileNotFoundError(f"数据文件不存在: {settings.db_path}（先运行 cngeocoder build）")
        self.store = Store(settings.db_path, editable=settings.editable)
        admins = [Admin(code=c, name=n, level=lv, parent=p, lng=x, lat=y)
                  for c, n, lv, p, x, y in self.store.iter_admins()]
        self.admin = AdminIndex(admins, list(self.store.iter_aliases()))
        self.parser = AddressParser(self.admin, road_lexicon=self.store.road_names())
        bidx = BoundaryIndex.from_wkb(self.store.iter_boundaries())
        self.boundaries = bidx if bidx else None
        self.fallback = TiandituGeocoder(settings.tianditu_tk, base_url=settings.tianditu_url,
                                         daily_quota=settings.tianditu_daily_quota) if settings.tianditu_tk else None
        self.geocoder = Geocoder(self.store, self.admin, self.parser, fallback=self.fallback,
                                 cache_size=settings.cache_size, min_confidence=settings.min_confidence,
                                 review_log=settings.review_log)
        self.reverse = ReverseGeocoder(self.store, self.admin, self.boundaries, cache_size=settings.cache_size)
        self.manager = DataManager(self)
        self._version = self.store.edit_version()
        self._checked = time.monotonic()
        self._sync_lock = threading.Lock()
        self.load_seconds = round(time.time() - t0, 2)
        log.info("engine loaded in %ss from %s", self.load_seconds, settings.db_path)

    # ------------------------------------------------------------ 在线编辑后的一致性
    SYNC_INTERVAL = 1.0  # 秒；其他 worker 的写入最多延迟这么久生效

    def sync(self) -> None:
        """在线编辑模式下检查数据版本：其他进程写入后清空本地缓存、刷新路名词典。每秒最多查一次。"""
        if not self.store.editable or time.monotonic() - self._checked < self.SYNC_INTERVAL:
            return
        with self._sync_lock:
            if time.monotonic() - self._checked < self.SYNC_INTERVAL:
                return
            self._checked = time.monotonic()
            v = self.store.edit_version()
            if v != self._version:
                self._version = v
                self.parser.road_lexicon.update(self.store.road_names())
                self._clear_caches()

    def on_edit(self, version: int, roads: set[str]) -> None:
        """本进程写入后立即生效（不等 sync）。"""
        self._version = version
        self.parser.road_lexicon.update(roads)
        self._clear_caches()

    def _clear_caches(self) -> None:
        self.geocoder.cache.clear()
        self.reverse.cache.clear()

    def info(self) -> dict:
        meta = self.store.meta()
        return {
            "db": os.path.abspath(self.settings.db_path),
            "built_at": meta.get("built_at"),
            "schema_version": meta.get("schema_version"),
            "editable": self.store.editable,
            "edit_version": int(meta.get("edit_version") or 0),
            "counts": self.store.counts(),
            "boundary_levels": sorted(self.boundaries.levels) if self.boundaries else [],
            "fallback": self.fallback.stats() if self.fallback else None,
            "cache": {"geocode": self.geocoder.cache.stats(), "reverse": self.reverse.cache.stats()},
            "load_seconds": self.load_seconds,
        }
