"""请求鉴权：API Key + 三级角色 + 限流。

一次请求的鉴权流程：

1. 取密钥：``Authorization: Bearer <key>`` 或 ``X-API-Key: <key>``（开启 ``GEOCODER_ALLOW_QUERY_KEY`` 时也接受 ``?key=``）；
2. 认证：密钥做 SHA-256 后查表（配置里可以只存哈希，明文不落盘）；没带密钥按匿名角色处理；
   带了错误的密钥直接 401，同一 IP 每分钟错误次数超过上限时 429，防止猜测；
3. 授权：接口要求的角色高于调用方角色时 403；
4. 限流：按调用方（密钥名或匿名 IP）每分钟计数，批量接口按条数计，超过上限 429 并给出 Retry-After；
   数据范围：带 ``regions`` 的密钥只能读写这些区划内的数据；``fallback=false`` 的密钥不会触发兜底服务；
5. 记账：调用方名称写入 ``request.state.principal``，在线编辑的变更日志里记为操作人，
   各密钥的调用次数在管理界面可见。

角色逐级包含：``read``（解析、正向 / 逆向、坐标转换、元数据）⊂ ``write``（再加业务反馈）
⊂ ``admin``（再加数据管理接口、管理界面数据、MCP 维护工具）。匿名调用方最多 ``write``，永远不是 ``admin``。
带数据范围的 admin 是“区域管理员”：不能导出全量变更记录、查看密钥配置。

拒绝（401 / 403 / 429）都记 WARNING 日志（logger ``cngeocoder.auth``），便于审计和告警。

密钥配置（两种可同时使用）：

- ``GEOCODER_API_KEYS="ops:admin:sha256:9f86d0…,app:read:明文密钥"``，逗号分隔，每项 ``名称:角色:密钥``；
- ``GEOCODER_API_KEYS_FILE``：JSON 数组 ``[{"name","role","key" 或 "key_sha256","rpm","regions","fallback"}]``，
  或每行一条 ``名称:角色:密钥``（``#`` 开头为注释）。限流（``rpm``）、数据范围（``regions``，区划编码前缀）、
  是否允许触发兜底（``fallback``）只能在 JSON 里配置。文件修改后约 5 秒内自动生效，吊销密钥不用重启。

``cngeocoder keygen --name ops --role admin`` 生成随机密钥及其配置行。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .admin import native_code

ROLES = {"none": 0, "read": 1, "write": 2, "admin": 3}
KEY_PREFIX = "cng_"
FAILED_PER_MINUTE = 20  # 同一 IP 每分钟允许的错误密钥次数
RELOAD_INTERVAL = 5.0  # 秒；密钥文件改动后最多这么久生效（吊销密钥不用重启）

log = logging.getLogger("cngeocoder.auth")


class AuthError(Exception):
    def __init__(self, status: int, detail: str, headers: dict | None = None):
        super().__init__(detail)
        self.status, self.detail, self.headers = status, detail, headers or {}


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(24)


def _regions(value, where: str) -> tuple[str, ...] | None:
    """数据范围：区划编码前缀列表（"11" 北京、"3101" 上海市辖区、"440305" 南山区）。"""
    if value in (None, [], ""):
        return None
    if isinstance(value, str):
        value = [v for v in value.replace("|", ",").split(",") if v.strip()]
    out = []
    for v in value:
        try:
            out.append(native_code(str(v)))
        except ValueError as e:
            raise ValueError(f"{where}: regions 里的 {v!r} 不是区划编码") from e
    return tuple(dict.fromkeys(out))


@dataclass
class ApiKey:
    name: str
    role: str
    key_sha256: str
    rpm: int | None = None  # 每分钟上限；None 用全局 GEOCODER_RATE_LIMIT
    regions: tuple[str, ...] | None = None  # 数据范围（区划编码前缀）；None 为全国
    fallback: bool = True  # 是否允许触发兜底服务（消耗天地图等外部配额）

    def public(self) -> dict:
        return {"name": self.name, "role": self.role, "rpm": self.rpm, "key_hint": self.key_sha256[:8],
                "regions": list(self.regions) if self.regions else None, "fallback": self.fallback}


@dataclass
class Principal:
    """一次请求的调用方。"""

    name: str
    role: str
    anonymous: bool = False
    rpm: int | None = None
    regions: tuple[str, ...] | None = None
    fallback: bool = True

    def allows(self, role: str) -> bool:
        return ROLES[self.role] >= ROLES[role]

    @property
    def scoped(self) -> bool:
        """是否只授权了部分区域的数据。"""
        return bool(self.regions)

    def in_scope(self, code: str | None) -> bool:
        if not self.regions:
            return True
        return bool(code) and any(code.startswith(r) for r in self.regions)

    def public(self) -> dict:
        return {"name": self.name, "role": self.role, "anonymous": self.anonymous,
                "regions": list(self.regions) if self.regions else None, "fallback": self.fallback}


def _parse_entry(entry: str, where: str) -> ApiKey:
    parts = entry.strip().split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"{where}: 密钥配置应为 名称:角色:密钥，实际为 {entry!r}")
    name, role, secret = (p.strip() for p in parts)
    return _make(name, role, secret, where)


def _make(name: str, role: str, secret: str, where: str, hashed: bool = False, rpm: int | None = None,
          regions=None, fallback: bool = True) -> ApiKey:
    role = role.lower()
    if role not in ("read", "write", "admin"):
        raise ValueError(f"{where}: 未知角色 {role!r}（可选 read / write / admin）")
    if hashed or secret.startswith("sha256:"):
        h = secret.removeprefix("sha256:").lower()
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
            raise ValueError(f"{where}: {name} 的 sha256 哈希应为 64 位十六进制")
    else:
        if len(secret) < 16:
            raise ValueError(f"{where}: {name} 的密钥太短（至少 16 个字符，可用 cngeocoder keygen 生成）")
        h = hash_key(secret)
    if rpm is not None and (not isinstance(rpm, int) or rpm < 0):
        raise ValueError(f"{where}: {name} 的 rpm 应为非负整数")
    return ApiKey(name=name, role=role, key_sha256=h, rpm=rpm, regions=_regions(regions, where),
                  fallback=bool(fallback))


def load_keys(spec: str | None, path: str | None) -> list[ApiKey]:
    keys: list[ApiKey] = []
    if spec:
        keys += [_parse_entry(e, "GEOCODER_API_KEYS") for e in spec.split(",") if e.strip()]
    if path:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if text.lstrip().startswith("["):
            for i, item in enumerate(json.loads(text)):
                where = f"{path}[{i}]"
                unknown = set(item) - {"name", "role", "key", "key_sha256", "rpm", "regions", "fallback", "note"}
                if unknown:
                    raise ValueError(f"{where}: 未知字段 {', '.join(sorted(unknown))}")
                hashed = "key_sha256" in item
                keys.append(_make(item["name"], item["role"], item["key_sha256"] if hashed else item["key"], where,
                                  hashed, item.get("rpm"), item.get("regions"), item.get("fallback", True)))
        else:
            for i, line in enumerate(text.splitlines(), 1):
                line = line.strip()
                if line and not line.startswith("#"):
                    keys.append(_parse_entry(line, f"{path}:{i}"))
    names = [k.name for k in keys]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ValueError(f"密钥名称重复: {', '.join(sorted(dup))}")
    return keys


class _Window:
    """固定一分钟窗口计数。单进程内有效；多 worker 时每个 worker 各自计数（上限按 worker 数折算）。"""

    def __init__(self):
        self._data: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()

    def hit(self, who: str, limit: int, cost: int = 1) -> int | None:
        """计 cost 次；超过上限时返回需要等待的秒数（超限的这次不计入）。"""
        now = time.time()
        minute = int(now // 60)
        with self._lock:
            m, n = self._data.get(who, (minute, 0))
            if m != minute:
                m, n = minute, 0
            if n + cost > limit:
                self._data[who] = (m, n)
                return max(1, int(60 - now % 60))
            self._data[who] = (m, n + cost)
            if len(self._data) > 50000:  # 防止匿名 IP 撑爆内存
                self._data = {k: v for k, v in self._data.items() if v[0] == minute}
        return None

    def refund(self, who: str, cost: int) -> None:
        with self._lock:
            m, n = self._data.get(who, (0, 0))
            if m == int(time.time() // 60):
                self._data[who] = (m, max(0, n - cost))


@dataclass
class KeyRing:
    keys: list[ApiKey] = field(default_factory=list)
    anonymous_role: str = "write"
    allow_query_key: bool = False
    rate_limit: int = 0
    public_docs: bool = True
    spec: str | None = None   # 用于密钥文件热加载时重建
    path: str | None = None

    def __post_init__(self):
        self._index()
        self._limits = _Window()
        self._failures = _Window()
        self._mtime = self._file_mtime()
        self._checked = time.monotonic()
        self._reload_lock = threading.Lock()
        self.usage: dict[str, int] = defaultdict(int)
        self.denied: dict[str, int] = defaultdict(int)

    def _index(self) -> None:
        self._by_hash = {k.key_sha256: k for k in self.keys}

    @classmethod
    def from_settings(cls, s) -> KeyRing:
        keys = load_keys(s.api_keys, s.api_keys_file)
        # 没配密钥时保持原有行为（开放查询与反馈）；配了密钥则默认必须带密钥
        anon = (s.anonymous_role or ("none" if keys or s.api_keys_file else "write")).lower()
        if anon not in ("none", "read", "write"):
            raise ValueError(f"GEOCODER_ANONYMOUS_ROLE 只能是 none / read / write，实际为 {anon!r}")
        return cls(keys=keys, anonymous_role=anon, allow_query_key=s.allow_query_key, rate_limit=s.rate_limit,
                   public_docs=s.public_docs, spec=s.api_keys, path=s.api_keys_file)

    @property
    def enabled(self) -> bool:
        """是否要求鉴权（匿名调用方不是完整权限）。"""
        return bool(self.keys) or self.anonymous_role != "write"

    # ------------------------------------------------------------ 密钥文件热加载
    def _file_mtime(self) -> float | None:
        try:
            return os.stat(self.path).st_mtime if self.path else None
        except OSError:
            return None

    def maybe_reload(self) -> None:
        """密钥文件变化后重新加载：新增、吊销、改角色都不用重启。文件有误时保留旧配置并记错误日志。"""
        if not self.path or time.monotonic() - self._checked < RELOAD_INTERVAL:
            return
        with self._reload_lock:
            if time.monotonic() - self._checked < RELOAD_INTERVAL:
                return
            self._checked = time.monotonic()
            mtime = self._file_mtime()
            if mtime == self._mtime:
                return
            try:
                keys = load_keys(self.spec, self.path)
            except (OSError, ValueError) as e:
                log.error("密钥文件 %s 加载失败，继续使用旧配置: %s", self.path, e)
                return
            self._mtime = mtime
            self.keys = keys
            self._index()
            log.warning("密钥文件已重新加载: %s（%d 个密钥）", self.path, len(keys))

    # ------------------------------------------------------------ 鉴权流程
    def verify(self, token: str) -> ApiKey | None:
        h = hash_key(token)
        k = self._by_hash.get(h)
        # 字典按哈希查找本身不泄露明文；再做一次常量时间比较，避免对哈希前缀的时序探测
        return k if k is not None and hmac.compare_digest(k.key_sha256, h) else None

    def principal_for(self, k: ApiKey) -> Principal:
        return Principal(name=k.name, role=k.role, rpm=k.rpm, regions=k.regions, fallback=k.fallback)

    def authenticate(self, token: str | None, client: str) -> Principal:
        self.maybe_reload()
        if token:
            k = self.verify(token)
            if k is None:
                wait = self._failures.hit(client, FAILED_PER_MINUTE)
                self.denied["invalid_key"] += 1
                log.warning("auth denied: invalid key from %s", client)
                if wait:
                    raise AuthError(429, "错误密钥尝试过多，请稍后再试", {"Retry-After": str(wait)})
                raise AuthError(401, "API Key 无效", {"WWW-Authenticate": 'Bearer realm="cngeocoder"'})
            return self.principal_for(k)
        # 配了密钥时匿名调用方不能用兜底（外部配额是共享的付费资源）
        return Principal(name=f"anonymous@{client}", role=self.anonymous_role, anonymous=True,
                         fallback=not self.keys)

    def authorize(self, p: Principal, role: str, global_only: bool = False) -> None:
        if p.allows(role) and not (global_only and p.scoped):
            return
        who = p.name if not p.anonymous else "anonymous"
        self.denied[who] += 1
        log.warning("auth denied: %s (role %s) needs %s%s", p.name, p.role, role, " (全国范围)" if global_only else "")
        if p.anonymous:
            raise AuthError(401, f"需要 API Key（{role} 及以上角色）", {"WWW-Authenticate": 'Bearer realm="cngeocoder"'})
        if not p.allows(role):
            raise AuthError(403, f"权限不足：需要 {role} 角色，当前为 {p.role}")
        raise AuthError(403, "权限不足：该操作需要不限数据范围的管理员密钥")

    def throttle(self, p: Principal, cost: int = 1) -> None:
        """限流计数。批量接口按条数计（cost = 条数）。"""
        limit = p.rpm if p.rpm is not None else self.rate_limit
        self.usage[p.name if not p.anonymous else "anonymous"] += cost
        if limit and limit > 0:
            wait = self._limits.hit(p.name, limit, cost)
            if wait:
                log.warning("auth denied: %s rate limited (%d/min)", p.name, limit)
                raise AuthError(429, f"请求过于频繁（每分钟上限 {limit}，本次计 {cost}）", {"Retry-After": str(wait)})

    def charge(self, p: Principal, items: int) -> None:
        """批量接口按条数计：鉴权时已计 1 次，这里补上其余条数；超限时整次请求不计数。"""
        if items <= 1:
            return
        try:
            self.throttle(p, items - 1)
        except AuthError:
            self._limits.refund(p.name, 1)
            self.usage[p.name if not p.anonymous else "anonymous"] -= items
            raise

    def check(self, token: str | None, client: str, role: str, global_only: bool = False) -> Principal:
        p = self.authenticate(token, client)
        self.authorize(p, role, global_only)
        self.throttle(p)
        return p

    def describe(self) -> dict:
        return {
            "enabled": self.enabled,
            "anonymous_role": self.anonymous_role,
            "allow_query_key": self.allow_query_key,
            "rate_limit": self.rate_limit,
            "keys_file": bool(self.path),
            "keys": [{**k.public(), "requests": self.usage.get(k.name, 0)} for k in self.keys],
            "anonymous_requests": self.usage.get("anonymous", 0),
            "denied": dict(self.denied),
        }


def token_from(headers) -> str | None:
    auth = headers.get("authorization")
    if auth:
        scheme, _, value = auth.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return headers.get("x-api-key") or None


def keygen_line(name: str, role: str) -> dict:
    key = generate_key()
    return {"name": name, "role": role, "key": key, "env": f"{name}:{role}:sha256:{hash_key(key)}",
            "json": {"name": name, "role": role, "key_sha256": hash_key(key)}}
