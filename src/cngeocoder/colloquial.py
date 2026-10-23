"""口语化、非标准地址的预处理。

真实输入里常见的“不标准”大致几类，这里处理与语义无关、可以安全改写的部分：
- 繁体字（港澳台用户、OCR）：地址常用字的繁 → 简映射；
- 收寄件口语：“麻烦送到……谢谢”“放门卫/快递柜”“到了打电话”“工作日送货”；
- 北京话儿化（“院儿”“胡同儿”）、口语楼层（“负一楼”“地下二层”）、表情符号；
- POI 的常见简称（北京大学 → 北大，北京工人体育场 → 工体，上海火车站 → 上海站），建库时生成。

方位描述（“对面”“往东 500 米”）、交叉口、行政区错别字由 parser / admin 处理，
因为它们需要结构化结果，不能简单删掉。
"""

from __future__ import annotations

import re
import unicodedata

# 繁 → 简：只收地址里常见、且在简体文本中不会出现的繁体字（避免误改“乾县”等简体地名）
_T2S_PAIRS = (
    "區区縣县鎮镇鄉乡號号樓楼層层廣广東东華华門门灣湾龍龙豐丰雲云寶宝興兴陽阳開开發发園园廈厦場场廠厂莊庄"
    "橋桥灘滩嶺岭壩坝嶼屿島岛澤泽湯汤漢汉濱滨潤润溝沟滬沪臺台蘇苏匯汇滙汇黃黄寧宁遼辽齊齐濟济魯鲁貴贵陝陕"
    "遠远運运達达連连進进邊边過过這这還还鐵铁銀银錦锦鋼钢長长閣阁間间閘闸關关陸陆隊队際际陳陈隨随雙双雞鸡"
    "離离電电靈灵韓韩頭头順顺題题願愿風风飛飞館馆馬马驛驿體体鳳凤鳥鸟鴻鸿鶴鹤麗丽麥麦黨党齋斋為为烏乌無无"
    "熱热燈灯營营爐炉牆墙獅狮環环現现產产畫画當当療疗盤盘碼码礦矿禮礼稅税種种窯窑築筑範范簡简糧粮紅红紀纪"
    "紡纺線线組组經经綠绿網网編编緣缘縱纵總总織织繡绣繼继續续羅罗義义習习聖圣聯联職职腦脑臨临舊旧葉叶蓮莲"
    "蔣蒋藝艺蘭兰處处蝦虾衛卫衝冲補补裝装見见規规視视親亲覺觉觀观訊讯記记設设許许診诊試试話话誠诚詩诗語语"
    "說说課课調调談谈諾诺證证識识護护讀读貝贝財财貨货質质購购賓宾賢贤賣卖贊赞趙赵車车軍军軟软輝辉輪轮農农"
    "郵邮鄭郑醫医醬酱鋪铺錢钱鐘钟鏡镜問问閩闽閱阅闊阔陰阴階阶隱隐雜杂難难靜静頁页項项預预領领頻频顏颜類类"
    "顯显飯饭飲饮養养館馆驗验魚鱼鮮鲜鹽盐麵面點点齒齿龜龟慶庆應应張张強强彎弯徑径後后復复憲宪戲戏戶户揚扬"
    "換换據据擴扩斷断時时晉晋曉晓會会條条來来楊杨業业極极樂乐標标樹树橫横檢检櫃柜權权歐欧歲岁歷历殘残氣气"
    "漁渔滿满潔洁燒烧爾尔獎奖瑪玛畢毕異异盧卢碩硕確确稱称穩稳窩窝競竞筆笔節节簽签籃篮紙纸細细終终結结給给"
    "絲丝統统維维綜综緊紧練练舉举蘋苹蘆芦虛虚蟲虫術术製制複复覽览計计訂订認认誌志請请論论諮咨謝谢讓让豬猪"
    "貿贸費费資资賽赛趕赶躍跃軌轨較较載载輕轻輸输辦办遊游遞递適适選选遺遗釘钉針针鈴铃鋁铝錄录鍋锅鍾钟鏈链"
    "鑽钻閃闪閉闭閒闲雖虽霧雾韻韵響响頂顶頓顿顧顾颱台餅饼驅驱騰腾驚惊鬥斗鬧闹鳴鸣黴霉嘉嘉灣湾巖岩廟庙"
    "壇坛樞枢錫锡塢坞堯尧賀贺溫温灣湾濰潍滄沧瀋沈攤摊撫抚擋挡亞亚倉仓僑侨傢家儀仪兒儿兩两嚴严圍围團团"
    "圖图圓圆壽寿夢梦奧奥婦妇學学寧宁實实寬宽專专將将對对尋寻導导屬属峽峡巒峦幣币幫帮廬庐彙汇從从憶忆"
    "懷怀戰战撥拨擁拥攜携數数敵敌斬斩檔档歸归殺杀滙汇澳澳灑洒災灾爭争牽牵狀状獨独猶犹瑤瑶甌瓯畝亩發发"
    "盜盗碩硕禦御禱祷穀谷窮穷竊窃筍笋簾帘粵粤糾纠紋纹紹绍綿绵緒绪縮缩織织罰罚聞闻聲声肅肃膠胶藍蓝蘊蕴"
    "號号蠶蚕衆众袞衮襯衬覓觅訪访詞词誕诞誼谊謀谋謙谦講讲譯译變变貴贵賦赋賴赖贈赠趨趋跡迹蹤踪轉转辭辞"
    "迴回郟郏鄰邻釀酿釋释鉅钜銅铜鋒锋錯错鎂镁鎮镇鑄铸閔闵闖闯陣阵陽阳隴陇雁雁霸霸韶韶頤颐頌颂馳驰駐驻"
    "騎骑髒脏鬱郁魏魏鯉鲤鯨鲸鴨鸭鵬鹏鷹鹰麓麓黎黎黨党龐庞別别個个們们價价僅仅備备傳传偉伟側侧優优億亿創创"
    "劃划勝胜勞劳協协參参員员喬乔單单圖图執执堅坚塊块報报墾垦壓压奮奋婁娄孫孙寫写審审屆届岡冈峯峰崗岗"
    "幾几廚厨廢废廳厅轄辖郵邮莊庄濕湿灣湾礎础穩稳舖铺館馆鄧邓鄒邹鄺邝錢钱鏢镖鑼锣閥阀闆板陝陕隸隶"
    "澱淀測测鬆松燕燕藥药診诊鐸铎銷销鍵键聽听戀恋練练綱纲網网縣县樣样權权"
)
_T2S = {t: s for t, s in zip(_T2S_PAIRS[0::2], _T2S_PAIRS[1::2], strict=True) if t != s}

