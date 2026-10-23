"""从 yescallop/areacodes（CC0）生成区划补充数据。

数据集收录 1981–2025 年县级以上行政区划代码及新旧代码对应关系，来源为国家地名信息库与民政部发布。
本脚本产出两份文件（写入 data/admin/）：

- extra.csv：现行（2025）县级以上区划里、内置统计用区划代码 2023 版没有的条目，外加港澳台省级；
- history_aliases.csv：已撤销或更名的历史名称 → 现行区划编码（如 崇文区 → 110101），用于解析老地址。

用法：python scripts/sources/areacodes.py [--csv 本地 result.csv] [--admin-dir data/admin]
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import urllib.request
from collections import defaultdict

URL = "https://raw.githubusercontent.com/yescallop/areacodes/master/result.csv"
LEVELS = {"省级": 1, "地级": 2, "县级": 3}
PLACEHOLDER = {"市辖区", "县", "省直辖县级行政区划", "自治区直辖县级行政区划"}
# 历史上大量重复、单独出现时无法判断指向的名称
GENERIC = {"郊区", "城区", "矿区", "市中区", "新区", "开发区", "城关区", "城关镇", "市区"}
EXTRA_HEADER = ("# 内置统计用区划代码（2023）之外的现行区划条目。\n"
                "# 来源：yescallop/areacodes（CC0 1.0，数据取自国家地名信息库 2025 年行政区划代码、民政部县级以上行政区划代码）；\n"
                "# 港澳台省级代码依 GB/T 2260。由 scripts/sources/areacodes.py 生成，请勿手改。\n")
HIST_HEADER = ("# 历史区划名称 → 现行区划编码，用于解析使用旧名的地址。\n"
               "# 来源：yescallop/areacodes（CC0 1.0）的新旧代码对应关系，原始数据为民政部历年县级以上行政区划代码与变更情况。\n"
               "# 由 scripts/sources/areacodes.py 生成，请勿手改；人工维护的别名写在 aliases.csv。\n")


def native(code6: str, level: int) -> str:
    return {1: code6[:2], 2: code6[:4], 3: code6}[level]


def load_records(text: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    out = []
    for r in rows:
        out.append({
            "code": r["代码"], "name": r["名称"].strip(), "level": LEVELS[r["级别"]], "status": r["状态"],
            "start": int(r["启用时间"]), "end": int(r["变更/弃用时间"]) if r["变更/弃用时间"] else None,
            "new": r["新代码"].strip(), "province": r["一级行政区"], "parent_name": r["二级行政区"],
        })
    return out


def resolve(rec: dict, by_code: dict[str, list[dict]], depth: int = 0) -> list[str]:
    """沿“变更/弃用 → 新代码”链条找到现行编码（同一代码可能在不同年份指代不同区域，按年份取记录）。"""
    if depth > 12:
        return []
    if rec["end"] is None:
        return [rec["code"]]
    year = rec["end"]
    if rec["status"] == "变更":
        nxt = _at(by_code, rec["code"], year)
        return resolve(nxt, by_code, depth + 1) if nxt and nxt is not rec else []
    out: list[str] = []
    for part in filter(None, rec["new"].split(";")):
        m = re.match(r"(\d{6})(?:\[(\d{4})\])?", part)
        if not m:
            continue
        nxt = _at(by_code, m.group(1), int(m.group(2) or year))
        if nxt is not None:
            for c in resolve(nxt, by_code, depth + 1):
                if c not in out:
                    out.append(c)
    return out


def _at(by_code: dict[str, list[dict]], code: str, year: int) -> dict | None:
    recs = by_code.get(code, [])
    for r in recs:
        if r["start"] <= year and (r["end"] is None or year < r["end"]):
            return r
    later = [r for r in recs if r["start"] >= year]
    return later[0] if later else (recs[-1] if recs else None)


def load_dictionary(admin_dir: str) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for fname, level in (("provinces.csv", 1), ("cities.csv", 2), ("areas.csv", 3)):
        with open(os.path.join(admin_dir, fname), encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                out[r["code"]] = (r["name"], level)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--csv", help="本地 result.csv（默认从 GitHub 下载）")
    ap.add_argument("--admin-dir", default="data/admin")
    args = ap.parse_args()

    if args.csv:
        text = open(args.csv, encoding="utf-8-sig").read()
    else:
        with urllib.request.urlopen(URL, timeout=60) as resp:
            text = resp.read().decode("utf-8")
    recs = load_records(text)
    by_code: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by_code[r["code"]].append(r)
    for lst in by_code.values():
        lst.sort(key=lambda r: r["start"])

    current = [r for r in recs if r["end"] is None]
    dictionary = load_dictionary(args.admin_dir)

    # 1. 现行条目中内置词典没有的（新设区县、三沙市辖区等）
    extra = [("71", "台湾省", 1, "", "GB/T 2260"), ("81", "香港特别行政区", 1, "", "GB/T 2260"),
             ("82", "澳门特别行政区", 1, "", "GB/T 2260")]
    for r in sorted(current, key=lambda r: r["code"]):
        code = native(r["code"], r["level"])
        if code in dictionary or code in {e[0] for e in extra}:
            continue
        parent = {1: "", 2: r["code"][:2], 3: r["code"][:4]}[r["level"]]
        # 直辖市、省直辖县的上级在统计用代码里是“市辖区/县/省直辖…”占位节点，编码前四位即可对应
        extra.append((code, r["name"], r["level"], parent, f"国家地名信息库 {r['start']} 年起"))
    with open(os.path.join(args.admin_dir, "extra.csv"), "w", encoding="utf-8", newline="") as f:
        f.write(EXTRA_HEADER)
        w = csv.writer(f)
        w.writerow(["code", "name", "level", "parent", "source"])
        w.writerows(extra)

    # 2. 历史名称别名
    current_names = {r["name"] for r in current} | {n for n, _ in dictionary.values()}
    valid = set(dictionary) | {e[0] for e in extra}
    cand: dict[str, dict[str, tuple[str, int]]] = defaultdict(dict)
    for r in recs:
        if r["end"] is None or r["name"] in current_names or r["name"] in GENERIC or len(r["name"]) <= 2:
            continue
        targets = [native(c, _level_of(c)) for c in resolve(r, by_code)]
        targets = [c for c in targets if c in valid]
        if not targets:
            continue
        # 拆分时取第一个继承者（通常是原驻地所在区域）
        cand[r["name"]].setdefault(targets[0], (r["code"], r["end"]))
    rows = []
    for name, targets in sorted(cand.items()):
        if len(targets) > 3:  # 指向过多，单独出现时没有区分度
            continue
        for code, (old, year) in sorted(targets.items()):
            rows.append((name, code, old, year))
    with open(os.path.join(args.admin_dir, "history_aliases.csv"), "w", encoding="utf-8", newline="") as f:
        f.write(HIST_HEADER)
        w = csv.writer(f)
        w.writerow(["alias", "code", "old_code", "changed_year"])
        w.writerows(rows)
    print(f"extra.csv: {len(extra)} 条；history_aliases.csv: {len(rows)} 条（{len(cand)} 个历史名称）")
    return 0


def _level_of(code6: str) -> int:
    if code6[2:] == "0000":
        return 1
    if code6[4:] == "00":
        return 2
    return 3


if __name__ == "__main__":
    sys.exit(main())
