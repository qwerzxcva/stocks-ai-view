#!/usr/bin/env python3
"""
auction_engine.py — 竞价抢筹与日内资金结构引擎

背景 (我们此前的缺口):
  premarket_picks 的「竞价抢筹」只有一个 zt_time 字段 (09:25:00 之类),
  无法区分「真抢筹」与「一字板无对手盘」。ban_prob 的封单强度只能估算。
  fetchers_ticks.py 落地后, 09:25 那一笔就是开盘集合竞价的撮合结果
  (价格/量/额), 连续竞价段的主动买卖方向也齐了, 可以真正量化。

## 四个指标 (全部由逐笔算出, 不用估算)

1. **竞价抢筹强度 auction_strength** (0-100)
   竞价撮合额 / 近 5 日竞价撮合额均值 → 看今日竞价是否异常放量。
   放量 3 倍以上且价格高开, 才是真抢筹; 一字板无量竞价不算。

2. **主动买卖失衡 buy_sell_imbalance** (-100 ~ +100)
   (主动买额 - 主动卖额) / (主动买额 + 主动卖额)。
   涨停股若收盘时仍强正, 说明封板后仍有持续主动买入 (真实资金);
   若转负, 说明封板是靠挂单而非主动成交 (易炸板)。

3. **大单集中度 large_tick_ratio** (0-100)
   单笔 >= 50 万元的成交额占比。占比高 = 机构/大户主导;
   全小单 = 散户行情, 持续性差。

4. **尾盘异动 tail_anomaly** (标量 + 方向)
   14:30 后成交额占全天比例, 及尾盘主动买卖失衡。
   尾盘放量主动买 = 资金抢次日溢价; 尾盘放量主动卖 = 出货。

## 数据来源
  data/ticks_<date>.json (fetchers_ticks.py 产出)
  data/latest.json      (涨停池, 用于标注哪些是涨停股)
  data/enrich.json      (ths_strong, 龙虎榜, 补充交叉验证)

输出: data/auction_engine.json
用法:
  python3 auction_engine.py
  python3 auction_engine.py --ticks data/ticks_20261015.json
"""
import argparse
import glob
import json
import math
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA
except Exception:
    AI_VIEW_DATA = os.path.join(os.path.dirname(HERE), "data")

LARGE_TICK_AMT = 500000.0     # 单笔 >= 50 万算大单
TAIL_START = "14:30:00"
SESSION_END = "15:00:59"
HIST_DIR = "ticks"            # 历史逐笔目录 (若存在则算 5 日均值)