# 收寄件口语与无定位意义的嘱咐语
_NOISE_PATTERNS = [
    r"^(?:麻烦|请|帮忙|帮我|劳驾|亲)+(?:送到|寄到|送至|寄至|派送到|放到|放在|送去)?",
    r"^(?:送到|寄到|送至|寄至|派送到|收货地址|地址是|地址)",
    r"(?:放|放到|放在|存|存放在?|寄存在?)(?:门卫|门卫室|传达室|前台|物业|收发室|快递柜|丰巢柜?|菜鸟驿站|驿站|门口)(?:就行|即可)?",
    r"(?:到了|到楼下|到门口)(?:给我)?(?:先)?(?:打|拨打)?电话(?:联系)?|(?:给我)?(?:先)?(?:打|拨打)电话(?:联系)?",
    r"不要打电话|电话联系|短信联系|提前联系",
    r"(?:工作日|周末|节假日|上班时间|下班后|晚上|白天)(?:送货|派送|配送|收货)(?:均可)?",
    r"(?:加急|急件|谢谢|谢谢啦|辛苦了|多谢)+$",
]
_NOISE_RE = [re.compile(p) for p in _NOISE_PATTERNS]
_ERHUA_RE = re.compile(r"(?<=[院同门边口巷])儿")
_FLOOR_RE = re.compile(r"(?:负|地下)([一二三四五六七八九\d])(?:层|楼)")
_CN_SMALL = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}


