"""行政区划词典与层级消歧。

中国地址的第一段几乎总是行政区（省 → 市 → 区县 → 乡镇街道），但写法很随意：
可能省略任意层级（“海淀区中关村”）、用简称（“广东深圳南山”）、用旧名（“襄樊”“崇文区”），
还存在大量同名区县（全国有 3 个“朝阳”、几十个“城关镇”）。

做法：
1. 用全称、简称、别名构建 Aho-Corasick 自动机，一次扫描拿到所有候选命中；
2. 用动态规划在候选里找一条“位置递增、层级递增、编码前缀一致”的最优链；
3. 链只命中下级时，靠区划编码前缀把上级补全；同分时用“直辖市/省会/计划单列市”先验打破平局，
   并把分数接近的其他解释一并返回，交给检索阶段用数据再判一次。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

try:  # C 实现的 AC 自动机，没有时退回纯 Python
    import ahocorasick  # type: ignore
except ImportError:  # pragma: no cover
    ahocorasick = None

LEVEL_PROVINCE, LEVEL_CITY, LEVEL_DISTRICT, LEVEL_TOWN = 1, 2, 3, 4
LEVEL_NAMES = {1: "province", 2: "city", 3: "district", 4: "town"}
CODE_LEN = {1: 2, 2: 4, 3: 6, 4: 9}

PLACEHOLDER_NAMES = {"市辖区", "县", "省直辖县级行政区划", "自治区直辖县级行政区划"}
MUNICIPALITIES = {"11", "12", "31", "50"}
# 计划单列市和部分特大城市；省会城市编码都以 01 结尾，单独判断
MAJOR_CITIES = {"2102", "3302", "3502", "3702", "4403", "3205", "3202", "4406", "4419"}

ETHNIC_GROUPS = sorted([
    "蒙古", "回", "藏", "维吾尔", "苗", "彝", "壮", "布依", "朝鲜", "满", "侗", "瑶", "白", "土家",
    "哈尼", "哈萨克", "傣", "黎", "傈僳", "佤", "畲", "高山", "拉祜", "水", "东乡", "纳西", "景颇",
    "柯尔克孜", "土", "达斡尔", "仫佬", "羌", "布朗", "撒拉", "毛南", "仡佬", "锡伯", "阿昌", "普米",
    "塔吉克", "怒", "乌孜别克", "俄罗斯", "鄂温克", "德昂", "保安", "裕固", "京", "塔塔尔", "独龙",
    "鄂伦春", "赫哲", "门巴", "珞巴", "基诺", "各",
], key=len, reverse=True)

_SUFFIXES = {
    LEVEL_PROVINCE: ("维吾尔自治区", "壮族自治区", "回族自治区", "自治区", "特别行政区", "省", "市"),
    LEVEL_CITY: ("地区", "盟", "市"),
    LEVEL_DISTRICT: ("新区", "特区", "区", "县", "市", "旗"),
}

# 简称后面紧跟这些字时，多半是路名/地名的一部分（“中山路”“朝阳北路”“玄武湖”），不当行政区
_SHORT_BLOCK_NEXT = set("路街道巷弄胡同桥门里村屯港湾园苑庭府居楼厦店场站口坊寺庙塔湖江河东西南北大")

# alias：人工维护的别名；hist：由历史区划变更自动生成的旧名；nick：城市口语别称（魔都、羊城）
KIND_WEIGHT = {"full": 1.0, "alias": 0.9, "hist": 0.7, "short": 0.75, "nick": 0.75, "gen": 0.6}
# 这些写法只在地址开头或紧跟其他行政区时才算行政区，且后面紧跟路名用字时不算
_CONTIGUOUS_KINDS = ("short", "nick", "gen")
_CORRECT_SUFFIXES = ("自治县", "自治州", "新区", "区", "县", "市", "旗", "州", "盟")


@dataclass(slots=True)
class Admin:
    code: str  # 原生编码：省 2 位、市 4 位、区县 6 位、乡镇 9 位
    name: str
    level: int
    parent: str | None = None
    lng: float | None = None
    lat: float | None = None

    @property
    def virtual(self) -> bool:
        return self.name in PLACEHOLDER_NAMES

    @property
    def adcode(self) -> str:
        """对外编码：省市区补齐 6 位（110000/110100/110105），乡镇 9 位。"""
        return self.code if self.level == LEVEL_TOWN else self.code.ljust(6, "0")


def native_code(code: str | int) -> str:
    """把 6/9/12 位编码转换为原生层级编码（去掉补零）。"""
    c = str(code).strip()
    if not c.isdigit():
        raise ValueError(f"非法区划编码: {code}")
    if len(c) > 6 and c[6:9].strip("0"):
        return c[:9]
    c = c[:6].ljust(6, "0")
    if c[4:6] != "00":
        return c
    if c[2:4] != "00":
        return c[:4]
    return c[:2]


def strip_autonomous(name: str) -> str:
    """'黔东南苗族侗族自治州' → '黔东南'；'长阳土家族自治县' → '长阳'。"""
    for suf in ("自治州", "自治县", "自治旗"):
        if name.endswith(suf):
            n = name[: -len(suf)]
            changed = True
            while changed:
                changed = False
                for eth in ETHNIC_GROUPS:
                    if n.endswith(eth + "族") and len(n) > len(eth) + 1:
                        n = n[: -(len(eth) + 1)]
                        changed = True
                        break
            return n
    return name


def short_names(name: str, level: int) -> list[str]:
    out: list[str] = []
    if level in (LEVEL_CITY, LEVEL_DISTRICT):
        n = strip_autonomous(name)
        if n != name and len(n) >= 2:
            out.append(n)
    for suf in _SUFFIXES.get(level, ()):
        if name.endswith(suf) and len(name) - len(suf) >= 2:
            out.append(name[: -len(suf)])
            break
    if level == LEVEL_TOWN and name.endswith("办事处"):
        out.append(name[:-3])  # 漓东街道办事处 → 漓东街道
    return [o for o in dict.fromkeys(out) if o != name]


@dataclass(slots=True)
class Match:
    start: int
    end: int
    form: str
    code: str
    level: int
    kind: str

    @property
    def weight(self) -> float:
        return KIND_WEIGHT[self.kind]


@dataclass
class Correction:
    """行政区名称纠错：text[start:end] 的 wrong 改为 admin.name。weak=没有上级约束、靠先验选出。"""

    start: int
    end: int
    wrong: str
    admin: Admin
    weak: bool = False


def _one_char_off(a: str, b: str, keep_tail: int) -> bool:
    """等长、只差一个字，且差异不在后缀（区/县/市…）上。"""
    if len(a) != len(b) or a == b:
        return False
    diff = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
    return len(diff) == 1 and diff[0] < len(a) - keep_tail


@dataclass
class AdminChain:
    """一种行政区解释。levels 只包含实体层级（不含“市辖区”等占位）。"""

    levels: dict[int, Admin]
    score: float
    start: int = 0
    end: int = 0
    matched: list[Match] = field(default_factory=list)

    @property
    def finest(self) -> Admin | None:
        return self.levels[max(self.levels)] if self.levels else None

    def get(self, level: int) -> Admin | None:
        return self.levels.get(level)

    @property
    def district_code(self) -> str | None:
        a = self.levels.get(LEVEL_DISTRICT)
        return a.code if a else None


class _PyMatcher:
    """pyahocorasick 不可用时的退路：按长度从长到短查字典。"""

    def __init__(self, forms: Iterable[str]):
        self.forms = set(forms)
        self.lens = sorted({len(f) for f in self.forms}, reverse=True)

    def iter(self, text: str):
        n = len(text)
        for i in range(n):
            for ln in self.lens:
                if i + ln <= n and text[i:i + ln] in self.forms:
                    yield i + ln - 1, text[i:i + ln]


class AdminIndex:
    def __init__(self, admins: Iterable[Admin], aliases: Iterable[tuple[str, str] | tuple[str, str, str]] = ()):
        self.by_code: dict[str, Admin] = {}
        self.children: dict[str, list[str]] = defaultdict(list)
        for a in admins:
            self.by_code[a.code] = a
        for a in self.by_code.values():
            if a.parent:
                self.children[a.parent].append(a.code)

        forms: dict[str, list[tuple[str, str]]] = defaultdict(list)
        full_names = set()
        for a in self.by_code.values():
            if a.virtual:
                continue
            forms[a.name].append((a.code, "full"))
            full_names.add(a.name)
        for a in self.by_code.values():
            if a.virtual:
                continue
            for s in short_names(a.name, a.level):
                forms[s].append((a.code, "short"))
            # 县改区、县级市改区之后旧称仍然常见：密云县 → 密云区
            if a.level == LEVEL_DISTRICT and a.name.endswith("区") and not a.name.endswith("新区"):
                stem = a.name[:-1]
                if len(stem) >= 2:
                    for suf in ("县", "市"):
                        old = stem + suf
                        if old not in full_names:
                            forms[old].append((a.code, "gen"))
        for item in aliases:
            alias, code = item[0], item[1]
            kind = item[2] if len(item) > 2 and item[2] in KIND_WEIGHT else "alias"
            c = native_code(code)
            if c in self.by_code and alias and alias not in full_names:
                forms[alias].append((c, kind))
        # 同一个 form 下同一 code 只保留权重最高的一种
        self.forms: dict[str, list[tuple[str, str]]] = {}
        for f, lst in forms.items():
            best: dict[str, str] = {}
            for code, kind in lst:
                if code not in best or KIND_WEIGHT[kind] > KIND_WEIGHT[best[code]]:
                    best[code] = kind
            self.forms[f] = list(best.items())

        # 纠错索引：(长度, 末字) → 地级/县级区划
        self._by_len_tail: dict[tuple[int, str], list[Admin]] = defaultdict(list)
        for a in self.by_code.values():
            if a.level in (LEVEL_CITY, LEVEL_DISTRICT) and not a.virtual:
                self._by_len_tail[(len(a.name), a.name[-1])].append(a)

        if ahocorasick is not None:
            self._ac = ahocorasick.Automaton()
            for f in self.forms:
                self._ac.add_word(f, f)
            self._ac.make_automaton()
        else:  # pragma: no cover
            self._ac = _PyMatcher(self.forms)

    # ------------------------------------------------------------------ 查询
    def get(self, code: str | None) -> Admin | None:
        if not code:
            return None
        return self.by_code.get(code) or self.by_code.get(native_code(code))

    def lineage(self, code: str) -> dict[int, Admin]:
        """按编码前缀补全上级，跳过“市辖区”之类占位节点。"""
        out: dict[int, Admin] = {}
        for level, ln in CODE_LEN.items():
            if len(code) < ln:
                break
            a = self.by_code.get(code[:ln])
            if a is not None and not a.virtual:
                out[level] = a
        # 不设区的地级市（东莞、中山、儋州、嘉峪关）：市下面只有一个同名“区县”，补齐到区县级
        city = out.get(LEVEL_CITY)
        if city is not None and LEVEL_DISTRICT not in out:
            kids = [self.by_code[c] for c in self.children.get(city.code, ())]
            if len(kids) == 1 and kids[0].name == city.name:
                out[LEVEL_DISTRICT] = kids[0]
        return out

    def chain_for(self, code: str, score: float = 1.0) -> AdminChain:
        return AdminChain(levels=self.lineage(native_code(code)), score=score)

    def descendants(self, code: str, depth: int = 2) -> list[Admin]:
        """下级区划（穿过“市辖区”等占位节点，占位节点不计层数）。"""
        out: list[Admin] = []
        frontier = [(code, 0)]
        while frontier:
            c, d = frontier.pop()
            for k in self.children.get(c, ()):
                a = self.by_code[k]
                if a.virtual:
                    frontier.append((k, d))
                elif d + 1 <= depth:
                    out.append(a)
                    frontier.append((k, d + 1))
        return out

    def correct(self, text: str, chain: AdminChain | None) -> Correction | None:
        """行政区错别字纠正（“北京市海定区”→海淀区，“江苏省苏洲市”→苏州市）。

        只纠地级、县级名称（这两级名单完整、稳定），且要求：紧跟在已识别的行政区之后（或在开头）、
        以区/县/市等后缀结尾、与候选只差一个非后缀字。有上级时只在其下级里找；没有上级时全国唯一才纠，
        多个候选时只接受唯一一个有“直辖市/省会”先验的，并标记为弱纠错。
        """
        matched = chain is not None and chain.matched
        parent = chain.finest if matched else None
        if parent is not None and parent.level >= LEVEL_DISTRICT:
            return None
        pos = chain.end if matched else 0
        rest = text[pos:]
        pool_parent = self.descendants(parent.code) if parent is not None else None
        for ln in range(3, 9):
            tok = rest[:ln]
            if len(tok) < ln:
                break
            suf = next((s for s in _CORRECT_SUFFIXES if tok.endswith(s)), None)
            if suf is None or any(ch.isdigit() for ch in tok):
                continue
            if tok in self.forms:
                return None  # 本身就是合法名称
            pool = pool_parent if pool_parent is not None else self._by_len_tail.get((ln, tok[-1]), [])
            cands = [a for a in pool if a.name.endswith(suf) and _one_char_off(a.name, tok, len(suf))]
            if len(cands) == 1:
                return Correction(pos, pos + ln, tok, cands[0], weak=parent is None)
            if len(cands) > 1 and parent is None:
                preferred = [a for a in cands if self._prior(a.code) > 0]
                if len(preferred) == 1:
                    return Correction(pos, pos + ln, tok, preferred[0], weak=True)
        return None

    def lookup_name(self, name: str) -> list[Admin]:
        return [self.by_code[c] for c, _ in self.forms.get(name, [])]

    def resolve_hint(self, hint: str | None) -> list[str]:
        """city 参数：既可以是区划编码，也可以是名称/简称。"""
        if not hint:
            return []
        h = hint.strip()
        if h.isdigit():
            try:
                c = native_code(h)
            except ValueError:
                return []
            return [c] if c in self.by_code else []
        return [a.code for a in self.lookup_name(h)]

    def find_matches(self, text: str) -> list[Match]:
        out: list[Match] = []
        n = len(text)
        hits = [(end_idx - len(form) + 1, end_idx + 1, form) for end_idx, form in self._ac.iter(text)]
        starts = {h[0] for h in hits}
        for start, end, form in hits:
            nxt = text[end] if end < n else ""
            # 简称后面紧跟另一个行政区（“深圳南山”）时不受后续字限制
            blocked = nxt in _SHORT_BLOCK_NEXT and end not in starts
            for code, kind in self.forms[form]:
                if kind in ("short", "nick") and blocked:
                    continue
                a = self.by_code[code]
                out.append(Match(start, end, form, code, a.level, kind))
        out.sort(key=lambda m: (m.start, -m.end))
        return out

    def _prior(self, code: str) -> float:
        p = code[:2]
        c = code[:4]
        if p in MUNICIPALITIES:
            return 0.03
        if len(code) >= 4 and (c.endswith("01") or c in MAJOR_CITIES):
            return 0.02
        return 0.0

    def resolve(self, text: str, hint: str | None = None, top: int = 3) -> list[AdminChain]:
        """返回按分数排序的若干行政区解释（最多 top 个，终点编码互不相同）。"""
        matches = self.find_matches(text)
        hint_codes = self.resolve_hint(hint)
        n = len(matches)
        best: list[tuple[float, int]] = [(float("-inf"), -1)] * n
        for i, m in enumerate(matches):
            # 作为链首：简称/推导别名必须在开头；全称允许前面有少量噪声
            if m.kind in _CONTIGUOUS_KINDS:
                head = m.weight if m.start == 0 else float("-inf")
            else:
                head = m.weight - min(0.6, 0.04 * m.start)
            cand = (head, -1)
            for j in range(i):
                pj = matches[j]
                if pj.end > m.start or pj.level >= m.level or not m.code.startswith(pj.code):
                    continue
                if best[j][0] == float("-inf"):
                    continue
                gap = m.start - pj.end
                if gap > 0 and m.kind in _CONTIGUOUS_KINDS:
                    continue
                s = best[j][0] + m.weight - min(0.8, 0.08 * gap)
                if s > cand[0]:
                    cand = (s, j)
            best[i] = cand

        chains: list[AdminChain] = []
        for i in range(n):
            if best[i][0] == float("-inf"):
                continue
            path: list[Match] = []
            k = i
            while k != -1:
                path.append(matches[k])
                k = best[k][1]
            path.reverse()
            final = path[-1].code
            score = best[i][0] + self._prior(final)
            if hint_codes:
                if any(final.startswith(h) or h.startswith(final) for h in hint_codes):
                    score += 0.5
                else:
                    score -= 0.5
            chains.append(AdminChain(levels=self.lineage(final), score=score,
                                     start=path[0].start, end=path[-1].end, matched=path))

        if not chains and hint_codes:
            ch = self.chain_for(hint_codes[0], score=0.5)
            return [ch]

        # 同分时更细的层级优先、编码短（上级）的排后面
        chains.sort(key=lambda c: (c.score, max(c.levels) if c.levels else 0, c.end), reverse=True)
        out: list[AdminChain] = []
        seen: set[str] = set()
        for c in chains:
            f = c.finest
            if f is None or f.code in seen:
                continue
            if out and c.score < out[0].score - 0.35:
                break
            seen.add(f.code)
            out.append(c)
            if len(out) >= top:
                break
        return out


def load_admin_csvs(admin_dir: str) -> list[Admin]:
    """读取 modood/Administrative-divisions-of-China 格式的 CSV（provinces/cities/areas/streets）。"""
    import csv
    import os

    specs = [("provinces.csv", LEVEL_PROVINCE, None), ("cities.csv", LEVEL_CITY, "provinceCode"),
             ("areas.csv", LEVEL_DISTRICT, "cityCode"), ("streets.csv", LEVEL_TOWN, "areaCode")]
    out: list[Admin] = []
    names: dict[str, str] = {}
    for fname, level, parent_col in specs:
        path = os.path.join(admin_dir, fname)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                code, name = row["code"].strip(), row["name"].strip()
                parent = row[parent_col].strip() if parent_col else None
                pname = names.get(parent or "", "")
                # 统计用区划里少数乡镇名带了区县前缀（“济源市沁园街道”），去掉
                if level == LEVEL_TOWN and pname and name.startswith(pname):
                    rest = name[len(pname):]
                    if len(rest) >= 3 and _TOWN_SUFFIX_RE.search(rest):
                        name = rest
                names[code] = name
                out.append(Admin(code=code, name=name, level=level, parent=parent))
    extra = os.path.join(admin_dir, "extra.csv")
    if os.path.exists(extra):
        seen = {a.code for a in out}
        with open(extra, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(line for line in f if not _ALIAS_LINE.match(line)):
                code = row["code"].strip()
                if code in seen:
                    continue
                out.append(Admin(code=code, name=row["name"].strip(), level=int(row["level"]),
                                 parent=(row.get("parent") or "").strip() or None))
    return out


_ALIAS_LINE = re.compile(r"^\s*#")
_TOWN_SUFFIX_RE = re.compile(r"(街道|镇|乡|街道办事处|苏木)$")


def load_alias_csv(path: str, kind: str = "alias") -> list[tuple[str, str, str]]:
    import csv

    out: list[tuple[str, str, str]] = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = (line for line in f if not _ALIAS_LINE.match(line))
        for row in csv.DictReader(rows):
            if row.get("alias") and row.get("code"):
                out.append((row["alias"].strip(), row["code"].strip(), kind))
    return out


def load_admin_aliases(admin_dir: str) -> list[tuple[str, str, str]]:
    """区划目录下的人工别名（aliases.csv）和历史旧名（history_aliases.csv）。"""
    import os

    out: list[tuple[str, str, str]] = []
    for fname, kind in (("aliases.csv", "alias"), ("history_aliases.csv", "hist"), ("nicknames.csv", "nick")):
        path = os.path.join(admin_dir, fname)
        if os.path.exists(path):
            out.extend(load_alias_csv(path, kind))
    return out