def load(name):
    p = os.path.join(AI_VIEW_DATA, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def load_ticks(path=None):
    """读逐笔文件。优先 --ticks 指定, 否则取 data/ticks_*.json 里最新的。"""
    if path:
        p = path if os.path.isabs(path) else os.path.join(AI_VIEW_DATA, path)
        if not os.path.exists(p):
            p = path
        return json.load(open(p, encoding="utf-8"))
    files = sorted(glob.glob(os.path.join(AI_VIEW_DATA, "ticks_*.json")))
    if not files:
        return None
    return json.load(open(files[-1], encoding="utf-8"))


def auction_baseline(code, days=5):
    """近 N 日竞价撮合额均值 (用于判断今日竞价是否异常放量)。

    历史逐笔默认不落盘 (每只 4500+ 笔, 全市场太大), 所以:
      - 有 HIST_DIR 历史 → 用真实历史
      - 没有 → 返回 None, 强度分改用「竞价额占当日成交额比例」的相对口径
    """
    hd = os.path.join(AI_VIEW_DATA, HIST_DIR)
    if not os.path.isdir(hd):
        return None
    vals = []
    for f in sorted(glob.glob(os.path.join(hd, f"ticks_{code}_*.json")))[-days:]:
        try:
            d = json.load(open(f, encoding="utf-8"))
            vals.append(d.get("auction_amount") or 0)
        except Exception:
            continue
    return sum(vals) / len(vals) if vals else None


def _large_threshold(price):
    """大单阈值按价格分层。

    分笔是约 3 秒一笔, 高价股 (茅台 1300 元) 单笔 40 手就 50 万,
    低价股 (3 元) 要 16 万手才到 50 万 —— 固定 50 万会让高价股全是大单
    (实测茅台 95.4%)、低价股永远没有大单, 指标失去区分度。
    按「单笔成交额 / 当日价格」折算成手数, 阈值取 200 手 (约 20 万元的
    低价股 / 26 万元的高价股都在同一量级)。
    """
    if not price or price <= 0:
        return LARGE_TICK_AMT
    return max(200.0 * price, 100000.0)     # 下限 10 万, 避免极低价股阈值过小
def analyze(item):
    """单只股票的逐笔 → 四指标 dict。

    两种输入形态:
      A) 完整逐笔 (item["ticks"] 非空) — 本地调试或未加 --summary-only
      B) 仅汇总 (fetchers_ticks 用 --summary-only 输出, ticks 已剔除)
         CI 用这个形态 (36 只全采时文件会很大), 此时 buy/sell/auction_amount
         等字段 fetchers_ticks 已算好, 直接用; 只有 large_tick_ratio 和
         tail_* 无法从汇总推导, 置 None 并在 meta 说明。
    """
    code = item.get("code")
    ticks = item.get("ticks") or []
    summary_only = not ticks

    if summary_only:
        buy = item.get("buy_amount") or 0
        sell = item.get("sell_amount") or 0
        total = item.get("session_amount") or 0
        active = buy + sell
        imbalance = round((buy - sell) / active * 100, 2) if active else None
        auction_amt = item.get("auction_amount") or 0
        session = None
    else:
        session = [t for t in ticks if t["time"] <= SESSION_END]
        if not session:
            return None
    if summary_only:
        # 汇总形态: 大单/尾盘需逐笔, 无法从汇总推导 → None
        large_ratio = None
        thr = None
        avg_px = None
        tail_ratio = None
        tail_imb = None
    else:
        tot_vol = sum(t["volume"] or 0 for t in session)
        tot_amt = sum(t["amount"] or 0 for t in session)
        avg_px = (tot_amt / tot_vol) if tot_vol else (session[0].get("price") or 0)
        thr = _large_threshold(avg_px)

        buy = sum(t["amount"] for t in session if t["side"] == "B")
        sell = sum(t["amount"] for t in session if t["side"] == "S")
        active = buy + sell
        imbalance = round((buy - sell) / active * 100, 2) if active else None

        large = sum(t["amount"] for t in session if (t["amount"] or 0) >= thr)
        total = sum(t["amount"] for t in session)
        large_ratio = round(large / total * 100, 2) if total else None

        tail = [t for t in session if t["time"] >= TAIL_START]
        tail_amt = sum(t["amount"] for t in tail)
        tail_ratio = round(tail_amt / total * 100, 2) if total else None
        tail_buy = sum(t["amount"] for t in tail if t["side"] == "B")
        tail_sell = sum(t["amount"] for t in tail if t["side"] == "S")
        tail_active = tail_buy + tail_sell
        tail_imb = (round((tail_buy - tail_sell) / tail_active * 100, 2)
                    if tail_active else None)

    auction = item.get("auction") or []
    auction_px = auction[0]["price"] if auction else None
    base = auction_baseline(code)
    if base and base > 0:
        # 有历史: 用放量倍数
        strength = min(100.0, auction_amt / base * 25.0)
        strength_basis = f"竞价额为5日均值的{auction_amt / base:.1f}倍"
    elif total > 0:
        # 无历史: 用竞价额占当日成交额比例 (正常 0.5%~3%, 抢筹可到 10%+)
        #  映射 0→0 分, 3%→60 分, 10%+→100 分 (对数式, 避免多只并列 100 失去排序意义)
        ratio = auction_amt / total * 100
        strength = min(100.0, max(0.0, math.log(ratio * 100 + 1) / math.log(1100) * 100))
        strength_basis = f"竞价额占当日成交额{ratio:.2f}% (无历史均值, 用相对口径)"
    else:
        strength, strength_basis = 0.0, "无成交额数据"

    return {
        "code": code,
        "date": item.get("date"),
        "n_ticks": item.get("n_ticks"),
        "summary_only": summary_only,
        "auction_price": auction_px,
        "auction_amount": round(auction_amt, 0),
        "auction_strength": round(strength, 1),
        "auction_basis": strength_basis,
        "buy_amount": round(buy, 0),
        "sell_amount": round(sell, 0),
        "buy_sell_imbalance": imbalance,
        "large_tick_ratio": large_ratio,
        "large_tick_threshold": round(thr, 0) if thr else None,
        "avg_price": round(avg_px, 2) if avg_px else None,
        "tail_ratio": tail_ratio,
        "tail_imbalance": tail_imb,
        "amount_matches": item.get("amount_matches"),
        "complete": item.get("complete"),
        "session_amount": item.get("session_amount"),
        "snapshot_amount": item.get("snapshot_amount"),
    }


def verdict(row, is_zt):
    """一段可读结论: 这个票的资金结构是真实的还是虚的。"""
    c = row["code"]
    bits = []
    if row["auction_strength"] >= 70:
        bits.append(f"竞价异常放量({row['auction_basis']})")
    elif row["auction_strength"] >= 40:
        bits.append("竞价温和放量")
    else:
        bits.append("竞价平淡")

    imb = row["buy_sell_imbalance"]
    if imb is not None:
        if imb >= 15:
            bits.append(f"全天主动买占优({imb:+.1f}%)")
        elif imb <= -15:
            bits.append(f"全天主动卖占优({imb:+.1f}%)")
        else:
            bits.append(f"主动买卖均衡({imb:+.1f}%)")

    if row["large_tick_ratio"] is not None:
        if row["large_tick_ratio"] >= 25:
            bits.append(f"大单主导({row['large_tick_ratio']:.1f}%)")
        elif row["large_tick_ratio"] <= 8:
            bits.append(f"散户行情(大单仅{row['large_tick_ratio']:.1f}%)")

    if row["tail_ratio"] is not None and row["tail_ratio"] >= 12:
        d = "主动买" if (row["tail_imbalance"] or 0) > 0 else "主动卖"
        bits.append(f"尾盘放量({row['tail_ratio']:.1f}%, {d}{row['tail_imbalance']:+.1f}%)")

    if is_zt:
        if imb is not None and imb >= 10:
            act = "涨停且主动买持续 → 封板真实, 次日溢价概率较高"
        elif imb is not None and imb <= -5:
            act = "涨停但主动卖占优 → 封板靠挂单, 炸板风险高"
        else:
            act = "涨停但主动买卖均衡 → 封板质量一般, 观察次日竞价"
    else:
        if imb is not None and imb >= 15 and row["auction_strength"] >= 50:
            act = "竞价放量 + 主动买占优 → 资金真在进, 可关注"
        elif imb is not None and imb <= -15:
            act = "主动卖占优 → 资金在撤, 回避"
        else:
            act = "资金结构无显著倾向"

    warn = []
    if row["complete"] is False:
        warn.append("盘后段有缺笔, 尾盘指标可能偏低")
    if row["amount_matches"] is False:
        warn.append("连续竞价段成交额与快照不符, 数据可能不全")

    return " / ".join(bits) + "。操作: " + act + "." + \
           ((" 注意: " + "; ".join(warn) + "。") if warn else "")


def main():
    ap = argparse.ArgumentParser(description="竞价抢筹与日内资金结构引擎")
    ap.add_argument("--ticks", help="逐笔文件路径 (默认取最新的 data/ticks_*.json)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    now = datetime.now(CST)
    td = load_ticks(args.ticks)
    if not td:
        print("[auction] 没有逐笔数据, 先跑 fetchers_ticks.py", file=sys.stderr)
        sys.exit(1)

    latest = load("latest.json") or {}
    zt_codes = {x.get("code") for x in (latest.get("limit_up_pool") or [])}
    # 代码 → 名称 (涨停池 + 强势股 + latest 快照三处合并, 前端表格要显示)
    name_of = {}
    for x in (latest.get("limit_up_pool") or []):
        if x.get("code") and x.get("name"):
            name_of[str(x["code"]).zfill(6)] = x["name"]
    for x in (latest.get("ths_strong") or []):
        if x.get("code") and x.get("name"):
            name_of[str(x["code"]).zfill(6)] = x["name"]
    enrich = load("enrich.json") or {}
    for x in (enrich.get("ths_strong") or []):
        if x.get("code") and x.get("name"):
            name_of[str(x["code"]).zfill(6)] = x["name"]

    rows = []
    for it in td.get("items") or []:
        r = analyze(it)
        if not r:
            continue
        r["is_limit_up"] = r["code"][2:] in zt_codes
        r["name"] = name_of.get(str(r["code"][2:]).zfill(6), "")
        r["verdict"] = verdict(r, r["is_limit_up"])
        rows.append(r)

    rows.sort(key=lambda x: -(x["auction_strength"] or 0))
    n_zt = sum(1 for r in rows if r["is_limit_up"])
    strong = [r for r in rows if (r["auction_strength"] or 0) >= 70]

    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "date": td.get("items", [{}])[0].get("date") if td.get("items") else None,
        "engine": "auction_engine.py",
        "source": "fetchers_ticks.py (腾讯成交明细, 分笔)",
        "n_analyzed": len(rows),
        "n_limit_up": n_zt,
        "n_auction_strong": len(strong),
        "stocks": rows,
        "meta": {
            "metrics": {
                "auction_strength": "竞价撮合额强度 0-100; 有5日历史用放量倍数, "
                                    "否则用竞价额占当日成交额比例 (相对口径)",
                "buy_sell_imbalance": "(主动买额-主动卖额)/(主动买额+主动卖额)*100, -100~+100",
                "large_tick_ratio": f"单笔>={int(LARGE_TICK_AMT / 10000)}万元的成交额占比",
                "tail_ratio": "14:30后成交额占全天比例",
                "tail_imbalance": "尾盘主动买卖失衡",
            },
            "caliber": "分笔数据 (约3秒一笔), 非Level-2逐笔委托; 09:25那笔=开盘竞价撮合",
            "large_tick_threshold": LARGE_TICK_AMT,
            "caveat": "只覆盖采集时传入的股票 (全市场4500+只 x 4500笔太大, 建议按需取); "
                      "无历史逐笔时 auction_strength 用相对口径 (竞价额占当日成交额, "
                      "对数映射 3%->60分/10%+->100分), 跨日不可比; "
                      "complete=False 表示盘后段有缺笔, 尾盘指标偏低; "
                      "summary_only=True 表示逐笔明细未落盘 (CI 为省空间用 --summary-only), "
                      "此时 large_tick_ratio 与 tail_* 无法从汇总推导, 置 None",
            "next_step": "接入 ban_prob (用 buy_sell_imbalance 替代封单强度估算) 与 "
                         "premarket_picks (用 auction_strength 替代 zt_time 粗略判断)",
        },
    }

    out = args.out or os.path.join(AI_VIEW_DATA, "auction_engine.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    print(f"[auction] {len(rows)} 只 (涨停 {n_zt}, 竞价强 {len(strong)})")
    for r in rows[:6]:
        lr = f"{r['large_tick_ratio']:5.1f}%" if r["large_tick_ratio"] is not None else "   n/a"
        print(f"  {r['code']} 竞价强度{r['auction_strength']:5.1f} "
              f"失衡{r['buy_sell_imbalance']:+6.1f}% 大单{lr} "
              f"{'[涨停]' if r['is_limit_up'] else ''}{'[summary]' if r.get('summary_only') else ''} "
              f"| {r['verdict'][:64]}")
    print(f"→ {out}")


if __name__ == "__main__":
    main()
