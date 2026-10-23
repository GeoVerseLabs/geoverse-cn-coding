"""装配：从一个数据文件加载解析器、正向/逆向编码器和兜底服务。"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from .admin import Admin, AdminIndex
from .boundary import BoundaryIndex
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

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ.get
        return cls(
            db_path=env("GEOCODER_DB", cls.db_path),
            tianditu_tk=env("TIANDITU_TK") or None,
            tianditu_url=env("TIANDITU_URL", cls.tianditu_url),
            tianditu_daily_quota=int(env("TIANDITU_DAILY_QUOTA", cls.tianditu_daily_quota)),
            min_confidence=float(env("GEOCODER_MIN_CONFIDENCE", cls.min_confidence)),
            review_log=env("GEOCODER_REVIEW_LOG") or None,
            feedback_log=env("GEOCODER_FEEDBACK_LOG") or None,
            cache_size=int(env("GEOCODER_CACHE_SIZE", cls.cache_size)),
        )


class Engine:
    def __init__(self, settings: Settings):
        t0 = time.time()
        self.settings = settings
        if not os.path.exists(settings.db_path):
            raise FileNotFoundError(f"数据文件不存在: {settings.db_path}（先运行 cngeocoder build）")
        self.store = Store(settings.db_path)
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
        self.load_seconds = round(time.time() - t0, 2)
        log.info("engine loaded in %ss from %s", self.load_seconds, settings.db_path)

    def info(self) -> dict:
        meta = self.store.meta()
        return {
            "db": os.path.abspath(self.settings.db_path),
            "built_at": meta.get("built_at"),
            "schema_version": meta.get("schema_version"),
            "counts": self.store.counts(),
            "boundary_levels": sorted(self.boundaries.levels) if self.boundaries else [],
            "fallback": self.fallback.stats() if self.fallback else None,
            "cache": {"geocode": self.geocoder.cache.stats(), "reverse": self.reverse.cache.stats()},
            "load_seconds": self.load_seconds,
        }
