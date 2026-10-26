"""在线数据维护：要素（POI / 门牌 / 道路）与历史确认结果的增删改、待审核队列、变更日志。

数据仍然是一个 SQLite 文件。开启 ``GEOCODER_EDITABLE=1`` 后：

- 写入走单独的写连接，每次提交只锁几毫秒；每次写入在同一事务里递增 ``meta.edit_version``，
  各 worker 进程发现版本变化后清空本地缓存，最多延迟约 1 秒；
- 每次写入记一条变更日志（``change_log``），``cngeocoder edits export`` 导出为 JSONL，
  下次建库时用 ``build --edits`` 按顺序重放，在线编辑不会因为重建丢失。所有操作按 uid / key 幂等，
  同一份日志重放多次结果相同；
- 修改、删除要素时旧的全文索引条目留在倒排里（FTS5 contentless 表不能按 rowid 删除），
  检索时与 place 表关联会自动过滤，下次重建时清理；要素 id 只增不复用，失效条目不会挂到新要素上。

``Writer`` 只操作给定的连接，离线建库重放与在线编辑共用；``DataManager`` 负责在线场景的事务、
版本号和缓存失效。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import shapely
from shapely.geometry import mapping, shape

from . import coords
from .admin import native_code
from .build import PlaceIndexer, _transform_geom
from .geo import haversine

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)

KINDS = ("poi", "address", "road")
# 要素的输入字段（与建库输入的列名一致）；变更日志里存的就是这些字段
PLACE_FIELDS = ("uid", "kind", "name", "alias", "address", "road", "road_no", "adcode", "lng", "lat", "weight",
                "geometry")
_PLACE_SELECT = ("SELECT id, kind, name, road, road_no, adcode, address, lng, lat, weight, source, alias, abbr, uid, "
                 "geom FROM place")


class EditError(ValueError):
    """输入不合法（HTTP 400）。"""

    status = 400


class NotFound(EditError):
    status = 404


class ReadOnly(EditError):
    status = 409


def _split(v: str | None) -> list[str]:
    return [a for a in (v or "").split("|") if a]


def _now() -> int:
    return int(time.time())


class Writer:
    """在一个写连接上执行变更。调用方负责事务；``next_id`` 是下一个可用的要素 id。"""

    def __init__(self, db, indexer: PlaceIndexer, next_id: int):
        self.db, self.indexer, self.next_id = db, indexer, next_id
        self.roads: set[str] = set()  # 本次写入涉及的路名，在线时补进解析器的路名词典

    # ------------------------------------------------------------ 要素
    def place_record(self, uid: str, full: bool = False) -> dict | None:
        """已有要素还原成输入字段（WGS84），用于合并修改；full 时再带上 id、来源、简称等只读字段。"""
        r = self.db.execute(_PLACE_SELECT + " WHERE uid = ?", (uid,)).fetchone()
        return _record(r, full) if r else None

    def place_upsert(self, data: dict, source: str = "manual") -> tuple[dict, dict | None]:
        """新增或修改要素（按 uid）。修改时只覆盖 data 里出现的字段。返回 (修改后, 修改前)。"""
        data = _to_wgs84(data)
        uid = str(data.get("uid") or "").strip() or "m-" + uuid.uuid4().hex[:12]
        before = self.place_record(uid)
        rec = {**(before or {}), **{k: v for k, v in data.items() if k in PLACE_FIELDS}, "uid": uid}
        kind = (rec.get("kind") or "").lower() or None
        if kind is not None and kind not in KINDS:
            raise EditError(f"kind 只能是 {' / '.join(KINDS)}")
        if rec.get("adcode") not in (None, ""):
            try:
                code = native_code(rec["adcode"])
            except ValueError as e:
                raise EditError(f"非法区划编码: {rec['adcode']}") from e
            if code not in self.indexer.by_code:
                raise EditError(f"未知区划编码: {rec['adcode']}")
            rec["adcode"] = code
        # 改了地址或位置、又没有显式给编码时，重新推断所属行政区（推断不出来再沿用原编码）
        old_code = before.get("adcode") if before else None
        moved = before is not None and "adcode" not in data and any(k in data for k in ("address", "lng", "lat",
                                                                                          "geometry"))
        if moved:
            rec["adcode"] = None
        src = _source(self.db, uid) or source
        row = self._index(rec, kind, src)
        if row[0][6] is None and moved and old_code:
            rec["adcode"] = old_code
            row = self._index(rec, kind, src)
        place, fts, rt = row
        if place[1] != "road" and not place[2] and not place[4]:
            raise EditError("要素至少需要名称（name）或门牌（road_no）")
        if before is not None:
            self._delete_rows(uid)
        self.db.execute(f"INSERT INTO place VALUES ({','.join('?' * 17)})", place)
        self.db.execute("INSERT INTO place_fts(rowid, region, name, addr) VALUES (?,?,?,?)", fts)
        self.db.execute("INSERT INTO place_rtree VALUES (?,?,?,?,?)", rt)
        self.next_id += 1
        if place[3]:
            self.roads.add(place[3])
        return self.place_record(uid, full=True), before

    def _index(self, rec: dict, kind: str | None, source: str):
        try:
            if kind == "road" or (kind is None and rec.get("geometry")):
                props = {**rec, "kind": "road"}
                return self.indexer.road(props, rec.get("geometry"), self.next_id, source)
            row = {k: v for k, v in rec.items() if k != "geometry"}
            if kind:
                row["kind"] = kind
            return self.indexer.point(row, self.next_id, source)
        except EditError:
            raise
        except ValueError as e:
            raise EditError(str(e)) from e

    def place_delete(self, uid: str) -> dict:
        before = self.place_record(uid, full=True)
        if before is None:
            raise NotFound(f"要素不存在: {uid}")
        self._delete_rows(uid)
        return before

    def _delete_rows(self, uid: str) -> None:
        r = self.db.execute("SELECT id FROM place WHERE uid = ?", (uid,)).fetchone()
        if r:
            self.db.execute("DELETE FROM place WHERE id = ?", (r[0],))
            self.db.execute("DELETE FROM place_rtree WHERE id = ?", (r[0],))

    # ------------------------------------------------------------ 历史确认结果
    def memory_upsert(self, address: str, lng: float, lat: float, count: int = 1, spread: float = 0.0,
                      coordsys: str = "wgs84") -> tuple[list[dict], list[dict]]:
        """记录“地址写法 → 确认坐标”。同一地址的归一化写法、标准写法都会写入。返回 (写入的, 覆盖前的)。"""
        address = (address or "").strip()
        if not address:
            raise EditError("地址不能为空")
        lng, lat = coords.to_wgs84(*_check_lnglat(lng, lat), _coordsys(coordsys))
        if count < 1:
            raise EditError("count 至少为 1")
        if not (0 <= spread <= 300):
            raise EditError("spread 应在 0~300 米之间（超过 300 米的确认结果不会被采用）")
        p = self.indexer.parser.parse(address, alternatives=0)
        adcode = p.district.code if p.district else p.region_code
        keys = p.memory_keys()
        before = [m for m in (self.memory_get(k) for k in keys) if m]
        rows = [(k, round(lng, 7), round(lat, 7), int(count), float(spread), adcode, address) for k in keys]
        self.db.executemany("INSERT OR REPLACE INTO memory VALUES (?,?,?,?,?,?,?)", rows)
        return [self.memory_get(k) for k in keys], before

    def memory_get(self, key: str) -> dict | None:
        r = self.db.execute("SELECT key, lng, lat, n, spread, adcode, sample FROM memory WHERE key = ?",
                            (key,)).fetchone()
        return _memory(r) if r else None

    def memory_delete(self, key: str) -> dict:
        before = self.memory_get(key)
        if before is None:
            raise NotFound(f"确认结果不存在: {key}")
        self.db.execute("DELETE FROM memory WHERE key = ?", (key,))
        return before

    # ------------------------------------------------------------ 待审核
    def review_resolve(self, item_id: str, status: str, actor: str | None, ts: int | None = None) -> None:
        self.db.execute("INSERT OR REPLACE INTO review_state VALUES (?,?,?,?)", (item_id, status, ts or _now(), actor))

    # ------------------------------------------------------------ 日志与重放
    def log(self, op: str, target: str | None, data: Any, before: Any, actor: str | None,
            ts: int | None = None) -> int:
        cur = self.db.execute("INSERT INTO change_log (ts, actor, op, target, data, before) VALUES (?,?,?,?,?,?)",
                              (ts or _now(), actor, op, target, json.dumps(data, ensure_ascii=False),
                               json.dumps(before, ensure_ascii=False) if before is not None else None))
        return cur.lastrowid

    def apply(self, op: str, data: dict, actor: str | None = None, ts: int | None = None) -> None:
        """按变更日志的一条记录执行（建库重放用）。"""
        if op == "place.upsert":
            self.place_upsert(data, source=data.get("source") or "manual")
        elif op == "place.delete":
            self.place_delete(data["uid"])
        elif op == "memory.upsert":
            self.memory_upsert(data["address"], data["lng"], data["lat"], int(data.get("count") or 1),
                               float(data.get("spread") or 0))
        elif op == "memory.delete":
            self.memory_delete(data["key"])
        elif op == "review.resolve":
            self.review_resolve(data["id"], data["status"], actor, ts)
        else:
            raise EditError(f"未知操作: {op}")


def replay(db, indexer: PlaceIndexer, paths: tuple[str, ...], next_id: int) -> tuple[int, int]:
    """建库时重放变更日志。返回 (重放条数, 下一个要素 id)。目标已不存在的删除等操作跳过并告警。"""
    w = Writer(db, indexer, next_id)
    n = 0
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                try:
                    w.apply(item["op"], item.get("data") or {}, item.get("actor"), item.get("ts"))
                except (EditError, KeyError, ValueError) as e:
                    log.warning("skip edit %s:%d (%s): %s", path, lineno, item.get("op"), e)
                    continue
                w.log(item["op"], item.get("target"), item.get("data"), item.get("before"), item.get("actor"),
                      item.get("ts"))
                n += 1
    return n, w.next_id


# ---------------------------------------------------------------------- 在线管理
class DataManager:
    """管理界面、管理 API 与 MCP 共用的数据维护入口。读操作在只读模式下也可用。"""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.store = engine.store
        self.indexer = PlaceIndexer(engine.admin, engine.boundaries)

    @property
    def editable(self) -> bool:
        return self.store.editable

    # ------------------------------------------------------------ 读
    def list_places(self, q: str | None = None, kind: str | None = None, adcode: str | None = None,
                    source: str | None = None, offset: int = 0, limit: int = 20) -> dict:
        where, args = [], []
        if q:
            q = q.strip()
            like = f"%{q}%"
            where.append("(uid = ? OR name LIKE ? OR address LIKE ? OR road LIKE ? OR alias LIKE ?)")
            args += [q, like, like, like, like]
        if kind:
            where.append("kind = ?")
            args.append(kind)
        if adcode:
            try:
                code = native_code(adcode)
            except ValueError as e:
                raise EditError(f"非法区划编码: {adcode}") from e
            where.append("adcode LIKE ?")
            args.append(code + "%")
        if source:
            where.append("source = ?")
            args.append(source)
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        c = self.store.conn()
        cap = 10000
        total = c.execute(f"SELECT COUNT(*) FROM (SELECT 1 FROM place{cond} LIMIT {cap + 1})", args).fetchone()[0]
        rows = c.execute(_PLACE_SELECT + cond + " ORDER BY id DESC LIMIT ? OFFSET ?", [*args, limit, offset])
        return {"total": min(total, cap), "total_capped": total > cap, "offset": offset,
                "items": [self.view(_record(r, full=True)) for r in rows]}

    def get_place(self, uid: str) -> dict:
        r = self.store.conn().execute(_PLACE_SELECT + " WHERE uid = ?", (uid,)).fetchone()
        if r is None:
            raise NotFound(f"要素不存在: {uid}")
        return self.view(_record(r, full=True))

    def view(self, rec: dict) -> dict:
        """输出给界面 / API / MCP 的要素：位置用 {lng, lat}（便于统一转换坐标系），带所属行政区名称。"""
        out = {k: rec.get(k) for k in ("uid", "id", "kind", "name", "address", "road", "road_no", "adcode")}
        out["alias"] = _split(rec.get("alias"))
        out["abbr"] = _split(rec.get("abbr"))
        out["region"] = self.region_name(rec.get("adcode"))
        out["location"] = {"lng": round(rec["lng"], 7), "lat": round(rec["lat"], 7)}
        out["weight"] = rec.get("weight")
        out["source"] = rec.get("source")
        if rec.get("geometry"):
            out["geometry"] = rec["geometry"]
        return out

    def region_name(self, code: str | None) -> str | None:
        if not code:
            return None
        return " ".join(a.name for _, a in sorted(self.engine.admin.lineage(code).items()))

    def search_regions(self, q: str, limit: int = 20) -> list[dict]:
        """按名称片段或编码前缀查行政区，给编辑表单选择 adcode 用。"""
        q = (q or "").strip()
        if not q:
            return []
        out = []
        by_code = self.engine.admin.by_code
        if q.isdigit():
            try:
                hits = [a for a in (by_code.get(native_code(q)),) if a]
            except ValueError:
                hits = []
            hits += [a for c, a in by_code.items() if c.startswith(q) and a not in hits]
        else:
            hits = [a for a in by_code.values() if q in a.name]
            hits.sort(key=lambda a: (a.name != q, a.level, len(a.name)))
        for a in hits[:limit]:
            out.append({"adcode": a.code, "name": a.name, "level": a.level, "full_name": self.region_name(a.code),
                        "location": {"lng": a.lng, "lat": a.lat} if a.lng is not None else None})
        return out

    def list_memory(self, q: str | None = None, offset: int = 0, limit: int = 20) -> dict:
        args: list = []
        cond = ""
        if q:
            cond = " WHERE key LIKE ? OR sample LIKE ?"
            args = [f"%{q}%", f"%{q}%"]
        c = self.store.conn()
        total = c.execute("SELECT COUNT(*) FROM memory" + cond, args).fetchone()[0]
        rows = c.execute("SELECT key, lng, lat, n, spread, adcode, sample FROM memory" + cond +
                         " ORDER BY rowid DESC LIMIT ? OFFSET ?", [*args, limit, offset])
        return {"total": total, "offset": offset, "items": [_memory(r) for r in rows]}

    def list_changes(self, offset: int = 0, limit: int = 50, op: str | None = None) -> dict:
        cond, args = (" WHERE op = ?", [op]) if op else ("", [])
        c = self.store.conn()
        total = c.execute("SELECT COUNT(*) FROM change_log" + cond, args).fetchone()[0]
        rows = c.execute("SELECT seq, ts, actor, op, target, data, before FROM change_log" + cond +
                         " ORDER BY seq DESC LIMIT ? OFFSET ?", [*args, limit, offset])
        return {"total": total, "offset": offset, "items": [_change(r) for r in rows]}

    def export_changes(self) -> Iterator[str]:
        """全部变更日志，JSONL，按发生顺序；用于 build --edits 重放。"""
        rows = self.store.conn().execute("SELECT seq, ts, actor, op, target, data, before FROM change_log "
                                         "ORDER BY seq")
        for r in rows:
            yield json.dumps(_change(r), ensure_ascii=False) + "\n"

    # ------------------------------------------------------------ 待审核队列
    def _queue_files(self) -> dict[str, str | None]:
        s = self.engine.settings
        return {"review": s.review_log, "feedback": s.feedback_log}

    def review_items(self, source: str | None = None, status: str = "pending", offset: int = 0,
                     limit: int = 20) -> dict:
        """兜底结果（GEOCODER_REVIEW_LOG）与业务反馈（GEOCODER_FEEDBACK_LOG）合并成一个待审核队列。"""
        states = {r[0]: (r[1], r[2], r[3]) for r in self.store.conn().execute("SELECT * FROM review_state")}
        items = []
        for src, path in self._queue_files().items():
            if source and src != source:
                continue
            for item in _read_queue(src, path):
                st = states.get(item["id"])
                item["status"] = st[0] if st else "pending"
                if st:
                    item["resolved_at"], item["resolved_by"] = st[1], st[2]
                if status in ("all", item["status"]):
                    items.append(item)
        items.reverse()  # 新的在前
        enabled = {k: bool(v) for k, v in self._queue_files().items()}
        return {"total": len(items), "offset": offset, "enabled": enabled, "items": items[offset:offset + limit]}

    def _review_item(self, item_id: str) -> dict:
        for src, path in self._queue_files().items():
            for item in _read_queue(src, path):
                if item["id"] == item_id:
                    return item
        raise NotFound(f"待审核条目不存在: {item_id}")

    # ------------------------------------------------------------ 写
    def _tx(self, fn, actor: str | None):
        """一次写入：一个事务内完成数据修改、变更日志、id 计数与版本号，提交后让本进程立即生效。"""
        if not self.editable:
            raise ReadOnly("数据文件为只读模式：设置 GEOCODER_EDITABLE=1 并确保数据文件可写后重启服务")
        store = self.store
        with store.write_lock:
            db = store.writer()
            db.execute("BEGIN IMMEDIATE")
            try:
                meta = dict(db.execute("SELECT key, value FROM meta WHERE key IN ('place_seq', 'edit_version')"))
                seq = int(meta.get("place_seq") or db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM place")
                          .fetchone()[0])
                w = Writer(db, self.indexer, seq)
                result = fn(w)
                version = int(meta.get("edit_version") or 0) + 1
                db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                               [("place_seq", str(w.next_id)), ("edit_version", str(version))])
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        self.engine.on_edit(version, w.roads)
        return result

    # 区划编码与坐标的粗校验：距该级行政区中心超过这个距离（米）时给出提示
    _WARN_DISTANCE = {2: 1_500_000, 4: 300_000, 6: 80_000}

    def check(self, view: dict) -> list[str]:
        """要素的行政区与坐标是否明显矛盾（选错同名区划、经纬度写反、坐标系弄错等）。只提示，不拦截。"""
        code, loc = view.get("adcode"), view.get("location") or {}
        if not code or loc.get("lng") is None:
            return []
        lng, lat = loc["lng"], loc["lat"]
        out = []
        b = self.engine.boundaries
        pip = b.locate(lng, lat) if b else None
        if pip and not (code.startswith(pip) or pip.startswith(code)):
            out.append(f"坐标落在 {self.region_name(pip)}（{pip}），与区划编码 {code}（{self.region_name(code)}）不一致")
        else:
            key = code[:6] if len(code) >= 6 else code
            a = self.engine.admin.by_code.get(key)
            limit = self._WARN_DISTANCE.get(len(key))
            if a is not None and a.lng is not None and limit:
                d = haversine(lng, lat, a.lng, a.lat)
                if d > limit:
                    out.append(f"坐标距 {self.region_name(key)} 中心 {d / 1000:.0f} 公里，请确认区划编码、坐标和坐标系")
        return out

    def _with_warnings(self, rec: dict) -> dict:
        v = self.view(rec)
        w = self.check(v)
        if w:
            v["warnings"] = w
        return v

    def create_place(self, data: dict, actor: str | None = None) -> dict:
        if data.get("uid"):
            r = self.store.conn().execute("SELECT 1 FROM place WHERE uid = ?", (data["uid"],)).fetchone()
            if r:
                raise EditError(f"uid 已存在: {data['uid']}（修改请用更新接口）")

        def fn(w: Writer):
            after, _ = w.place_upsert(data)
            w.log("place.upsert", after["uid"], _journal(after), None, actor)
            return after

        return self._with_warnings(self._tx(fn, actor))

    def update_place(self, uid: str, patch: dict, actor: str | None = None) -> dict:
        def fn(w: Writer):
            if w.place_record(uid) is None:
                raise NotFound(f"要素不存在: {uid}")
            after, before = w.place_upsert({**patch, "uid": uid})
            w.log("place.upsert", uid, _journal(after), _journal(before), actor)
            return after

        return self._with_warnings(self._tx(fn, actor))

    def delete_place(self, uid: str, actor: str | None = None) -> dict:
        def fn(w: Writer):
            before = w.place_delete(uid)
            w.log("place.delete", uid, {"uid": uid}, _journal(before), actor)
            return before

        return self.view(self._tx(fn, actor))

    def add_memory(self, address: str, lng: float, lat: float, coordsys: str = "wgs84", count: int = 1,
                   spread: float = 0.0, actor: str | None = None) -> list[dict]:
        def fn(w: Writer):
            rows, before = w.memory_upsert(address, lng, lat, count, spread, coordsys=coordsys)
            data = {"address": address, "lng": rows[0]["location"]["lng"], "lat": rows[0]["location"]["lat"],
                    "count": count, "spread": spread}
            w.log("memory.upsert", address, data, before or None, actor)
            return rows

        return self._tx(fn, actor)

    def delete_memory(self, key: str, actor: str | None = None) -> dict:
        def fn(w: Writer):
            before = w.memory_delete(key)
            w.log("memory.delete", key, {"key": key}, before, actor)
            return before

        return self._tx(fn, actor)

    def resolve_review(self, item_id: str, action: str, lng: float | None = None, lat: float | None = None,
                       coordsys: str = "wgs84", actor: str | None = None) -> dict:
        """审核通过：写入历史确认结果（可改坐标），同样的写法下次直接命中；驳回：只标记状态。"""
        if action not in ("approve", "dismiss"):
            raise EditError("action 只能是 approve 或 dismiss")
        item = self._review_item(item_id)
        status = "approved" if action == "approve" else "dismissed"

        def fn(w: Writer):
            out = {"id": item_id, "status": status}
            if action == "approve":
                if lng is not None and lat is not None:
                    x, y = coords.to_wgs84(*_check_lnglat(lng, lat), _coordsys(coordsys))
                else:
                    x, y = item["location"]["lng"], item["location"]["lat"]
                rows, before = w.memory_upsert(item["address"], x, y)
                w.log("memory.upsert", item["address"], {"address": item["address"], "lng": round(x, 7),
                                                          "lat": round(y, 7), "count": 1, "spread": 0.0},
                      before or None, actor)
                out["memory"] = rows
            w.review_resolve(item_id, status, actor)
            w.log("review.resolve", item_id, {"id": item_id, "status": status}, None, actor)
            return out

        return self._tx(fn, actor)


# ---------------------------------------------------------------------- 工具函数
def _check_lnglat(lng, lat) -> tuple[float, float]:
    try:
        lng, lat = float(lng), float(lat)
    except (TypeError, ValueError) as e:
        raise EditError("坐标格式错误") from e
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        raise EditError("经纬度超出范围")
    return lng, lat


def _coordsys(value) -> coords.CoordSys:
    try:
        return coords.CoordSys.parse(value or "wgs84")
    except ValueError as e:
        raise EditError(str(e)) from e


def _to_wgs84(data: dict) -> dict:
    """输入统一转成 WGS84 再处理，变更日志里只存 WGS84。"""
    data = dict(data)
    cs = _coordsys(data.pop("coordsys", None))
    if data.get("geometry") is not None:
        try:
            g = shape(data["geometry"])
        except Exception as e:  # noqa: BLE001 shapely 对非法 GeoJSON 抛的异常类型不统一
            raise EditError(f"geometry 不是合法的 GeoJSON 几何: {e}") from e
        if cs.base is not coords.CoordSys.WGS84:
            g = _transform_geom(g, cs.value)
        data["geometry"] = mapping(shapely.set_precision(g, 1e-7))
    if "lng" in data or "lat" in data:
        if data.get("lng") is None or data.get("lat") is None:
            raise EditError("lng 与 lat 需同时提供")
        lng, lat = coords.to_wgs84(*_check_lnglat(data["lng"], data["lat"]), cs)
        data["lng"], data["lat"] = round(lng, 7), round(lat, 7)
    return data


def _source(db, uid: str) -> str | None:
    r = db.execute("SELECT source FROM place WHERE uid = ?", (uid,)).fetchone()
    return r[0] if r else None


def _record(r, full: bool = False) -> dict:
    pid, kind, name, road, road_no, adcode, address, lng, lat, weight, source, alias, abbr, uid, geom = r
    rec = {"uid": uid, "kind": kind, "name": name, "alias": alias, "address": address, "road": road,
           "road_no": road_no, "adcode": adcode, "weight": weight}
    if kind == "road" and geom:
        rec["geometry"] = json.loads(json.dumps(mapping(shapely.from_wkb(geom))))
    if kind != "road" or full:
        rec["lng"], rec["lat"] = lng, lat
    if full:
        rec.update(id=pid, source=source, abbr=abbr)
    return rec


def _journal(rec: dict | None) -> dict | None:
    """变更日志里的要素：只保留输入字段，去掉空值。"""
    if rec is None:
        return None
    skip = ("lng", "lat") if rec.get("kind") == "road" else ()  # 道路的位置由几何决定
    return {k: rec[k] for k in PLACE_FIELDS if rec.get(k) not in (None, "") and k not in skip}


def _memory(r) -> dict:
    key, lng, lat, n, spread, adcode, sample = r
    return {"key": key, "location": {"lng": lng, "lat": lat}, "count": n, "spread_m": spread, "adcode": adcode,
            "address": sample}


def _change(r) -> dict:
    seq, ts, actor, op, target, data, before = r
    return {"seq": seq, "ts": ts, "actor": actor, "op": op, "target": target,
            "data": json.loads(data) if data else None, "before": json.loads(before) if before else None}


def _read_queue(source: str, path: str | None) -> Iterator[dict]:
    if not path or not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            loc = raw.get("location") if isinstance(raw.get("location"), dict) else raw
            try:
                lng, lat = float(loc["lng"]), float(loc["lat"])
            except (KeyError, TypeError, ValueError):
                continue
            item_id = hashlib.sha1(f"{source}\x1f{line}".encode()).hexdigest()[:16]
            yield {"id": item_id, "source": source, "address": raw.get("address"),
                   "location": {"lng": lng, "lat": lat}, "precision": raw.get("precision"),
                   "adcode": raw.get("adcode"), "provider": raw.get("source"), "ts": raw.get("ts")}
