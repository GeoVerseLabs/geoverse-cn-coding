"""中文 n-gram 切分与相似度。

SQLite FTS5 自带分词器不切中文，这里在入库和查询两侧都把中文串切成二元组（bigram），
英文数字按整词处理，再交给 FTS5 的 unicode61 分词器按空格建倒排。
相比 IK 词典分词，bigram 不依赖词典、不怕新词和简称，召回稳定，排序交给后面的重打分。
"""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]+|[A-Za-z0-9]+")


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF


def ngrams(text: str) -> list[str]:
    out: list[str] = []
    if not text:
        return out
    for m in _TOKEN_RE.finditer(text):
        t = m.group()
        if _is_cjk(t[0]):
            if len(t) == 1:
                out.append(t)
            else:
                out.extend(t[i:i + 2] for i in range(len(t) - 1))
        else:
            out.append(t.lower())
    return out


def index_text(*parts: str | None) -> str:
    """生成写入 FTS5 的文本（空格分隔的 token）。"""
    toks: list[str] = []
    for p in parts:
        if p:
            toks.extend(ngrams(p))
    return " ".join(toks)


def match_expr(text: str, max_terms: int = 24, drop: frozenset[str] | set[str] = frozenset()) -> str | None:
    """生成 FTS5 MATCH 表达式：token 之间取 OR，排序交给 bm25 + 重打分。

    drop 是高频 token（“大厦”“小区”等），剔除后能显著缩短倒排扫描；若全部被剔除则保留原样。
    """
    seen: list[str] = []
    for t in ngrams(text):
        if t not in seen:
            seen.append(t)
        if len(seen) >= max_terms:
            break
    if not seen:
        return None
    if drop:
        kept = [t for t in seen if t not in drop]
        seen = kept or seen
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in seen)


def dice(a: str, b: str) -> float:
    """bigram Dice 相似度，取值 0~1。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ga, gb = ngrams(a), ngrams(b)
    if not ga or not gb:
        return 0.0
    sb = list(gb)
    inter = 0
    for g in ga:
        if g in sb:
            sb.remove(g)
            inter += 1
    return 2.0 * inter / (len(ga) + len(gb))


def is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def name_similarity(query: str, name: str) -> float:
    """POI/道路名相似度：Dice 为主，包含关系、按序省略（腾讯大厦 ⊂ 腾讯滨海大厦）给加分。"""
    if not query or not name:
        return 0.0
    q, n = query.upper(), name.upper()
    if q == n:
        return 1.0
    s = dice(q, n)
    if q in n or n in q:
        shorter, longer = (q, n) if len(q) <= len(n) else (n, q)
        s = max(s, 0.6 + 0.35 * len(shorter) / len(longer))
    elif len(q) >= 4 and len(q) < len(n) and is_subsequence(q, n):
        # 口语里常省略中间的字；只认 4 字以上、全部按序出现的情况（“北京南站”不会匹配“北京西站”）
        s = max(s, 0.45 + 0.3 * len(q) / len(n))
    return min(s, 0.99)
