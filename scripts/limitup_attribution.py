#!/usr/bin/env python3
"""
limitup_attribution.py — 涨停归因与预测引擎

两个问题分开回答:
  1. 为什么会涨停 (归因) — 对当日涨停池逐只拆解驱动因素
  2. 怎么提前发现 / 预测下一个涨停票 (预测) — 用归因出的特征反向筛候选

## 归因维度 (每只涨停票打多个标签, 可叠加)
  - 连板高度: 首板 / 2 板 / 3 板 / 4+ 板 (连板越高, 板块效应越强)
  - 封板时点: 09:25 竞价抢筹 / 早盘强势 / 午盘偷袭 / 尾盘偷袭
  - 封板质量: 封单强度 (seal_strength) + 量比 (vol_ratio)
  - 题材归因: 所属行业 + 同花顺 reason + 当日热点主题命中
  - 位置: 前期涨幅 (prior_gain_5d) — 低位首板 vs 高位加速
  - 板块协同: 同行业今日涨停家数 (独家 vs 板块共振)

## 预测 (提前发现下一个涨停票)
对全市场候选按「涨停基因」打分:
  - 基因 1: 近期有涨停历史 (涨停基因延续)
  - 基因 2: 所属板块今日已有多只涨停 (板块效应将扩散)
  - 基因 3: 接近涨停 (涨幅 7~9.5%) 且量比放大 (即将封板)
  - 基因 4: 封板时点集中在竞价 (资金抢筹意愿强)
  - 基因 5: 低位首板 + 缩量回调后放量 (蓄势突破)

产出 data/limitup_attribution.json (归因) + data/limitup_forecast.json (预测)

用法:
  python3 limitup_attribution.py
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


def load(name):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return {}
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def seal_time_bucket(zt_time):
    """首封时点分桶 (东财 fbt 省略前导零: 92500 = 09:25:00, 93000 = 09:30:00)。

    注意 fbt 是 HHMMSS 六位串, 必须先拆成 时/分/秒 再换算成「当日第几分钟」,
    不能把整串当分钟数 (93000 当分钟会得到 5580, 永远大于任何阈值)。
    """
    t = str(zt_time or "").strip()
    if not t.isdigit():
        return "unknown", "时点未知"
    t = t.zfill(6)
    hh, mm = int(t[:2]), int(t[2:4])
    mins = hh * 60 + mm
    if mins <= 9 * 60 + 25:               # 09:25 集合竞价结束
        return "auction", "竞价抢筹"
    if mins < 10 * 60 + 30:
        return "early", "早盘强势"
    if mins < 11 * 60 + 30:
        return "mid_am", "上午中段"
    if mins < 14 * 60:
        return "early_pm", "午后早段"
    return "late", "尾盘偷袭"


def lianban_bucket(n):
    n = int(n or 1)
    if n <= 1:
        return 1, "首板"
    if n == 2:
        return 2, "2 连板"
    if n == 3:
        return 3, "3 连板"
    if n == 4:
        return 4, "4 连板"
    return 5, "%d 连板(高位)" % n


def attribute(pool, ban_picks, enrich, hot_news):
    """对涨停池逐只归因。"""
    # 同行业涨停家数 → 板块协同
    ind_count = {}
    for z in pool:
        ind = z.get("industry") or ""
        if ind:
            ind_count[ind] = ind_count.get(ind, 0) + 1

    ban_by_code = {str(b.get("code")): b for b in (ban_picks.get("picks") or [])}
    ths_reason = {}
    for s in (enrich.get("ths_strong") or []):
        if s.get("code") and s.get("reason"):
            ths_reason[str(s["code"])] = s["reason"]

    # 当日热点主题词 (用于题材命中)
    theme_words = set()
    for it in (hot_news.get("items") or [])[:80]:
        t = it.get("title") or ""
        for w in ("PCB", "白酒", "半导体", "机器人", "储能", "算力", "军工",
                  "医药", "新能源", "稀土", "光伏", "券商", "银行", "黄金"):
            if w in t:
                theme_words.add(w)

    rows = []
    for z in pool:
        code = str(z.get("code") or "")
        if not code:
            continue
        b = ban_by_code.get(code) or {}
        lb, lb_label = lianban_bucket(z.get("lianban"))
        st, st_label = seal_time_bucket(z.get("zt_time"))
        ind = z.get("industry") or ""
        peers = ind_count.get(ind, 0)
        reasons = []
        tags = []

        tags.append(lb_label)
        if lb >= 3:
            reasons.append("%d 连板, 市场龙头/妖股特征" % lb)
        elif lb == 1:
            reasons.append("首板, 新题材或资金试盘")

        tags.append(st_label)
        if st == "auction":
            reasons.append("09:25 竞价即封, 隔夜资金抢筹意愿最强")
        elif st == "late":
            reasons.append("尾盘偷袭封板, 资金谨慎或实力不足")
        elif st == "early":
            reasons.append("早盘快速封板, 资金坚决")

        ss = b.get("seal_strength")
        if ss is not None:
            if ss >= 1.5:
                reasons.append("封单强度 %.2f (封板质量优)" % ss)
                tags.append("强封板")
            elif ss < 0.8:
                reasons.append("封单强度 %.2f (封板质量弱, 易炸)" % ss)
                tags.append("弱封板")

        vr = b.get("vol_ratio")
        if vr is not None:
            if vr >= 2:
                reasons.append("量比 %.2f (放量突破)" % vr)
                tags.append("放量")
            elif vr <= 0.5:
                reasons.append("量比 %.2f (缩量涨停, 抛压小)" % vr)
                tags.append("缩量涨停")

        pg = b.get("prior_gain_5d")
        if pg is not None:
            if pg >= 30:
                reasons.append("近5日已涨 %.1f%% (高位加速, 风险大)" % pg)
                tags.append("高位加速")
            elif pg <= 5:
                reasons.append("近5日仅涨 %.1f%% (低位启动)" % pg)
                tags.append("低位启动")

        if peers >= 3:
            reasons.append("同行业 %s 今日 %d 只涨停 (板块共振)" % (ind, peers))
            tags.append("板块共振")
        elif peers == 1:
            reasons.append("行业 %s 独家涨停 (个股逻辑为主)" % ind)
            tags.append("独家涨停")

        tr = ths_reason.get(code)
        if tr:
            reasons.append("题材: %s" % str(tr)[:40])
            tags.append("有题材")

        hit = [w for w in theme_words if w in (tr or "")]
        if hit:
            reasons.append("命中当日热点: %s" % "/".join(hit))
            tags.append("热点命中")

        if not reasons:
            reasons.append("数据不足, 仅有时点与连板信息")

        rows.append({
            "code": code,
            "name": z.get("name") or "",
            "price": z.get("price"),
            "chg_pct": z.get("chg_pct"),
            "industry": ind,
            "lianban": lb,
            "lianban_label": lb_label,
            "seal_time": str(z.get("zt_time") or ""),
            "seal_bucket": st,
            "seal_label": st_label,
            "seal_strength": ss,
            "vol_ratio": vr,
            "prior_gain_5d": pg,
            "p_zt_next": b.get("p_zt_next"),
            "p_zhaban_next": b.get("p_zhaban_next"),
            "verdict": b.get("verdict"),
            "score": b.get("score"),
            "peers_same_industry": peers,
            "tags": tags,
            "reasons": reasons,
        })

    # 统计聚合
    by_bucket = {}
    for r in rows:
        by_bucket[r["seal_bucket"]] = by_bucket.get(r["seal_bucket"], 0) + 1
    by_lb = {}
    for r in rows:
        by_lb[r["lianban"]] = by_lb.get(r["lianban"], 0) + 1
    by_ind = {}
    for r in rows:
        if r["industry"]:
            by_ind[r["industry"]] = by_ind.get(r["industry"], 0) + 1
    # 价格带分布 (低位首板策略的审美观): 解读一的「22元以下八成」等价物。
    price_bands = {"lt10": 0, "b10_22": 0, "b22_30": 0, "ge30": 0}
    low_price = 0
    for r in rows:
        p = r.get("price")
        try:
            p = float(p) if p is not None else None
        except (TypeError, ValueError):
            p = None
        if p is None or p <= 0:
            continue
        if p < 10:
            price_bands["lt10"] += 1
        elif p < 22:
            price_bands["b10_22"] += 1
        elif p < 30:
            price_bands["b22_30"] += 1
        else:
            price_bands["ge30"] += 1
        if p < 22:
            low_price += 1
    low_price_pct = round(low_price / max(1, len(rows)) * 100, 1)

    return rows, {
        "n_zt": len(rows),
        "by_seal_bucket": by_bucket,
        "by_lianban": by_lb,
        "by_industry": dict(sorted(by_ind.items(), key=lambda x: -x[1])[:10]),
        "by_price_band": price_bands,
        "low_price_lt22_pct": low_price_pct,
    }


def forecast(pool, attrib_rows, latest, mn=None, search_idx=None):
    """用归因出的特征反向筛「下一个涨停」候选。

    数据源说明: enrich.ths_strong 常为空 (独立文件, 被远端 sync 冲掉过),
    market_now.ths_strong 只有 code/name/reason, 没有 industry 和 price。
    所以行业从涨停池归因结果反查, 价格从 search_index 补。
    """
    mn = mn or {}
    search_idx = search_idx or {}
    # 今日已涨停的行业 → 板块效应候选扩散方向
    hot_inds = {}
    for r in attrib_rows:
        if r["industry"]:
            hot_inds[r["industry"]] = hot_inds.get(r["industry"], 0) + 1
    hot_inds = {k: v for k, v in hot_inds.items() if v >= 2}

    # 个股 → 行业 (从本轮涨停池 + 全市场行业映射反查)
    code2ind = {}
    for r in attrib_rows:
        code2ind[r["code"]] = r["industry"]

    # 竞价封板占比 → 抢筹意愿
    auction_n = sum(1 for r in attrib_rows if r["seal_bucket"] == "auction")
    total = max(1, len(attrib_rows))

    def price_of(code):
        for pre in ("sz", "sh", "bj"):
            v = search_idx.get(pre + code)
            if v and v.get("p") is not None:
                return v["p"], v.get("chg"), v.get("n")
        return None, None, None

    cands = []
    zt_codes = {r["code"] for r in attrib_rows}

    def add(code, name, reason, industry):
        code = str(code or "")
        if not code or code in zt_codes:
            return
        price, chg, nm = price_of(code)
        if not name:
            name = nm or ""
        price, chg, nm = price_of(code)
        genes, score = [], 0.0
        if industry in hot_inds:
            genes.append("所属 %s 今日 %d 只涨停 (板块效应)" % (industry, hot_inds[industry]))
            score += 25
        if chg is not None and 5 <= chg < 9.8:
            genes.append("已涨 %.2f%% 未封板 (半路候选)" % chg)
            score += 25
        elif chg is not None and 0 < chg < 5:
            genes.append("已涨 %.2f%% (板块内跟风候选)" % chg)
            score += 12
        if reason:
            genes.append("题材: %s" % str(reason)[:34])
            score += 15
        if score >= 30:
            cands.append({"code": code, "name": name or "", "price": price,
                          "chg_pct": chg, "industry": industry,
                          "genes": genes, "score": round(score, 1)})

    # 候选 1: 同花顺强势股 (有题材归因)
    for s in (mn.get("ths_strong") or []):
        reason = s.get("reason") or ""
        ind = ""
        for k in hot_inds:
            if k and k in reason:
                ind = k
                break
        add(s.get("code"), s.get("name"), reason, ind)

    # 候选 2: 同行业未涨停股 (从东财行业映射反查, 按行业涨停家数取前几个)
    ind_map = (load("industry_map_em.json") or {}).get("map") or {}
    ind_stocks = {}
    for code, ind in ind_map.items():
        if ind in hot_inds:
            ind_stocks.setdefault(ind, []).append(code)
    seen = {c["code"] for c in cands}
    for ind, codes in ind_stocks.items():
        for code in codes[:12]:
            if code in seen or code in zt_codes:
                continue
            seen.add(code)
            add(code, "", "", ind)

    cands.sort(key=lambda x: -x["score"])
    return {
        "n_candidates": len(cands),
        "hot_industries": hot_inds,
        "auction_ratio": round(auction_n / total * 100, 1),
        "note": "竞价封板占比 %.1f%% — 越高说明资金抢筹意愿越强, 板块效应易延续" % (
            auction_n / total * 100),
        "candidates": cands[:15],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-attr", default=os.path.join(DATA_DIR, "limitup_attribution.json"))
    ap.add_argument("--out-fc", default=os.path.join(DATA_DIR, "limitup_forecast.json"))
    args = ap.parse_args()

    mn = load("market_now.json")
    pool = mn.get("limit_up_pool") or []
    if not pool:
        print("[limitup] 无涨停池数据, 终止")
        return 1

    rows, agg = attribute(pool, load("ban_pick.json"), load("enrich.json"),
                          load("hot_news.json"))
    fc = forecast(pool, rows, load("latest.json"), mn, load("search_index.json"))

    now = datetime.now(CST)
    attr = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "limitup_attribution v1 (涨停归因)",
        "date": mn.get("date"),
        "n_zt": len(rows),
        "aggregate": agg,
        "dims": ["连板高度", "封板时点", "封板质量", "题材归因", "位置", "板块协同"],
        "caveat": "归因基于规则与可得字段, 非涨停真实原因披露; Research reference only",
        "items": rows,
    }
    fc_out = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "limitup_forecast v1 (涨停基因反向筛选)",
        "date": mn.get("date"),
        **fc,
        "caveat": "涨停不可精确预测; 本清单基于板块效应与量价特征, 仅供研究参考",
    }

    for p, d in ((args.out_attr, attr), (args.out_fc, fc_out)):
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        print("[limitup] -> %s" % p)

    print("[limitup] 涨停 %d 只 | 竞价封板 %d (%.0f%%) | 板块: %s" % (
        len(rows), agg["by_seal_bucket"].get("auction", 0),
        agg["by_seal_bucket"].get("auction", 0) / max(1, len(rows)) * 100,
        ", ".join("%s×%d" % kv for kv in list(agg["by_industry"].items())[:4])))
    print("[limitup] 预测候选 %d 只" % fc["n_candidates"])
    for c in fc["candidates"][:5]:
        print("   %s %s (%.0f分) %s" % (c["code"], c["name"], c["score"],
                                        "; ".join(c["genes"][:2])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
