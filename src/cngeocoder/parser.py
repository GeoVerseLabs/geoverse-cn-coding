"""中文地址结构化解析。

输出结构与方案文档一致：
province / city / district / town / community / road / road_no / poi / building / unit / floor / room + 各级 adcode。

流程：归一化 → 行政区链（admin.AdminIndex）→ 对剩余“详细地址”按后缀切分：
乡镇街道 → 社区/村 → 道路 + 门牌（含上海“弄”、北京“甲 1 号”）→ POI → 楼栋/单元/楼层/室。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .admin import LEVEL_CITY, LEVEL_DISTRICT, LEVEL_PROVINCE, LEVEL_TOWN, Admin, AdminChain, AdminIndex
from .normalize import normalize, normalize_numbers

_CJK = "一-龥"

_TOWN_RE = re.compile(rf"^(?P<town>[{_CJK}]{{2,10}}?(?:街道办事处|地区办事处|街道|镇|乡|苏木))(?!路|街|大街|大道|道)")
_COMMUNITY_RE = re.compile(
    rf"^(?P<community>[{_CJK}A-Z0-9]{{2,12}}?(?:社区居民委员会|社区居委会|村民委员会|居委会|村委会|社区|嘎查|村)"
    rf"(?:\d+组)?)(?!大街|大道|路|街|道|巷|口|里|桥|小学|中学|站)"
)
_ROAD_SUFFIX = "大道|大街|公路|胡同|路|街|巷|道"
_ROAD_RE = re.compile(
    rf"(?P<road>[{_CJK}A-Z0-9]{{1,18}}?(?:{_ROAD_SUFFIX})(?:[东西南北中]段|\d+段)?(?:辅路)?)"
    rf"(?![道口里坊村桥心馆])"
)
# 路名里如果夹着这些词，说明前面是 POI/片区，路名从它后面开始
_POI_CUT_RE = re.compile(r"(小区|大厦|广场|中心|花园|家园|公寓|大楼|商场|医院|学校|大学|学院|公园|酒店|宾馆|园区|期|号院|号楼|栋|座|园)")
_NO_RE = re.compile(r"^(?:(?P<lane>\d+)弄)?(?:(?P<no>[甲乙丙丁]?\d+(?:-\d+)?)(?P<nosuf>号院|号(?!楼|馆|房)))?")
_BARE_NO_RE = re.compile(r"^(?P<no>\d+)$")
_CROSS_RE = re.compile(rf"^(?:与|和|交)(?P<road2>[{_CJK}A-Z0-9]{{1,12}}?(?:{_ROAD_SUFFIX}))(?:交叉路口|交叉口|交汇处|交口|路口)?")
_DASH_TAIL_RE = re.compile(r"(?P<b>\d+)-(?P<u>\d+)-(?P<r>\d+)(?:室|号)?$")
_DASH2_TAIL_RE = re.compile(r"(?P<b>\d+)-(?P<r>\d{3,})(?:室|号)?$")
_TAIL_RE = re.compile(
    r"(?P<building>(?:[A-Z]?\d+[A-Z]?|[A-Z])(?:号楼|栋|幢|座|号馆)|T\d+|\d+号(?=-?\d+单元))?"
    r"-?(?P<unit>\d+单元)?"
    r"-?(?P<floor>(?:-?\d+|B\d+|[A-Z]\d*)(?:层|F|楼(?![\d栋座])))?"
    r"-?(?P<room>[A-Z]?\d+[A-Z]?(?:室|房间|房|户|号房)?)?$"
)
_RELATIVE_RE = re.compile(r"(附近|旁边|旁|对面|斜对面|隔壁|周边|往[东西南北]\d*米?|[东西南北]侧\d*米?|\d+米处?)$")
_GATE_RE = re.compile(r"([东西南北]门|正门|侧门|后门|\d+号门)$")
_NO_ONLY_RE = re.compile(r"^(?P<no>[甲乙丙丁]?\d+(?:-\d+)?)(?P<nosuf>号院|号)$")


@dataclass
class ParsedAddress:
    raw: str
    normalized: str
    chain: AdminChain | None = None
    town_name: str | None = None
    town_code: str | None = None
    community: str | None = None
    road: str | None = None
    road_no: str | None = None
    poi: str | None = None
    building: str | None = None
    unit: str | None = None
    floor: str | None = None
    room: str | None = None
    extra: list[str] = field(default_factory=list)
    detail: str = ""
    alternatives: list[ParsedAddress] = field(default_factory=list)

    # ----------------------------------------------------------- 行政区便捷属性
    def _admin(self, level: int) -> Admin | None:
        return self.chain.get(level) if self.chain else None

    @property
    def province(self) -> Admin | None:
        return self._admin(LEVEL_PROVINCE)

    @property
    def city(self) -> Admin | None:
        return self._admin(LEVEL_CITY)

    @property
    def district(self) -> Admin | None:
        return self._admin(LEVEL_DISTRICT)

    @property
    def town(self) -> Admin | None:
        return self._admin(LEVEL_TOWN)

    @property
    def admin_score(self) -> float:
        return self.chain.score if self.chain else 0.0

    @property
    def region_code(self) -> str | None:
        """检索时用来过滤的最细行政区编码（区县优先，乡镇只做加分不做过滤）。"""
        for a in (self.district, self.city, self.province):
            if a is not None:
                return a.code
        return None

    @property
    def road_no_num(self) -> int | None:
        if not self.road_no:
            return None
        m = re.search(r"\d+", self.road_no)
        return int(m.group()) if m else None

    @property
    def finest_level(self) -> str:
        if self.room or self.unit or self.building or self.floor:
            return "building"
        return self.target_level

    @property
    def target_level(self) -> str:
        """可定位的最细层级：楼栋/单元/室挂在门牌或 POI 上，坐标只能落到它们。"""
        if self.road_no:
            return "door"
        if self.poi:
            return "poi"
        if self.road:
            return "road"
        if self.community:
            return "community"
        if self.town or self.town_name:
            return "town"
        if self.district:
            return "district"
        if self.city:
            return "city"
        if self.province:
            return "province"
        return "none"

    def admin_names(self) -> dict[str, str | None]:
        p, c, d = self.province, self.city, self.district
        city_name = c.name if c else None
        # 直辖市：市一级沿用省名（北京市北京市朝阳区 → city=北京市）
        if c is None and p is not None and p.code in ("11", "12", "31", "50") and d is not None:
            city_name = p.name
        return {
            "province": p.name if p else None,
            "city": city_name,
            "district": d.name if d else None,
            "town": self.town.name if self.town else self.town_name,
        }

    def adcodes(self) -> dict[str, str | None]:
        p, c, d = self.province, self.city, self.district
        city_code = c.adcode if c else None
        if c is None and p is not None and d is not None and p.code in ("11", "12", "31", "50"):
            city_code = d.code[:4] + "00"
        return {
            "province": p.adcode if p else None,
            "city": city_code,
            "district": d.adcode if d else None,
            "town": self.town.adcode if self.town else self.town_code,
        }

    def components(self) -> dict[str, str | None]:
        out = self.admin_names()
        out.update(community=self.community, road=self.road, road_no=self.road_no, poi=self.poi,
                   building=self.building, unit=self.unit, floor=self.floor, room=self.room)
        return out

    def formatted(self, with_detail: bool = True) -> str:
        names = self.admin_names()
        parts: list[str] = []
        for key in ("province", "city", "district", "town"):
            v = names[key]
            if v and (not parts or parts[-1] != v):
                parts.append(v)
        if with_detail:
            if self.community and not self.road:
                parts.append(self.community)
            for v in (self.road, self.road_no, self.poi):
                if v:
                    parts.append(v)
        out = "".join(parts)
        return join_parts([out, self.room_part()]) if with_detail else out

    def room_part(self) -> str:
        """楼栋/单元/楼层/室；字母数字相邻时用“-”隔开（T3-1201室）。"""
        return join_parts([self.building, self.unit, self.floor, self.room])

    def to_dict(self) -> dict:
        return {
            "normalized": self.normalized,
            "components": self.components(),
            "adcode": self.adcodes(),
            "formatted_address": self.formatted(),
            "level": self.finest_level,
            "admin_score": round(self.admin_score, 3),
            "extra": self.extra,
        }


def join_parts(parts: list[str | None]) -> str:
    out = ""
    for v in parts:
        if not v:
            continue
        if out and out[-1].isascii() and out[-1].isalnum() and v[0].isascii() and v[0].isalnum():
            out += "-"
        out += v
    return out


class AddressParser:
    def __init__(self, admin: AdminIndex, road_lexicon: set[str] | None = None):
        self.admin = admin
        self.road_lexicon = road_lexicon or set()

    def parse(self, text: str, city: str | None = None, alternatives: int = 2) -> ParsedAddress:
        norm = normalize(text)
        chains = self.admin.resolve(norm, hint=city, top=1 + alternatives)
        if not chains:
            p = self._parse_detail(text, norm, None, norm)
            return p
        main = self._from_chain(text, norm, chains[0])
        main.alternatives = [self._from_chain(text, norm, c) for c in chains[1:]]
        return main

    # ----------------------------------------------------------------- 内部
    def _from_chain(self, raw: str, norm: str, chain: AdminChain) -> ParsedAddress:
        if chain.matched:
            prefix, rest = norm[: chain.start], norm[chain.end:]
        else:  # 只有 city 提示、正文没有行政区
            prefix, rest = "", norm
        p = self._parse_detail(raw, norm, chain, rest)
        if prefix and len(prefix) >= 2:
            # 行政区前面的内容（“望京SOHO，北京市朝阳区”）当作 POI 线索
            if p.poi is None and not p.road:
                p.poi = prefix
            else:
                p.extra.append(prefix)
        return p

    def _resolve_town(self, chain: AdminChain | None, name: str) -> Admin | None:
        if chain is None:
            return None
        district = chain.get(LEVEL_DISTRICT)
        if district is None:
            return None
        for code in self.admin.children.get(district.code, ()):
            a = self.admin.by_code[code]
            if a.name == name or a.name == name + "办事处" or a.name.removesuffix("办事处") == name:
                return a
            if name.endswith("街道") and a.name == name[:-2] + "地区办事处":
                return a
        return None

    def _split_road(self, s: str, m: re.Match[str]) -> tuple[int, str]:
        """处理“POI + 路名”粘连：优先用路名词典，其次按 POI 后缀切开。返回 (路名起点, 路名)。"""
        start, road = m.start("road"), m.group("road")
        if self.road_lexicon:
            for i in range(len(road) - 1):
                if road[i:] in self.road_lexicon:
                    return start + i, road[i:]
        cut = 0
        for pm in _POI_CUT_RE.finditer(road):
            if pm.end() < len(road):
                tail = road[pm.end():]
                # 剩余部分（去掉路名后缀）至少 2 个字才切
                core = re.sub(rf"(?:{_ROAD_SUFFIX})(?:[东西南北中]段|\d+段)?(?:辅路)?$", "", tail)
                if len(core) >= 2:
                    cut = pm.end()
        return start + cut, road[cut:]

    def _parse_detail(self, raw: str, norm: str, chain: AdminChain | None, rest: str) -> ParsedAddress:
        p = ParsedAddress(raw=raw, normalized=norm, chain=chain)
        s = normalize_numbers(rest)
        p.detail = s

        # 尾部的相对位置描述（“附近”“对面”“东侧 50 米”）不参与匹配
        while True:
            m = _RELATIVE_RE.search(s)
            if not m or m.start() == 0:
                break
            p.extra.insert(0, m.group())
            s = s[: m.start()]

        # 1. 乡镇街道（若行政区链里已有则跳过）
        m = _TOWN_RE.match(s)
        if m and (chain is None or chain.get(LEVEL_TOWN) is None):
            town = self._resolve_town(chain, m.group("town"))
            if town is not None:
                chain.levels[LEVEL_TOWN] = town
            else:
                p.town_name = m.group("town")
            s = s[m.end():]

        # 2. 社区 / 村
        m = _COMMUNITY_RE.match(s)
        if m and self._accept_community(p, chain, m.group("community"), s[m.end():]):
            p.community = m.group("community")
            s = s[m.end():]

        # 3. 楼栋单元室（从尾部剥离）
        s = self._parse_tail(p, s)

        # 4. 道路 + 门牌
        m = self._find_road(p, s)
        pre = ""
        if m:
            rstart, road = self._split_road(s, m)
            pre = s[:rstart]
            p.road = road
            s = s[m.end():]
            cm = _CROSS_RE.match(s)
            if cm:
                p.extra.append("与" + cm.group("road2") + "交叉口")
                s = s[cm.end():]
            nm = _NO_RE.match(s)
            if nm and (nm.group("lane") or nm.group("no")):
                if nm.group("lane"):
                    p.road_no = nm.group("lane") + "弄"
                    if nm.group("no") and not p.building:
                        p.building = nm.group("no") + "号"
                else:
                    p.road_no = nm.group("no") + nm.group("nosuf")
                s = s[nm.end():]
            elif _BARE_NO_RE.match(s):
                p.road_no = s + "号"
                s = ""
        else:
            nm = _NO_ONLY_RE.match(s)
            if nm:  # 村里的“15 号”这类没有路名的门牌
                p.road_no = s
                s = ""

        # 5. POI：道路之后的剩余文本；道路之前的片区名作为备选
        poi = s.strip("-的")
        gm = _GATE_RE.search(poi)
        if gm and gm.start() >= 2:
            p.extra.append(gm.group())
            poi = poi[: gm.start()]
        pre = pre.strip("-的")
        if poi:
            p.poi = poi
            if pre:
                p.extra.append(pre)
        elif pre:
            p.poi = pre
        return p

    @staticmethod
    def _accept_community(p: ParsedAddress, chain: AdminChain | None, name: str, after: str) -> bool:
        if re.search(rf"(?:{_ROAD_SUFFIX})|\d+号", name):
            return False
        if not re.search(r"村(?:\d+组)?$", name):
            return True  # 社区/居委会/村委会/嘎查
        # 城区里的“XX村”多是地名（中关村、西二旗村），只有乡镇或县域上下文才当行政村
        has_town = p.town_name is not None or (chain is not None and chain.get(LEVEL_TOWN) is not None)
        district = chain.get(LEVEL_DISTRICT) if chain else None
        rural = district is not None and district.name.endswith(("县", "旗"))
        return has_town or rural or not after or after[0].isdigit()

    def _find_road(self, p: ParsedAddress, s: str) -> re.Match[str] | None:
        pos = 0
        while pos < len(s):
            m = _ROAD_RE.search(s, pos)
            if m is None:
                return None
            road = m.group("road")
            if road.endswith("街道") or road.endswith("街道办事处"):
                # 夹在中间的“XX街道”是乡镇级，不是路
                if p.town_name is None and (p.chain is None or p.chain.get(LEVEL_TOWN) is None):
                    p.town_name = road
                pos = m.end()
                continue
            return m
        return None

    def _parse_tail(self, p: ParsedAddress, s: str) -> str:
        m = _DASH_TAIL_RE.search(s)
        if m and m.start() > 0:
            p.building, p.unit, p.room = m.group("b") + "号楼", m.group("u") + "单元", m.group("r") + "室"
            return s[: m.start()].rstrip("-")
        m = _TAIL_RE.search(s)
        if m and m.start() < len(s):
            head = s[: m.start()]
            room = m.group("room")
            # 只有一个裸数字时，它更可能是门牌（“XX路100”），留给道路解析
            only_room = room and not any(m.group(k) for k in ("building", "unit", "floor"))
            if only_room and (not head or re.search(rf"(?:{_ROAD_SUFFIX})$", head) or room.isdigit() and len(room) < 3):
                return s
            if m.group("building"):
                b = m.group("building")
                p.building = b[:-1] + "号楼" if b.endswith("号") else b
            p.unit = m.group("unit")
            p.floor = m.group("floor")
            if room:
                p.room = room
            return head.rstrip("-")
        m = _DASH2_TAIL_RE.search(s)
        if m and m.start() > 0 and not re.search(rf"(?:{_ROAD_SUFFIX})$", s[: m.start()]):
            p.building, p.room = m.group("b") + "号楼", m.group("r") + "室"
            return s[: m.start()].rstrip("-")
        return s
