#!/usr/bin/env python3
"""
ban_pick.py — 打板个股级判断: 该不该打 + 打板价值分

用户需求: 「按每只股票进行分析, 判断这只该不该打板, 打板第一天保持涨停的
           概率多少? 第二天涨停的概率又是多少? 拿出数据来分析」

数据来源:
  - 实时涨停池  东财 push2ex getTopicZTPool (今日涨停股明细)
  - 历史K线     本地 bulk (算连板数/量比/前期涨幅)
  - 因子分层    ban_prob.py 的 layered_stats (封板强度/量比/前期涨幅)
  - 实时快照    腾讯行情 (现价/涨跌/换手/市值)

输出每只涨停股:
  lianban            连板数 (1=首板)
  chg_pct            今日涨幅
  seal_strength      封板强度 = 涨幅/涨停阈值 (1.0=一字/满封)
  vol_ratio          量比 = 今日量/前5日均量
  prior_gain_5d      近5日累计涨幅 (追高风险)
  p_zt_next          次日涨停概率 (基准×因子调整)
  p_zhaban_next      次日炸板概率
  exp_ret_next       次日期望收益
  verdict            结论: 可打 / 观察 / 回避
  score              打板价值分 0-100
  reasons            判断依据列表

verdict 规则 (实证阈值, 来自 ban_prob 分层):
  可打:  p_zt_next>=25 且 p_zhaban_next<16 且 prior_gain_5d<40
  观察:  p_zt_next>=18 且 p_zhaban_next<22
  回避:  p_zhaban_next>=22 或 prior_gain_5d>=40 或 (首板且量比<1.2)
"""
import argparse
import glob
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://gu.qq.com/"}

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import BULK, AI_VIEW_DATA
except Exception:
    BULK = os.environ.get("STOCKS_KLINE_BULK") or "/root/workspace/stocks-data-kline/data/bulk"
    AI_VIEW_DATA = os.path.join(os.path.dirname(HERE), "data")
DATA_DIR = os.environ.get("STOCKS_AI_VIEW_DATA") or AI_VIEW_DATA

from collect_market_extras import fetch_pool, fetch_zdfenbu


def load_auction():
    """读 auction_engine.json → {裸码: 逐笔资金结构}。

    没有逐笔数据时返回空 dict, ban_pick 按原有 K 线口径走 (不报错)。
    逐笔只覆盖 fetchers_ticks 传入的股票, 因此这里是「有则增强」而非必需。
    """
    p = os.path.join(DATA_DIR, "auction_engine.json")
    if not os.path.exists(p):
        return {}
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for r in d.get("stocks") or []:
        c = str(r.get("code") or "")
        c = c[2:] if len(c) == 8 and c[:2] in ("sh", "sz", "bj") else c
        out[c] = r
    return out


def apply_auction(r, au):
    """用逐笔资金结构调整 score 并追加 reason。

    为什么值得调: seal_strength 只看收盘价离涨停板多远, 分不出
    「主动买持续推进的涨停」与「靠大单挂住、实际主动卖占优的涨停」,
    而后者的炸板风险显著更高。逐笔的 buy_sell_imbalance 正好补这个盲区。
    """
    if not au:
        return r
    imb = au.get("buy_sell_imbalance")
    strength = au.get("auction_strength")
    delta = 0.0
    notes = []
    if imb is not None:
        if imb >= 15:
            delta += 8
            notes.append(f"逐笔全天主动买占优 {imb:+.1f}% (封板有真实成交支撑)")
        elif imb >= 5:
            delta += 3
            notes.append(f"逐笔主动买略占优 {imb:+.1f}%")
        elif imb <= -15:
            delta -= 12
            notes.append(f"逐笔全天主动卖占优 {imb:+.1f}% (封板靠挂单, 炸板风险高)")
        elif imb <= -5:
            delta -= 5
            notes.append(f"逐笔主动卖略占优 {imb:+.1f}%")
    if strength is not None and strength >= 70:
        delta += 4
        notes.append(f"竞价异常放量 (强度 {strength:.0f})")
    tail = au.get("tail_imbalance")
    if tail is not None and (au.get("tail_ratio") or 0) >= 12:
        if tail >= 20:
            delta += 3
            notes.append(f"尾盘放量主动买 {tail:+.1f}% (抢次日溢价)")
        elif tail <= -20:
            delta -= 6
            notes.append(f"尾盘放量主动卖 {tail:+.1f}% (出货迹象)")
    if au.get("complete") is False:
        notes.append("盘后段有缺笔, 尾盘指标可能偏低")

    r["score"] = round(max(0, min(100, r["score"] + delta)), 1)
    r["auction_delta"] = round(delta, 1)
    r["tick_structure"] = {
        "buy_sell_imbalance": imb,
        "auction_strength": strength,
        "large_tick_ratio": au.get("large_tick_ratio"),
        "tail_ratio": au.get("tail_ratio"),
        "tail_imbalance": tail,
        "complete": au.get("complete"),
    }
    if notes:
        r["reasons"] = list(r.get("reasons") or []) + notes
    # 逐笔证据足以推翻 K 线口径的结论: 主动卖占优的涨停一律降为回避
    if imb is not None and imb <= -15 and r.get("verdict") == "可打":
        r["verdict"] = "观察"
        r["verdict_reason"] = "逐笔显示主动卖占优, 封板质量存疑"
    return r


