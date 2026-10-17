#!/usr/bin/env python3
"""
news_transmission.py — 消息传导引擎 (坏消息利好谁 / 好消息利空谁)

news_impact.py 只做单向映射: 坏消息 → 该板块的股票 bear。
但真实市场里一条消息有两个方向的资金流向:
  - 直接受损方 (半导体大跌 → 半导体股 bear)
  - 间接受益方 (半导体大跌 → 资金可能流向银行/公用事业/黄金等防御板块;
               或者「国产替代」逻辑 → 本土设备/材料股 bull)

本引擎补上第二半。对每条新闻:
  1. judge_direction 判多空 (复用 news_impact 的词典)
  2. match_news 找直接命中的行业/概念 → 受损或受益方
  3. transmission 表定义「对冲/替代/避险」关系:
       半导体↓ → 利好 国产半导体设备/材料 (国产替代), 利空 依赖进口的封测
       医药集采↓ → 利好 创新药/CXO (不受集采), 利空 仿制药
       油价↑ → 利好 油气开采/油服, 利空 航空/物流/化工
       某股东减持 → 利好 同板块竞争对手 (资金转移), 利空 该股本身
       美联储加息 → 利好 银行/保险 (息差), 利空 高估值成长/黄金
  4. 输出 direct (直接命中) + inverse (反向传导) + hedge (避险去向)

产出 data/news_transmission.json, 前端「消息传导」区块与 AI 复盘消费。

用法:
  python3 news_transmission.py --in data/hot_news.json --news data/news_extra.json \
      --out data/news_transmission.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")


# ─── 传导关系表 ────────────────────────────────────────────────────
# trigger: 命中任一关键词即触发
# direct_bear / direct_bull: 直接受影响方向
# inverse_bull: 坏消息下的受益方 (资金转移/替代逻辑)
# inverse_bear: 好消息下的受害方 (被替代/被抽血)
# hedge: 坏消息时的避险去向
TRANS = [
    {
        "id": "semi_down", "trigger": ["半导体大跌", "芯片股跌", "费半", "芯片股跌势",
                                        "半导体板块跌", "半导体领跌", "芯片领跌", "集成电路跌"],
        "label": "半导体下跌",
        "direct_bear": ["半导体", "集成电路", "芯片"],
        "inverse_bull": ["半导体设备", "半导体材料", "国产替代"],
        "hedge": ["银行", "公用事业", "黄金"],
    },
    {
        "id": "oil_up", "trigger": ["油价上涨", "原油涨", "油价大涨", "布伦特", "OPEC减产"],
        "label": "油价上涨",
        "direct_bull": ["油气开采", "油服工程", "石油"],
        "inverse_bear": ["航空机场", "物流", "化工"],
        "hedge": [],
    },
    {
        "id": "oil_down", "trigger": ["油价下跌", "原油跌", "油价大跌", "布伦特跌"],
        "label": "油价下跌",
        "direct_bear": ["油气开采", "油服工程", "石油"],
        "inverse_bull": ["航空机场", "物流", "化工"],
        "hedge": [],
    },
    {
        "id": "rate_hike", "trigger": ["美联储加息", "加息预期", "紧缩", "加息必要性"],
        "label": "加息/紧缩",
        "direct_bear": ["高估值成长", "创新药", "新能源"],
        "inverse_bull": ["银行", "保险"],
        "hedge": [],
    },
    {
        "id": "rate_cut", "trigger": ["美联储降息", "降息预期", "宽松", "降准"],
        "label": "降息/宽松",
        "direct_bull": ["成长股", "创新药", "新能源", "科技"],
        "inverse_bear": ["银行"],
        "hedge": [],
    },
    {
        "id": "reduce_holding", "trigger": ["股东减持", "拟减持", "大股东减持", "减持计划"],
        "label": "股东减持",
        "direct_bear": [],
        "inverse_bull": [],
        "hedge": [],
        "note": "减持只利空该股本身; 同板块竞争对手是资金转移的候选受益方, "
                "但需要个股级信息, 这里只标注「该股承压」",
    },
    {
        "id": "buyback", "trigger": ["回购", "增持", "拟增持"],
        "label": "回购/增持",
        "direct_bull": [],
        "inverse_bear": [],
        "hedge": [],
        "note": "只利多该股本身",
    },
    {
        "id": "comm_down", "trigger": ["通信领跌", "通信板块跌", "5G跌", "运营商跌",
                                        "通信设备跌", "光通信跌"],
        "label": "通信下跌",
        "direct_bear": ["通信设备", "通信服务", "5G"],
        "inverse_bull": ["光模块", "卫星通信", "算力"],
        "hedge": ["银行", "公用事业"],
    },
    {
        "id": "ai_down", "trigger": ["AI大跌", "AI泡沫", "算力股跌", "AI概念跌", "AI领域大跌",
                                      "人工智能跌", "科技股大跌"],
        "label": "AI/算力下跌",
        "direct_bear": ["算力", "人工智能", "计算机", "软件"],
        "inverse_bull": ["国产算力", "信创", "AI应用"],
        "hedge": ["银行", "公用事业", "黄金"],
    },
    {
        "id": "market_broad_down", "trigger": ["超5000股飘绿", "超5000股收绿", "全市场飘绿",
                                                "沪指跌近2%", "创业板指跌近3%", "普跌"],
        "label": "大盘普跌",
        "direct_bear": [],
        "inverse_bull": ["银行", "公用事业", "黄金", "防御"],
        "hedge": ["银行", "公用事业", "黄金"],
        "note": "普跌时资金流向低估值防御; 逆势活跃的板块 (如 PCB/白酒) 是当日主线",
    },
    {
        "id": "new_energy_down", "trigger": ["新能源车大跌", "锂电跌", "光伏跌", "储能跌"],
        "label": "新能源下跌",
        "direct_bear": ["锂电池", "光伏", "储能", "新能源车"],
        "inverse_bull": ["传统能源", "火电", "煤炭"],
        "hedge": ["公用事业"],
    },
    {
        "id": "medicine_down", "trigger": ["医药集采", "药品集采", "医保谈判", "医药股跌"],
        "label": "医药集采",
        "direct_bear": ["仿制药", "化学制药"],
        "inverse_bull": ["创新药", "CXO", "医疗器械"],
        "hedge": [],
    },
    {
        "id": "realestate_down", "trigger": ["房地产大跌", "地产股跌", "楼市下跌"],
        "label": "地产下跌",
        "direct_bear": ["房地产", "建材", "家居"],
        "inverse_bull": ["银行", "物业管理"],
        "hedge": [],
    },
    {
        "id": "export_control", "trigger": ["出口管制", "制裁", "实体清单", "断供"],
        "label": "出口管制/制裁",
        "direct_bear": [],
        "inverse_bull": ["国产替代", "半导体设备", "信创", "自主可控"],
        "hedge": [],
        "note": "制裁对本土供应链是国产替代逻辑, 双向: 依赖进口的环节 bear",
    },
    {
        "id": "geopolitics", "trigger": ["地缘", "冲突", "战争", "军事"],
        "label": "地缘冲突",
        "direct_bull": ["军工", "黄金", "石油"],
        "inverse_bear": ["航空", "旅游"],
        "hedge": ["黄金"],
    },
    {
        "id": "epidemic", "trigger": ["疫情", "流感", "病毒"],
        "label": "疫情",
        "direct_bull": ["医药", "医疗器械", "疫苗"],
        "inverse_bear": ["旅游", "航空", "餐饮"],
        "hedge": [],
    },
]


def load(name):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def industry_stocks():
    """行业 → 成分股 (东财中文, build_industry_map_em 的产物)。"""
    d = load("industry_map_em.json")
    m = d.get("map") or {}
    out = {}
    for code, ind in m.items():
        if ind:
            out.setdefault(ind, []).append(code)
    return {k: v[:15] for k, v in out.items()}


def concept_stocks():
    """概念 → 成分股。"""
    d = load("concept_map.json")
    out = {}
    for k, v in (d.get("concepts") or d).items():
        stocks = v.get("stocks") if isinstance(v, dict) else v
        if isinstance(stocks, list) and stocks:
            out[k] = stocks[:15]
    return out


def find_sector_stocks(name, ind_map, con_map):
    """按行业/概念名找成分股, 支持包含匹配 (国产半导体设备 vs 半导体)。"""
    codes = []
    for k, v in ind_map.items():
        if name in k or k in name:
            codes.extend(v)
    for k, v in con_map.items():
        if name in k or k in name:
            codes.extend(v)
    # 去重保序
    seen, out = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out[:10]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="hot", default="data/hot_news.json")
    ap.add_argument("--news", default="data/news_extra.json")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "news_transmission.json"))
    args = ap.parse_args()

    hot = load(os.path.basename(args.hot))
    extra = load(os.path.basename(args.news))
    ind_map = industry_stocks()
    con_map = concept_stocks()

    items = []
    for it in (hot.get("items") or []):
        items.append({"title": it.get("title") or "", "source": it.get("source_name") or it.get("source"),
                      "time": it.get("updated") or "", "url": it.get("url") or ""})
    for it in (extra.get("items") or []):
        items.append({"title": it.get("title") or "", "source": it.get("source") or "",
                      "time": it.get("time") or "", "url": it.get("url") or ""})
    for cat, arr in (extra.get("categories") or {}).items():
        if isinstance(arr, list):
            for it in arr[:20]:
                items.append({"title": it.get("title") or "", "source": cat,
                              "time": it.get("time") or "", "url": it.get("url") or ""})

    print("[transmission] 输入 %d 条资讯 | 行业 %d | 概念 %d" % (
        len(items), len(ind_map), len(con_map)), flush=True)

    money = load("moneyflow.json") or {}
    flow_date = str(money.get("data_date") or "")[:10]
    flow = {}
    if not money.get("fully_degraded"):
        for x in (money.get("top_outflow") or []) + (money.get("top_inflow") or []):
            row_date = str(x.get("date") or "")[:10]
            if row_date and (not flow_date or row_date == flow_date):
                flow[str(x.get("code"))] = x

    seen_events = set()
    rows = []
    for it in items:
        t = it["title"]
        event_key = ("".join(str(t).split()), str(it.get("time") or "")[:10])
        if not t or event_key in seen_events:
            continue
        seen_events.add(event_key)
        hit = None
        for tr in TRANS:
            if any(k in t for k in tr["trigger"]):
                hit = tr
                break
        if not hit:
            continue
        rec = {
            "title": t[:90],
            "source": it.get("source"),
            "time": it.get("time"),
            "url": it.get("url"),
            "transmission_id": hit["id"],
            "label": hit["label"],
            "direct_bear": [], "direct_bull": [],
            "inverse_bull": [], "inverse_bear": [], "hedge": [],
        }
        for name in hit.get("direct_bear", []):
            cs = find_sector_stocks(name, ind_map, con_map)
            if cs:
                rec["direct_bear"].append({"sector": name, "stocks": cs})
        for name in hit.get("direct_bull", []):
            cs = find_sector_stocks(name, ind_map, con_map)
            if cs:
                rec["direct_bull"].append({"sector": name, "stocks": cs})
        for name in hit.get("inverse_bull", []):
            cs = find_sector_stocks(name, ind_map, con_map)
            if cs:
                rec["inverse_bull"].append({"sector": name, "stocks": cs})
        for name in hit.get("inverse_bear", []):
            cs = find_sector_stocks(name, ind_map, con_map)
            if cs:
                rec["inverse_bear"].append({"sector": name, "stocks": cs})
        for name in hit.get("hedge", []):
            cs = find_sector_stocks(name, ind_map, con_map)
            if cs:
                rec["hedge"].append({"sector": name, "stocks": cs})
        event_date = str(it.get("time") or "")[:10]
        flow_for_event = {code: row for code, row in flow.items()
                          if event_date and str(row.get("date"))[:10] == event_date}
        related = {str(code) for side in ("direct_bull", "direct_bear")
                   for sector in rec[side] for code in sector.get("stocks") or []}
        matched = related.intersection(flow_for_event)
        rec["flow_evidence"] = {"status": "matched" if matched else "insufficient",
                                "source": money.get("source"), "trade_date": flow_date or None,
                                "event_date": event_date or None, "matched_codes": sorted(matched)}
        for item in rec["direct_bull"]:
            for code in item.get("stocks") or []:
                evidence = flow_for_event.get(str(code)) or {}
                ratio = evidence.get("main_net_ratio")
                if ratio is not None and ratio < 0:
                    item.setdefault("behavior", []).append({"code": code, "label": "好消息诱多嫌疑", "evidence": f"主力净占比 {ratio}%", "source": evidence.get("_src"), "trade_date": evidence.get("date"), "snapshot_ts": evidence.get("snapshot_ts")})
        for item in rec["direct_bear"]:
            for code in item.get("stocks") or []:
                evidence = flow_for_event.get(str(code)) or {}
                ratio = evidence.get("main_net_ratio")
                if ratio is not None and ratio > 0:
                    item.setdefault("behavior", []).append({"code": code, "label": "坏消息洗盘嫌疑", "evidence": f"主力净占比 {ratio}%", "source": evidence.get("_src"), "trade_date": evidence.get("date"), "snapshot_ts": evidence.get("snapshot_ts")})
        if hit.get("note"):
            rec["note"] = hit["note"]
        # 只有真的推出受益/受损方才收录
        if any(rec[k] for k in ("direct_bear", "direct_bull", "inverse_bull",
                                "inverse_bear", "hedge")):
            rows.append(rec)

    rows.sort(key=lambda r: -(len(r["inverse_bull"]) + len(r["inverse_bear"])
                              + len(r["hedge"])))

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "news_transmission v1 (消息传导: 坏消息利好谁)",
        "n_input": len(items),
        "n_transmissions": len(rows),
        "n_rules": len(TRANS),
        "rules": [{"id": r["id"], "label": r["label"]} for r in TRANS],
        "flow_evidence": {"source": money.get("source"), "trade_date": flow_date or None,
                          "status": "dated_rows_available" if flow else "insufficient",
                          "n_dated_rows": len(flow)},
        "caveat": "传导关系是规则先验, 非实证; 行为标签要求新闻与资金同交易日, 缺失不判定。"
                  "Research reference only, not investment advice.",
        "transmissions": rows[:40],
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("[transmission] -> %s (%d 条传导)" % (args.out, len(rows)))
    for r in rows[:8]:
        seg = []
        if r["inverse_bull"]:
            seg.append("受益: " + "/".join(x["sector"] for x in r["inverse_bull"][:3]))
        if r["inverse_bear"]:
            seg.append("受害: " + "/".join(x["sector"] for x in r["inverse_bear"][:3]))
        if r["hedge"]:
            seg.append("避险: " + "/".join(x["sector"] for x in r["hedge"][:3]))
        print("  [%s] %s | %s" % (r["label"], r["title"][:40], " ; ".join(seg)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
