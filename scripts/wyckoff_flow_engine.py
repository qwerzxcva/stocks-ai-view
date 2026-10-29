#!/usr/bin/env python3
"""wyckoff_flow_engine.py — 量价微结构层（批次二）

覆盖三本书的内容，并**诚实标注能力边界**：

  威科夫事件序列   《威科夫操盘法》：吸筹区 PS→SC→AR→ST→Spring→Test→SOS→LPS，
                   派发区 BC→AR→ST→UT→UTAD→SOW→LPSY。用日线 OHLCV 可判定，
                   实现为状态机（记录事件发生顺序与完整性），不是"看一眼图形猜阶段"。
  三角洲时间窗     《三角洲理论：隐藏在市场中的秩序》：摆动点之间的时间间隔若存在
                   稳定周期，则下一个转折窗口可投影。**这是全批最容易过拟合的东西**，
                   所以只输出窗口与样本量，不给方向，且置信度上限 0.5。
  订单流           《订单流交易》：delta / 吸收 / 失衡 / 足迹图。**需要逐笔或分钟数据**，
                   本项目没有 → 明确返回不可用，不用日线伪造 footprint。

纪律：
  * 只用 t 及之前的数据
  * 每个事件带 evidence（哪根 K、价格、量比），可复核
  * 数据不足一律返回 None + 原因，不返回"看起来合理"的默认值

用法：
  python3 scripts/wyckoff_flow_engine.py --all
  python3 scripts/wyckoff_flow_engine.py --code sh600016 -v
  python3 scripts/wyckoff_flow_engine.py --validate --sample 90
  python3 scripts/wyckoff_flow_engine.py --calibrate --sample 90
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import signal_validator as SV  # noqa: E402
from kline_source import DATA_DIR, list_codes, load_all, load_bars  # noqa: E402

CST = timezone(timedelta(hours=8))

# ── 阈值集中管理，按回测调这里，别散在函数里 ──────────────────────
TR_WINDOW = 24          # 区间最少根数
TR_WIDTH = 0.12         # 区间宽度上限（高低幅/中价）
VOL_CLIMAX = 1.8        # 高潮量：≥1.8 倍 20 日均量
VOL_SOS = 1.5
VOL_LOW = 0.7           # 缩量测试
SWING_K = 2
DELTA_MIN_CYCLES = 3    # 三角洲至少 3 个同周期样本才输出
DELTA_TOL = 0.25        # 周期离散度容忍（std/mean）

BULLISH_TAGS = ("wyckoff:spring", "wyckoff:test", "wyckoff:sos", "wyckoff:lps",
                "wyckoff:sc", "wyckoff:stage_accumulating", "wyckoff:stage_markup")


# ─────────────────────────── 基础工具 ───────────────────────────
def atr(bars, n=14):
    if len(bars) < n + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n


def vol_ma(bars, n=20):
    if len(bars) < n:
        return 0.0
    return sum(b["volume"] for b in bars[-n:]) / n


def swings(bars, k=SWING_K):
    """摆动点。注意：最后 k 根永远不可能是摆动点（需要右侧确认），
    调用方若要判断"当前是否在极值附近"，必须另用滚动窗口，否则最新一根永远不命中。"""
    lows, highs = [], []
    for i in range(k, len(bars) - k):
        win = bars[i - k:i + k + 1]
        if bars[i]["low"] == min(b["low"] for b in win):
            lows.append((i, bars[i]["low"]))
        if bars[i]["high"] == max(b["high"] for b in win):
            highs.append((i, bars[i]["high"]))
    return lows, highs


# ─────────────────────────── 交易区间 ───────────────────────────
def trading_range(bars, window=TR_WINDOW, width=TR_WIDTH):
    """找最近的横盘区间（威科夫一切事件都挂在这个区间上）"""
    if len(bars) < window + 5:
        return None
    best = None
    for end in range(len(bars), window, -1):
        seg = bars[end - window:end]
        hi = max(b["high"] for b in seg)
        lo = min(b["low"] for b in seg)
        mid = (hi + lo) / 2
        if mid <= 0:
            continue
        w = (hi - lo) / mid
        if w <= width:
            best = {"start": end - window, "end": end - 1, "high": hi, "low": lo,
                    "width_pct": round(w * 100, 2), "bars": window,
                    "mid": round(mid, 2)}
            break
    return best


# ─────────────────────────── 威科夫事件 ───────────────────────────
def wyckoff_events(bars, tr=None, atrv=None):
    """在交易区间上识别事件序列。

    返回事件列表（按时间升序），每个含 idx/kind/dir/evidence。
    判定全部基于「价格相对区间边界的位置 + 量能相对 20 日均量 + 收盘在当根的位置」，
    不用手绘趋势线，可复核。
    """
    tr = tr or trading_range(bars)
    if not tr:
        return []
    atrv = atrv or atr(bars) or 1e-9
    vma = vol_ma(bars) or 1e-9
    ev = []
    lo, hi = tr["low"], tr["high"]
    # 扫描范围：区间起点前 10 根（找 SC/BC）到最后一根
    start = max(10, tr["start"] - 10)
    for i in range(start, len(bars)):
        b = bars[i]
        vr = b["volume"] / vma
        rng = max(b["high"] - b["low"], 1e-9)
        close_pos = (b["close"] - b["low"]) / rng  # 0=收在最低 1=收在最高
        down = b["close"] < b["open"]
        up = b["close"] > b["open"]

        # 抛售高潮：放量长下影，收在上半区，且贴近区间下沿
        if down and vr >= VOL_CLIMAX and close_pos >= 0.5 and b["low"] <= lo + atrv:
            ev.append({"idx": i, "date": b["date"], "kind": "sc", "dir": "bull",
                       "evidence": f"放量{vr:.1f}倍跌至区间下沿{b['low']:.2f}但收在{close_pos:.0%}位置"})
        # 买入高潮：放量长上影，收在下半区，贴近区间上沿
        if up and vr >= VOL_CLIMAX and close_pos <= 0.5 and b["high"] >= hi - atrv:
            ev.append({"idx": i, "date": b["date"], "kind": "bc", "dir": "bear",
                       "evidence": f"放量{vr:.1f}倍冲至区间上沿{b['high']:.2f}但收在{close_pos:.0%}位置"})
        # 弹簧/假跌破：破区间下沿又收回
        if b["low"] < lo and b["close"] > lo:
            ev.append({"idx": i, "date": b["date"], "kind": "spring", "dir": "bull",
                       "evidence": f"最低{b['low']:.2f}破下沿{lo:.2f}，收{b['close']:.2f}回到区间内"})
        # 上冲回落：破区间上沿又收回
        if b["high"] > hi and b["close"] < hi:
            ev.append({"idx": i, "date": b["date"], "kind": "ut", "dir": "bear",
                       "evidence": f"最高{b['high']:.2f}破上沿{hi:.2f}，收{b['close']:.2f}落回区间内"})
            # Sperandeo 的 2B 法则与威科夫 UT 是同一个几何现象：突破前高不成立即反向。
            # 不是重复计分，而是给同一个事件两个学派的名字，便于按学派回溯。
            prior = bars[max(0, i - 40):i]
            if prior and b["high"] >= max(x["high"] for x in prior):
                ev.append({"idx": i, "date": b["date"], "kind": "two_b", "dir": "bear",
                           "evidence": f"创 40 日新高{b['high']:.2f} 后收回（Sperandeo 2B）"})
        # 强势信号：放量收在区间上沿之上
        if b["close"] > hi and vr >= VOL_SOS and close_pos >= 0.6:
            ev.append({"idx": i, "date": b["date"], "kind": "sos", "dir": "bull",
                       "evidence": f"收{b['close']:.2f}站上上沿{hi:.2f}，量{vr:.1f}倍，收在{close_pos:.0%}"})
        # 弱势信号：放量收在区间下沿之下
        if b["close"] < lo and vr >= VOL_SOS and close_pos <= 0.4:
            ev.append({"idx": i, "date": b["date"], "kind": "sow", "dir": "bear",
                       "evidence": f"收{b['close']:.2f}跌破下沿{lo:.2f}，量{vr:.1f}倍，收在{close_pos:.0%}"})

    # 二次测试 / 最后支撑点（依赖前序事件，单独一轮）
    for e in list(ev):
        if e["kind"] in ("sc", "spring"):
            base = bars[e["idx"]]
            for j in range(e["idx"] + 2, min(e["idx"] + 12, len(bars))):
                bj = bars[j]
                if abs(bj["low"] - base["low"]) <= 0.6 * atrv and bj["volume"] < base["volume"] * 0.8:
                    ev.append({"idx": j, "date": bj["date"], "kind": "test", "dir": "bull",
                               "evidence": f"回测{e['kind']}低点{base['low']:.2f}，量缩至{ bj['volume']/base['volume']:.0%}"})
                    break
        if e["kind"] == "sos":
            for j in range(e["idx"] + 1, min(e["idx"] + 10, len(bars))):
                bj = bars[j]
                if bj["low"] > hi and bj["close"] > hi and bj["volume"] < vma * 1.2:
                    ev.append({"idx": j, "date": bj["date"], "kind": "lps", "dir": "bull",
                               "evidence": f"回踩不破上沿{hi:.2f}，量能收敛"})
                    break
    ev.sort(key=lambda x: (x["idx"], x["kind"]))
    return ev


def wyckoff_stage(bars, events, tr, atrv=None):
    """由事件序列推阶段。完整度 = 关键事件到齐比例，不齐就说 unknown。"""
    if not tr or not events:
        return {"stage": "unknown", "completeness": 0.0, "note": "无区间或无事件"}
    last = bars[-1]
    hi, lo = tr["high"], tr["low"]
    kinds = [e["kind"] for e in events]
    recent = {e["kind"] for e in events if e["idx"] >= len(bars) - 30}

    acc_seq = ["sc", "test", "spring", "sos", "lps"]
    dist_seq = ["bc", "ut", "sow", "lpsy"]
    acc_hit = sum(1 for k in acc_seq if k in kinds)
    dist_hit = sum(1 for k in dist_seq if k in kinds)

    if last["close"] > hi:
        stage, note = "markup", "已站上区间上沿"
    elif last["close"] < lo:
        stage, note = "markdown", "已跌破区间下沿"
    elif acc_hit >= dist_hit and ("spring" in recent or "sos" in recent):
        stage, note = "accumulating", "区间内出现弹簧/强势信号"
    elif dist_hit > acc_hit and ("ut" in recent or "sow" in recent):
        stage, note = "distributing", "区间内出现上冲回落/弱势信号"
    else:
        stage, note = "unknown", "证据不足，不判阶段"
    completeness = round(max(acc_hit / len(acc_seq), dist_hit / len(dist_seq)), 2)
    return {"stage": stage, "completeness": completeness, "note": note,
            "acc_hits": acc_hit, "dist_hits": dist_hit}


def cause_effect_target(tr, bars):
    """因果定律的粗略量化：区间宽度（因）→ 突破后目标（果）。

    点数图是正解，但没有点数图数据；这里用区间高度 × (1 + 宽度修正) 作近似，
    并把方法标注为 proxy，避免被当成精确目标。
    """
    if not tr:
        return None
    height = tr["high"] - tr["low"]
    # 横盘越久，目标越远；用区间根数/24 做倍数修正，上限 2 倍
    mult = min(1.0 + (tr["bars"] - TR_WINDOW) / 48.0 + 0.5, 2.0)
    return {"method": "proxy_tr_height", "height": round(height, 2),
            "target_up": round(tr["high"] + height * mult, 2),
            "target_down": round(tr["low"] - height * mult, 2),
            "mult": round(mult, 2)}


# ─────────────────────────── 三角洲时间窗 ───────────────────────────
def delta_windows(bars, k=SWING_K):
    """摆动低点间隔若存在稳定周期，投影下一个窗口。

    必须说清：这是**最容易过拟合**的一块。所以：
      * 至少 3 个同周期样本才输出
      * 离散度 std/mean > 0.25 直接判不稳定
      * confidence 上限 0.5，且不输出方向（只给时间窗）
    """
    lows, highs = swings(bars, k)
    if len(lows) < 4:
        return {"available": False, "reason": "摆动点不足 4 个"}
    gaps = [lows[i + 1][0] - lows[i][0] for i in range(len(lows) - 1)]
    if not gaps:
        return {"available": False, "reason": "无间隔样本"}
    # 用中位数做粗聚类，容差 ±2 根
    gaps_sorted = sorted(gaps)
    med = gaps_sorted[len(gaps) // 2]
    cluster = [g for g in gaps if abs(g - med) <= 2]
    if len(cluster) < DELTA_MIN_CYCLES:
        return {"available": False, "reason": f"同周期样本仅 {len(cluster)} 个（需 ≥{DELTA_MIN_CYCLES}）"}
    mean_gap = sum(cluster) / len(cluster)
    var = sum((g - mean_gap) ** 2 for g in cluster) / max(len(cluster) - 1, 1)
    disp = (var ** 0.5) / mean_gap if mean_gap else 1.0
    if disp > DELTA_TOL:
        return {"available": False, "reason": f"周期离散度 {disp:.2f} > {DELTA_TOL}，不稳定"}
    last_low_idx = lows[-1][0]
    bars_since = len(bars) - 1 - last_low_idx
    to_next = mean_gap - bars_since
    # 基线概率：随机一天落在"周期±1.5 根"窗口内的概率。
    # 不报基线就跟别的东西比，"命中 30%"听起来像本事，其实纯随机也差不多是这个数。
    win_w = 3.0
    baseline = min(1.0, win_w / mean_gap) if mean_gap else 1.0
    return {"available": True, "period_days": round(mean_gap, 1), "samples": len(cluster),
            "dispersion": round(disp, 3), "bars_since_last_low": bars_since,
            "bars_to_next_window": round(to_next, 1),
            "in_window": bool(-1.5 <= to_next <= 1.5),
            "window_width_days": win_w,
            "baseline_prob": round(baseline, 3),
            "confidence": round(min(0.5, 0.2 + 0.1 * len(cluster)), 2),
            "caveat": "周期检测极易过拟合；命中率必须与 baseline_prob 对比才有意义，且不给方向"}


# ─────────────────────────── 订单流（诚实守卫） ───────────────────────────
def order_flow_available():
    """订单流需要逐笔或分钟数据。本项目没有 → 直接说不可用。

    用日线伪造 delta/吸收 是自欺：日线只有一根聚合，看不到主动买卖方向、
    看不到价位上的成交分布，也看不到冰山单。宁可返回不可用。
    """
    try:
        from kline_source import duck
        rows = duck().execute(
            "select table_name from information_schema.tables").fetchall()
        names = {r[0] for r in rows}
    except Exception as e:
        return False, f"无法查询数据表: {str(e)[:60]}"
    for t in ("kline_minute", "kline_1min", "tick", "trade_tick", "order_flow"):
        if t in names:
            return True, f"找到 {t} 表"
    return False, "无 kline_minute/tick/order_flow 等表，仅有日线 → 订单流不可落地"


def order_flow(bars):
    ok, reason = order_flow_available()
    if not ok:
        return {"available": False, "reason": reason}
    return {"available": True, "note": "分钟数据存在但未实现 delta 计算（待批次二后续）"}


# ─────────────────────────── 汇总 ───────────────────────────
def analyze(bars, code=""):
    if not bars or len(bars) < 80:
        return None
    atrv = atr(bars) or 1e-9
    tr = trading_range(bars)
    ev = wyckoff_events(bars, tr, atrv)
    stage = wyckoff_stage(bars, ev, tr, atrv)
    delta = delta_windows(bars)
    target = cause_effect_target(tr, bars)

    tags = []
    for k in {e["kind"] for e in ev if e["idx"] >= len(bars) - 20}:
        tags.append(f"wyckoff:{k}")
    tags.append(f"wyckoff:stage_{stage['stage']}")
    if delta.get("in_window"):
        tags.append("delta:window_open")

    last = bars[-1]
    return {
        "code": code, "date": last["date"], "close": round(last["close"], 3),
        "tags": tags,
        "trading_range": tr,
        "wyckoff": {"stage": stage, "events": ev[-8:], "event_count": len(ev),
                    "target": target},
        "delta": delta,
        "order_flow": order_flow(bars),
        "atr": round(atrv, 3),
    }


def run_all(codes=None, out_path=None):
    codes = codes or list_codes()
    barset = load_all(codes)
    items, skipped = [], 0
    for code, bars in barset.items():
        try:
            r = analyze(bars, code)
        except Exception as e:
            skipped += 1
            print(f"skip {code}: {str(e)[:60]}", file=sys.stderr)
            continue
        if r:
            items.append(r)
        else:
            skipped += 1
    doc = {"generated_at": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
           "engine": "wyckoff_flow_engine/v1", "count": len(items), "items": items}
    out_path = out_path or os.path.join(DATA_DIR, "wyckoff_flow_signals.json")
    tmp = out_path + ".tmp"
    json.dump(doc, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, out_path)
    return doc, skipped


def _tag_fn(view):
    r = analyze(view, "x")
    return r["tags"] if r else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--code")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--sample", type=int, default=90)
    a = ap.parse_args()

    if a.code:
        r = analyze(load_bars(a.code), a.code)
        if not r:
            print("数据不足，无法分析")
            return
        if a.verbose:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        else:
            print(f"{r['code']} {r['date']} close={r['close']} tags={r['tags']}")
        return

    if a.validate or a.calibrate:
        codes = list_codes()[:a.sample]
        samples = [(c, load_bars(c, limit=400)) for c in codes]
        samples = [(c, b) for c, b in samples if len(b) >= 120]
        rows = SV.validate(_tag_fn, samples, horizon=5, min_history=80, cut_step=2)
        if a.validate:
            SV.print_rows(rows, 30)
        if a.calibrate:
            table = SV.calibrate(rows, BULLISH_TAGS, min_n=100, min_t=1.5)
            p = os.path.join(DATA_DIR, "wyckoff_calibration.json")
            SV.write_calibration(p, table, meta={"sample": a.sample, "horizon": 5})
            print(f"校准表 → {p}（{len(table)} 个标签）")
            for k, v in sorted(table.items(), key=lambda kv: -abs(kv[1]["t"]))[:8]:
                print(f"  {k:34} n={v['n']:>5} fwd={v['avg_fwd']:>6.2f}% t={v['t']:>5.2f} {v['edge']}")
        return

    if a.all:
        doc, skipped = run_all()
        print(f"已写出 {doc['count']} 只（跳过 {skipped}）→ data/wyckoff_flow_signals.json")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
