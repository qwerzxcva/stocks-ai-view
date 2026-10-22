#!/usr/bin/env python3
"""
name_matching.py — 字辈/谐音识别（玄学因子可选维度）

移植自 GitCode stock_analysis/metaphysics/name_matching.py 的设计意图：
按 A 股股票简称中的「字辈」（同音字组 / 同部首字组）给股票打标签。

⚠️ 这是玄学对照实验的组成部分，不是可用因子；任何输出不得进入引擎权重、
选股池或实盘决策。

实现约束：纯标准库，无外部依赖。
- 拼音相似度：内置常用字拼音表（覆盖字辈组及常见谐音字），按完整拼音比对。
- 部首检测：内置「部首 → 常用字」索引。CJK 码位本身不含部首信息，
  标准库无法可靠反查，这里用显式归属表替代，保证可解释性。

名称数据源（load_cn_names 依次回退）：
  1. data/data/stock_names.json（全市场中文名，6121 条）
  2. data/stock_names.json（英文名表，会被 CJK 检查跳过）
  3. 从 all_sources.json / afternoon_picks.json / hot_themes.json walk
     提取 {code: name}（覆盖较小，最后回退）

用法:
  from name_matching import classify_name, classify, match_groups
  classify_name("洋河股份")   # ['yang']
  classify("sz002249")        # 需本地中文名表
  python3 scripts/name_matching.py 600519 大洋电机
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))
DATA = os.path.join(HERE, "..", "data")

# ---------------------------------------------------------------------------
# 字辈组定义：chars = 代表字；pinyin = 谐音归并集合；radicals = 部首归并集合。
# pinyin 集合刻意只放各组「代表字本体」的读音：如土系收 tu/yao（圭属 gui
# 但归并会把贵/桂/轨等高频字误吸入土系）；谐音字的归并交给拼音表逐字完成。
# ---------------------------------------------------------------------------
NAME_GROUPS: dict[str, dict] = {
    "yang": {
        "label": "羊系",
        "chars": list("羊洋扬庠徉阳杨炀旸飏氧恙"),
        "pinyin": {"yang"},
        "radicals": {"羊"},
    },
    "jin": {
        "label": "金系",
        "chars": list("金锦锟鑫银钦铭锐锋钢铃铮钊钧钰"),
        "pinyin": {"jin"},
        "radicals": {"金", "钅"},
    },
    "shui": {
        "label": "水系",
        "chars": list("水淼波涛瀚海江河流泉润泽浩洪潮澎瀛清溪漫"),
        "pinyin": {"shui", "miao"},
        "radicals": {"水", "氵"},
    },
    "mu": {
        "label": "木系",
        "chars": list("木林森桐梓杉松柏杨柳桂槐榕桦楠楷棣栋材"),
        "pinyin": {"mu", "lin", "sen"},
        "radicals": {"木"},
    },
    "tu": {
        "label": "土系",
        "chars": list("土圭垚坚基培坤城域垒坦坪坝壤塬墅"),
        "pinyin": {"tu", "yao"},
        "radicals": {"土"},
    },
}

# 兼容别名：并行实现里使用的组名
GROUP_ALIASES = {"jin_yin": "jin", "mu_lin": "mu", "tu_jian": "tu"}

# 五行映射（供 xu_factor 集成；羊系为纯谐音组，不属五行）
GROUP_WUXING = {"jin": "金", "shui": "水", "mu": "木", "tu": "土", "yang": None}

# ---------------------------------------------------------------------------
# 常用字拼音表：字辈组代表字 + A 股简称高频谐音字（可解释优先，非全量字典）
# ---------------------------------------------------------------------------
_PINYIN_TABLE: dict[str, str] = {}


def _register(pinyin: str, chars: str) -> None:
    for ch in chars:
        _PINYIN_TABLE.setdefault(ch, pinyin)


_register("yang", "羊洋扬庠徉阳杨炀旸飏氧恙养仰秧央鸯鞅")
_register("jin", "金锦津今巾斤筋襟谨瑾晋进近劲禁浸靳")
_register("yin", "银饮引印荫茵音殷阴寅淫蚓")
_register("kun", "锟坤昆鲲琨髡")
_register("xin", "鑫心欣辛新信芯薪锌歆昕忻")
_register("shui", "水税睡")
_register("miao", "淼秒苗描瞄妙庙渺缈")
_register("bo", "波播玻剥菠博薄勃渤铂箔帛伯泊舶")
_register("tao", "涛掏滔桃陶萄淘讨套")
_register("han", "瀚翰寒韩含涵函晗焓汉汗旱憾撼")
_register("hai", "海亥害骇氦")
_register("jiang", "江将浆姜僵疆讲奖降绛强")
_register("he", "河何合和荷核涸赫贺鹤禾")
_register("liu", "流刘留榴硫馏柳六")
_register("quan", "泉全权拳犬劝券")
_register("run", "润闰")
_register("ze", "泽则责择仄")
_register("hao", "浩豪毫号好耗皓灏")
_register("hong", "洪红宏虹鸿弘哄烘")
_register("chao", "潮超抄钞巢朝嘲炒")
_register("qing", "清青轻氢倾卿情晴擎请庆")
_register("mu", "木目牧慕墓幕募穆拇")
_register("lin", "林临邻淋琳霖凛吝")
_register("sen", "森")
_register("tong", "桐同铜童统桶痛通彤佟")
_register("zi", "梓子紫姿资兹滋咨籽字自")
_register("shan", "杉山删衫珊栅闪善擅膳")
_register("song", "松嵩怂宋送颂诵")
_register("bai", "柏百白伯摆败拜")
_register("gui", "桂圭龟规闺轨鬼诡柜贵跪")
_register("tu", "土吐兔图徒途涂屠")
_register("yao", "垚尧姚窑谣摇遥瑶咬药要耀钥")
_register("jian", "坚奸尖兼煎监笺拣俭检简建剑健舰渐溅鉴键箭")
_register("ji", "基机鸡积姬激及吉即极急疾集己计记技季剂迹济继绩")
_register("pei", "培陪赔佩配")
_register("cheng", "城成呈诚承乘盛程惩澄橙秤")

# 部首 → 常用字索引（显式归属表，替代标准库无法做的部首拆分）
_RADICAL_INDEX: dict[str, str] = {}


def _register_radical(radical: str, chars: str) -> None:
    for ch in chars:
        _RADICAL_INDEX.setdefault(ch, radical)


_register_radical("羊", "羊洋痒氧恙样佯徉烊蛘")
_register_radical("金", "金鑫")
_register_radical("钅", "银钢铁铜铝锡铭锐锋钊钧钰铃铮铸铺链锁锤镜镶锟锦钦钓钞钠钙钛钾铵铆铬铟铨锂锆锗锭锴镀镁")
_register_radical("水", "水淼泉浆")
_register_radical("氵", "江河流湖海洋波涛汹涌润泽浩洪潮澎瀛清溪漫潇洒漪涟涤涵淬淀淤淳淇淋淞淡深淦渝渭港渤湛溅濮")
_register_radical("木", "木林森桐梓杉松柏杨柳桂槐榕桦楠楷棣栋材杵枉枝枫枰枸柑柚柠柯栎栗株核桅梁梅梗梦棉棚棋植椰椿楗榭榴榷樊橡檀")
_register_radical("土", "土圭垚坚基培坤城域垒坦坪坝壤塬墅块址坊坎坑坯坡坷垢垦埂堵塔填境墓墙增壁")


def pinyin_of(ch: str) -> str | None:
    """查内置拼音表；未收录返回 None。"""
    return _PINYIN_TABLE.get(ch)


def radical_of(ch: str) -> str | None:
    """查内置部首表；未收录返回 None。"""
    return _RADICAL_INDEX.get(ch)


def _cjk_chars(text: str) -> list[str]:
    return [c for c in text if "一" <= c <= "鿿"]


def match_groups(name: str) -> dict[str, list[str]]:
    """
    对一个中文简称做字辈匹配。
    返回 {group_key: [命中的字, ...]}，只保留有命中的组。
    命中规则（对一个组而言，满足其一即算该字命中）：
      1. 字本身在该组代表字集合内；
      2. 字的拼音（内置表）在该组拼音集合内；
      3. 字的部首（内置表）在该组部首集合内。
    """
    hits: dict[str, list[str]] = {}
    for ch in _cjk_chars(name):
        py = pinyin_of(ch)
        rad = radical_of(ch)
        for key, g in NAME_GROUPS.items():
            if ch in g["chars"] or (py and py in g["pinyin"]) or (rad and rad in g["radicals"]):
                hits.setdefault(key, [])
                if ch not in hits[key]:
                    hits[key].append(ch)
    return hits


def classify_name(name: str) -> list[str]:
    """返回该名称命中的字辈组 key 列表（按 NAME_GROUPS 定义顺序）。"""
    hits = match_groups(name)
    return [k for k in NAME_GROUPS if k in hits]


def match_group(name: str) -> str | None:
    """单组便捷接口：返回第一个命中的组 key，无命中返回 None。"""
    groups = classify_name(name)
    return groups[0] if groups else None


def extract_name_map(data_dir: str) -> dict[str, str]:
    """
    从 all_sources.json / afternoon_picks.json / hot_themes.json walk
    提取 {code: name}（覆盖较小，作为 load_cn_names 的最后回退）。
    """
    names: dict[str, str] = {}
    for fname in ("all_sources.json", "afternoon_picks.json", "hot_themes.json"):
        p = os.path.join(data_dir, fname)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        def walk(obj, depth=0):
            if depth > 5:
                return
            if isinstance(obj, dict):
                code = obj.get("code")
                name = obj.get("name")
                if code and name and isinstance(name, str) and _cjk_chars(name) \
                        and len(str(code)) >= 6:
                    names[str(code)] = name
                for v in obj.values():
                    if isinstance(v, (dict, list)):
                        walk(v, depth + 1)
            elif isinstance(obj, list):
                for item in obj[:1000]:
                    walk(item, depth + 1)

        walk(d)
    return names


def load_cn_names(path: str | None = None) -> dict[str, str]:
    """
    加载中文股票名表 {sz000001: 平安银行}。
    默认依次尝试 data/data/stock_names.json 与 data/stock_names.json，
    只接受值里含 CJK 字符的那份；两者皆无则回退 extract_name_map。
    """
    candidates = [path] if path else [
        os.path.join(DATA, "data", "stock_names.json"),
        os.path.join(DATA, "stock_names.json"),
    ]
    for p in candidates:
        if not p or not os.path.isfile(p):
            continue
        try:
            raw = json.load(open(p, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict) or not raw:
            continue
        sample = next(iter(raw.values()))
        if isinstance(sample, str) and _cjk_chars(sample):
            return {k: v for k, v in raw.items() if isinstance(v, str)}
    return extract_name_map(DATA)


def classify(code_or_name: str, names: dict[str, str] | None = None) -> list[str]:
    """
    输入股票代码（如 'sz002249' / '002249'）或直接输入中文名称，
    返回命中的字辈组 key 列表。
    """
    if _cjk_chars(code_or_name):
        return classify_name(code_or_name)
    names = names if names is not None else load_cn_names()
    name = names.get(code_or_name)
    if name is None:
        # 兼容纯数字代码
        digits = re.sub(r"\D", "", code_or_name)
        for k, v in names.items():
            if re.sub(r"\D", "", k) == digits:
                name = v
                break
    if not name:
        return []
    return classify_name(name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="*", help="股票代码或中文名称")
    ap.add_argument("--out", default=None,
                    help="给出时把全市场分组写入该 JSON（同 metaphysics_report 格式）")
    args = ap.parse_args()

    names = load_cn_names()
    if not args.queries and not args.out:
        print("用法: python3 scripts/name_matching.py <代码或名称> [...] [--out data/name_groups.json]")
        print(f"已加载中文名表 {len(names)} 条")
        for q in ["600519", "sz002249", "大洋电机"]:
            print(f"  {q!r:12} → {classify(q, names)}")
        return 0

    for q in args.queries:
        print(f"{q}: {classify(q, names)}")

    if args.out:
        import metaphysics_report  # 同目录
        rep = metaphysics_report.build_report(names)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
        print(f"→ {args.out} ({rep['n_classified']}/{rep['n_names']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