def to_simplified(text: str) -> str:
    if not text:
        return text
    return "".join(_T2S.get(ch, ch) for ch in text)


def has_traditional(text: str) -> bool:
    return any(ch in _T2S for ch in text)


def strip_symbols(text: str) -> str:
    """去掉表情等符号（Unicode So/Sk/Cs/Co 类别），保留文字、数字和普通标点。"""
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("So", "Sk", "Cs", "Co"))


def clean_colloquial(text: str) -> tuple[str, list[str]]:
    """返回 (清洗后文本, 被去掉的片段)。输入应已做过 NFKC 与空白处理。"""
    removed: list[str] = []
    s = text
    for rx in _NOISE_RE:
        def _drop(m: re.Match[str]) -> str:
            if m.group():
                removed.append(m.group())
            return ""

        s = rx.sub(_drop, s)
    s = _ERHUA_RE.sub("", s)
    s = _FLOOR_RE.sub(lambda m: f"B{_CN_SMALL.get(m.group(1), m.group(1))}层", s)
    return s, removed


# ----------------------------------------------------------------- 简称生成
_GENERIC = ("中心", "大厦", "广场", "大学", "医院", "公园", "学院", "中学", "小学", "大楼", "商场", "花园", "小区",
            "火车站", "汽车站", "机场", "体育场", "体育馆", "博物馆", "图书馆", "酒店", "宾馆", "市场", "银行")
_UNIVERSITY_PREFIX = ("中国",)


def is_generic(name: str) -> bool:
    """名称是否只剩类别词（火车站、中心大厦、南广场），这类名称不能单独作为匹配依据。"""
    core = name
    for _ in range(3):
        for g in sorted(_GENERIC, key=len, reverse=True):
            if core.endswith(g):
                core = core[: -len(g)]
                break
        else:
            break
    return len(core) <= 1


GENERIC_BIGRAMS = frozenset(g[i:i + 2] for g in _GENERIC for i in range(len(g) - 1))


def abbreviations(name: str, admin_prefixes: tuple[str, ...] = ()) -> list[str]:
    """按中文简称习惯生成别名：取词首字 + 类别字。只覆盖规律性强的几类，宁缺毋滥。

    北京大学 → 北大；中国人民大学 → 人大；北京师范大学 → 北师大；北京工人体育场 → 工体；
    首都体育馆 → 首体；上海火车站 → 上海站；北京首都国际机场 → 首都机场（去掉城市前缀）。
    """
    out: list[str] = []
    n = name.strip()
    stem_city = n
    for p in sorted(admin_prefixes, key=len, reverse=True):
        if n.startswith(p) and len(n) - len(p) >= 3:
            stem_city = n[len(p):]
            break
    if n.endswith("大学"):
        stem = n[:-2]
        for pre in _UNIVERSITY_PREFIX:
            if stem.startswith(pre) and len(stem) > len(pre) + 1:
                stem = stem[len(pre):]
        if len(stem) == 2:
            out.append(stem[0] + "大")
        elif len(stem) == 4:
            out.append(stem[0] + stem[2] + "大")
    for suf, short in (("体育场", "体"), ("体育馆", "体")):
        if stem_city.endswith(suf):
            stem = stem_city[: -len(suf)]
            if len(stem) == 2:
                out.append(stem[0] + short)
    for base in dict.fromkeys((n, stem_city)):
        if base.endswith("火车站") and len(base) > 3:
            out.append(base[:-3] + "站")
        if base.endswith("国际机场") and len(base) > 4:
            out.append(base[:-4] + "机场")
    # 去掉城市前缀后仍有辨识度的名称（北京首都国际机场 → 首都国际机场）：去掉类别词后至少 3 个字
    if stem_city != n:
        core = stem_city
        for g in sorted(_GENERIC, key=len, reverse=True):
            if core.endswith(g):
                core = core[: -len(g)]
                break
        if len(core) >= 3:
            out.append(stem_city)
    return [a for a in dict.fromkeys(out) if a != n and len(a) >= 2]
