"""地址文本归一化。

只做与语义无关的清洗：全角转半角、去噪声（电话、标签）、统一分隔符。
中文数字转阿拉伯数字只作用在行政区之后的“详细地址”部分（见 ``normalize_numbers``），
否则会把“二七区”“六安市”“十堰市”这类地名改坏。
"""

from __future__ import annotations

import re
import unicodedata

CN_DIGITS = {
    "零": 0, "〇": 0, "○": 0, "一": 1, "壹": 1, "幺": 1, "二": 2, "贰": 2, "两": 2,
    "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6,
    "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9,
}
CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_NUM_CHARS = "".join(CN_DIGITS) + "".join(CN_UNITS)

# 只在这些量词前做中文数字转换
_NUM_SUFFIX = "号楼|号院|号馆|号|栋|幢|座|单元|层|楼|室|弄|巷|期|段|组|排|队|区(?=\\d*号楼)"
_CN_NUM_RE = re.compile(rf"第?([{_CN_NUM_CHARS}]+)(?=(?:{_NUM_SUFFIX}))")

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86-?)?1[3-9]\d{9}(?!\d)|(?<!\d)0\d{2,3}-?\d{7,8}(?!\d)")
_LABEL_RE = re.compile(r"(收货人|收件人|联系人|联系电话|电话|手机|手机号|邮编|详细地址|地址)[:：]")
_POSTCODE_RE = re.compile(r"邮编[:：]?\d{6}")
_SPACE_BETWEEN_ALNUM = re.compile(r"(?<=[A-Za-z0-9])\s+(?=[A-Za-z0-9])")
_DASHES = str.maketrans({"—": "-", "–": "-", "－": "-", "‐": "-", "‑": "-", "~": "-", "～": "-", "#": "号"})
_DROP_RE = re.compile(r"[\s,，.。;；:：、!！?？\"'“”‘’`《》<>()（）\[\]【】{}|/\\·*]+")
_MULTI_DASH = re.compile(r"-{2,}")


def cn_to_int(s: str) -> int | None:
    """中文数字转整数：'十八'→18，'一百零一'→101，'一零一'→101。"""
    if not s:
        return None
    if all(c in CN_DIGITS for c in s):
        return int("".join(str(CN_DIGITS[c]) for c in s))
    total, cur = 0, 0
    for c in s:
        if c in CN_DIGITS:
            cur = CN_DIGITS[c]
        elif c in CN_UNITS:
            total += (cur or 1) * CN_UNITS[c]
            cur = 0
        else:
            return None
    return total + cur


def normalize(text: str) -> str:
    """清洗原始地址串，返回无空白、半角化的文本。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    s = _POSTCODE_RE.sub("", s)
    s = _PHONE_RE.sub(" ", s)
    s = _LABEL_RE.sub(" ", s)
    s = s.translate(_DASHES)
    # “T1 1201” 这类字母数字之间的空格改成连字符，避免直接拼接成 T11201
    s = _SPACE_BETWEEN_ALNUM.sub("-", s.strip())
    s = _DROP_RE.sub("", s)
    s = _MULTI_DASH.sub("-", s).strip("-")
    return s.upper()


def normalize_numbers(detail: str) -> str:
    """把详细地址里量词前的中文数字转成阿拉伯数字：'三号楼二单元' → '3号楼2单元'。"""

    def repl(m: re.Match[str]) -> str:
        n = cn_to_int(m.group(1))
        return str(n) if n is not None else m.group(0)

    return _CN_NUM_RE.sub(repl, detail)


def strip_tail_label(name: str) -> str:
    """去掉 POI 名称尾部常见的连接词。"""
    return name.strip("-的")
