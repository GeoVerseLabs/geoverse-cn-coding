"""兜底地理编码服务（本地置信度不足时调用）。"""

from .tianditu import CircuitBreaker, DailyQuota, FallbackResult, TiandituGeocoder

__all__ = ["CircuitBreaker", "DailyQuota", "FallbackResult", "TiandituGeocoder"]