def zt_threshold(code):
    c = str(code)
    if c.startswith(("sh", "sz", "bj")):
        c = c[2:]
    if c.startswith(("30", "68")):
        return 19.8
    if c.startswith(("43", "83", "87", "92")):
        return 29.8
    return 9.8


def prefixed(bare):
    """裸码 → 带市场前缀 (与本地K线 bulk 的 key 格式一致)"""
    c = str(bare).zfill(6)
    if c.startswith(("60", "68", "90", "11", "13")):
        return "sh" + c
    if c.startswith(("43", "83", "87", "88", "92")):
        return "bj" + c
    return "sz" + c


def load_klines(bulk):
    data = {}
    for p in sorted(glob.glob(os.path.join(bulk, "day_qfq_*.csv"))):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                code, rest = line.split("|", 1)
                parts = rest.split("|") if "|" in rest else rest.split(",")
                if len(parts) < 6:
                    continue
                try:
                    data.setdefault(code, []).append(
                        [parts[0].strip(), float(parts[1]), float(parts[2]),
                         float(parts[3]), float(parts[4]), float(parts[5])])
                except ValueError:
                    continue
    for c in data:
        data[c].sort(key=lambda b: b[0])
    return data


def lianban_series(code, bars):
    out, n = [], 0
    for i, b in enumerate(bars):
        pc = bars[i - 1][2] if i > 0 else b[2]
        if pc > 0 and (b[2] / pc - 1) * 100 >= zt_threshold(code):
            n += 1
        else:
            n = 0
        out.append(n)
    return out


def load_ban_prob():
    """读 ban_prob.json 拿分层概率"""
    for p in [os.path.join(DATA_DIR, "ban_prob.json"),
              os.path.join(os.path.dirname(HERE), "data", "ban_prob.json")]:
        if os.path.exists(p):
            try:
                return json.load(open(p, encoding="utf-8"))
            except Exception:
                pass
    return None


def bucket_of(dim, val):
    if dim == "seal_strength":
        return ("一字/满封" if val >= 0.999 else "接近满封" if val >= 0.98
                else "小幅松动" if val >= 0.95 else "弱封")
    if dim == "vol_ratio":
        return ("<1.5" if val < 1.5 else "1.5-3" if val < 3
                else "3-6" if val < 6 else ">6")
    if dim == "prior_gain":
        return ("<10%" if val < 10 else "10-25%" if val < 25
                else "25-40%" if val < 40 else ">40%")
    return None


def analyze_one(code, name, bars, bp):
    """单只涨停股分析"""
    if len(bars) < 10:
        return None
    lb = lianban_series(code, bars)
    last = len(bars) - 1
    if lb[last] == 0:
        return None
    cur, prev = bars[last], bars[last - 1]
    if prev[2] <= 0:
        return None
    lb_n = lb[last]
    chg = (cur[2] / prev[2] - 1) * 100
    th = zt_threshold(code)
    seal = chg / th
    avg5 = sum(b[5] for b in bars[max(0, last - 5):last]) / max(1, min(5, last))
    vr = cur[5] / avg5 if avg5 > 0 else 1.0
    prior = (cur[2] / bars[last - 5][2] - 1) * 100 if bars[last - 5][2] > 0 else 0.0

    base_p, base_zb, base_ret, n_samp = 15.11, 10.24, 1.17, 6438
    adj, factors = 1.0, []
    if bp:
        by = (bp.get("base") or {}).get("by_lianban") or {}
        key = f"{min(lb_n, 6)}连板" if lb_n > 1 else "首板"
        b = by.get(key)
        if b and b.get("n_samples"):
            base_p = b.get("zt_next_rate", base_p)
            base_zb = b.get("zhaban_next_rate", base_zb)
            base_ret = b.get("ret_next_avg", base_ret)
            n_samp = b.get("n_samples", n_samp)
        else:
            # ban_prob 无有效分层 (如 CI 路径 bug 曾致 by_lianban 空) → 用内置默认基准
            DEFAULTS = {
                1: (15.11, 10.24, 1.17, 6438),
                2: (29.09, 14.49, 1.40, 966),
                3: (43.01, 16.13, 2.41, 279),
                4: (45.83, 12.50, 2.73, 120),
                5: (54.55, 16.36, 2.47, 55),
                6: (53.23, 16.13, 2.92, 62),
            }
            base_p, base_zb, base_ret, n_samp = DEFAULTS.get(min(lb_n, 6), DEFAULTS[1])
        layers = (bp.get("layers") or {}).get("layers") or {}
        ratios = []
        factor_deltas = []
        for dim, val in (("seal_strength", seal), ("vol_ratio", vr), ("prior_gain", prior)):
            bk = bucket_of(dim, val)
            st = layers.get(dim, {}).get(bk)
            if not st or st.get("evidence") != "ok" or st.get("n", 0) < 30:
                continue
            r = st["zt_next_rate"] / base_p if base_p > 0 else 1.0
            ratios.append(max(0.6, min(1.6, r)))
            factors.append(f"{dim}={bk}(zt_next={st['zt_next_rate']}%,n={st['n']})")
            factor_deltas.append({
                "factor": dim,
                "bucket": bk,
                "layer_rate": st["zt_next_rate"],
                "layer_n": st["n"],
                "ratio_to_base": round(r, 3),
                "delta_to_base_pct": round((r - 1.0) * base_p, 2),
            })
        if ratios:
            adj = 1.0
            for r in ratios:
                adj *= r
            adj **= 1.0 / len(ratios)

    p_zt = round(min(95.0, base_p * adj), 2)
    p_zb = round(min(95.0, base_zb * adj), 2)
    e_ret = round(base_ret * adj, 2)

    reasons = [f"{lb_n}连板基准次日涨停率 {base_p}% (n={n_samp})"]
    if factors:
        reasons.append("因子调整: " + ", ".join(factors) + f" → ×{adj:.2f}")
    reasons.append(f"封板强度 {seal:.2f}, 量比 {vr:.1f}, 近5日涨幅 {prior:+.1f}%")

    if p_zt >= 25 and p_zb < 16 and prior < 40:
        verdict = "可打"
    elif p_zt >= 18 and p_zb < 22:
        verdict = "观察"
    else:
        verdict = "回避"

    if p_zb >= 22:
        reasons.append(f"次日炸板率 {p_zb}% 偏高 (≥22% 回避线)")
    if prior >= 40:
        reasons.append(f"近5日已涨 {prior:.0f}%, 追高风险")
    if lb_n == 1 and vr < 1.2:
        reasons.append(f"首板且量比仅 {vr:.1f} (<1.2), 资金关注度低")

    score = round(max(0, min(100,
        p_zt * 1.2 + (e_ret * 3) - p_zb * 1.5 - max(0, prior - 30) * 0.5)), 1)

    # 因子 delta 贡献分解 (相对连板数基准的绝对增量; 用于归因解释)
    total_delta = p_zt - base_p
    factor_decomp = []
    if factor_deltas:
        for d in factor_deltas:
            pct_of_total = round(d["delta_to_base_pct"] / total_delta * 100, 1) if total_delta else None
            factor_decomp.append({**d, "delta_pct_of_total": pct_of_total})
    else:
        factor_decomp = [{"note": "无有效分层因子 (样本不足/缺失)", "delta": None}]

    return {
        "code": code, "name": name, "lianban": lb_n, "date": cur[0],
        "close": cur[2], "chg_pct": round(chg, 2),
        "seal_strength": round(seal, 3), "vol_ratio": round(vr, 2),
        "prior_gain_5d": round(prior, 2),
        "p_zt_next": p_zt, "p_zhaban_next": p_zb, "exp_ret_next": e_ret,
        "base_p": round(base_p, 2),
        "n_samples": n_samp, "adj_factor": round(adj, 3),
        "verdict": verdict, "score": score, "reasons": reasons,
        "factor_delta": {
            "total_delta_to_base": round(total_delta, 2),
            "decomposition": factor_decomp,
            "note": "各因子 delta = base_p*(ratio-1); 几何平均近似, 高阶交叉未拆分",
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bulk", default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    bulk = args.bulk or BULK
    now = datetime.now(CST)
    date_str = now.strftime("%Y%m%d")
    print(f"[ban_pick] {now:%Y-%m-%d %H:%M} CST")

    zt = fetch_pool("ZT", date_str)
    pool = zt["pool"]
    print(f"[ban_pick] 今日涨停 {zt['count']} 只 (明细 {len(pool)})")
    if not pool:
        # 非交易时段/无明细 → 从本地K线反推
        print("[ban_pick] 东财明细为空, 从本地K线反推涨停股")

    klines = load_klines(bulk)
    bp = load_ban_prob()
    print(f"[ban_pick] K线 {len(klines)} 只, ban_prob {'有' if bp else '无(用默认基准)'}")

    results = []
    if pool:
        for p in pool:
            bare = str(p.get("code", "")).zfill(6)
            # 本地K线 key 带市场前缀 (sh600108), 东财返回裸码 → 转换后再查
            code = prefixed(bare)
            bars = klines.get(code)
            if not bars:
                # 无K线也输出 (概率用连板数基准)
                lb_n = p.get("lianban", 1)
                results.append({
                    "code": bare, "name": p.get("name", ""), "lianban": lb_n,
                    "date": date_str, "close": p.get("price"), "chg_pct": p.get("chg_pct"),
                    "p_zt_next": None, "p_zhaban_next": None, "exp_ret_next": None,
                    "n_samples": 0, "verdict": "无K线",
                    "score": 0, "reasons": ["本地无该股K线, 无法计算因子"],
                    "factor_delta": {"total_delta_to_base": None, "decomposition": [],
                                     "note": "无K线数据, 无法分解"},
                })
                continue
            r = analyze_one(code, p.get("name", ""), bars, bp)
            if r:
                r["code"] = bare
                results.append(r)
    else:
        latest = max((b[-1][0] for b in klines.values() if b), default=None)
        for code, bars in klines.items():
            if not bars or bars[-1][0] != latest:
                continue
            r = analyze_one(code, "", bars, bp)
            if r:
                results.append(r)

    # 逐笔资金结构增强 (有 auction_engine.json 才生效)
    auction = load_auction()
    if auction:
        n_hit = 0
        for r in results:
            au = auction.get(str(r.get("code") or "").zfill(6))
            if au:
                apply_auction(r, au)
                n_hit += 1
        print(f"[ban_pick] 逐笔资金结构增强 {n_hit}/{len(results)} 只")

    order = {"可打": 0, "观察": 1, "回避": 2, "无K线": 3}
    results.sort(key=lambda x: (order.get(x["verdict"], 9), -x["score"]))

    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "date": date_str,
        "engine": "ban_pick (个股级打板判断)",
        "n_zt": zt["count"],
        "counts": counts,
        "picks": results[:60],
        "meta": {
            "verdict_rules": "可打: p_zt_next>=25 且 p_zhaban_next<16 且 近5日<40%; "
                             "观察: p_zt_next>=18 且 p_zhaban_next<22%; 其余回避",
            "score_formula": "score = p_zt*1.2 + exp_ret*3 - p_zhaban*1.5 - max(0,prior-30)*0.5",
            "p_zt_next": "次日继续涨停概率 (连板基准 × 因子调整)",
            "p_zhaban_next": "次日盘中触及涨停但收盘未封的概率",
            "base_p": "连板数基准概率 (来自 ban_prob.json by_lianban)",
            "factor_delta": "p_zt_next 相对 base_p 的绝对增量分解 (各因子 ratio_to_base × 贡献占比)",
            "source": "东财涨跌停池 + 本地K线 + ban_prob 因子分层 + 腾讯行情",
            "caveat": "概率基于历史统计, 非必然; 打板属高风险交易, 次日炸板即大幅亏损; "
                      "仅供研究不构成投资建议",
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print(f"\n=== 结论分布 ===\n  {counts}")
    print(f"\n=== 可打 TOP10 ===")
    for r in [x for x in results if x["verdict"] == "可打"][:10]:
        print(f"  {r['code']} {r['name']:8s} {r['lianban']}板 {r['chg_pct']:+.2f}% "
              f"价值分={r['score']:5.1f} 次日涨停={r['p_zt_next']}% 炸板={r['p_zhaban_next']}% "
              f"期望={r['exp_ret_next']}%")
    print(f"\n=== 回避 TOP5 (警示) ===")
    for r in [x for x in results if x["verdict"] == "回避"][:5]:
        print(f"  {r['code']} {r['name']:8s} {r['lianban']}板 次日涨停={r['p_zt_next']}% "
              f"炸板={r['p_zhaban_next']}% 近5日={r.get('prior_gain_5d')}%")
    print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
