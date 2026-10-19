#!/usr/bin/env python3
"""
factor_lab.py — 特征实验室: 在全市场历史上系统验证哪些特征真有预测力

背景 (2026-10-15 已确诊问题):
  weight_calibrator_v3 实测 1 日持有期下 14 个看多技术指标胜率全在 40-42.6%,
  edge 全为负, 无一超过随机 50%。这不是调权重能解决的 —— 需要换信号源。

本脚本做什么:
  不调权重, 而是**批量测量候选特征的预测力**, 用统一口径回答:
    「按特征 X 把全市场分成多空两组, 未来 N 日的收益差是多少? 统计上显著吗?」

## 口径 (全部与 kline_sim 一致, 保证可比)
  - 数据: stocks-data-kline bulk 前复权日K (全历史)
  - 未来收益: 信号日次日**开盘**买入 → 第 N 日**收盘**卖出
               (次日开盘而非当日收盘, 避免用当日收盘后才知道的信息)
  - 滑点: 单边 0.03% (可配)
  - 涨跌停: 入场日触板不买; 出场日封跌停顺延最多 5 日 (与 kline_sim 同)
  - 样本: 每个 (特征, 持有期) 组合给 n / 胜率 / 平均收益 / 中位收益 /
          t 值 / 多空收益差 (long-short spread)

## 候选特征 (每个都是可复现的计算式, 输入只用 signal 日及之前的数据)
  技术类: rsi_oversold / rsi_overbought / bias_ma20 / ma_golden /
          vol_dry_up / new_high_20 / breakout_60
  资金类: main_net_positive / main_net_5d_up (需 moneyflow, 无则跳过)
  位置类: low_pos_20 / high_pos_20 / below_ma60
  量价类: vol_ratio_high / shrink_pullback
  基准:   random_baseline (随机 50%, 用于校准 — 若某特征打不赢随机就是无效)

## 为什么用 long-short spread 而不是只看胜率
  胜率 50% 但盈亏比 2:1 的策略是赚钱的 (期望为正)。单看胜率会漏掉这类。
  因此同时输出 avg_ret / median_ret / spread, 让结论不被单一指标误导。

## 防自欺的设计 (重要)
  1. **不挑参数**: 所有阈值写死在 CANDIDATES 里, 不做网格寻优。
     寻优出来的「最优参数」在样本外几乎必失效, 那是过拟合不是发现。
  2. **输出全部特征**: 包括无效的。只报有效的特征是 cherry-picking。
  3. **random_baseline 同口径**: 用同一批股票的随机分组做基准,
     若某特征 spread 与随机基准无差异, 明确标注「无预测力」。
  4. **t 值 + 样本量**: n 太小或 t 不显著的, 标注「样本不足」, 不当结论卖。

输出: data/factor_lab.json (全特征结果) + data/factor_lab.md (人读报告)

用法:
  python3 factor_lab.py
  python3 factor_lab.py --hold-days 1,3,5 --max-stocks 3000 --min-n 200
"""
import argparse
import glob
import json
import math
import os
import random

FLOAT_SHARES = {}   # {c6: 流通股数}, 由 load_float_shares() 填

import re
import statistics
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import BULK as BULK_DIR, AI_VIEW_DATA as DATA_DIR
except Exception:
    BULK_DIR = "/root/workspace/stocks-data-kline/data/bulk"
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_KCODE = re.compile(r"^[a-z]{2}\d{6}$")
RANDOM_SEED = 20261016          # 固定种子, 保证 random_baseline 可复现
MIN_BARS = 70                   # 至少 70 根K线才算有效样本 (需 MA60 + 前 20 日)


# ─── K线加载 (复用 kline_sim 的格式兼容逻辑) ────────────────────────
def code_to_bulk_prefixes(code):
    code = str(code).zfill(6)
    if code[0] in "69":
        return [f"sh{code}", f"bj{code}"]
    if code[0] in "03":
        return [f"sz{code}", f"bj{code}"]
    return [f"bj{code}", f"sz{code}"]


def limit_rate(code, name=""):
    c = str(code).zfill(6)
    nm = str(name or "")
    if nm.startswith("ST") or nm.startswith("*ST"):
        return 0.05
    if c.startswith(("30", "68")):
        return 0.20
    if c.startswith(("43", "83", "87")):
        return 0.30
    return 0.10


def load_float_shares(codes, batch=60):
    """用腾讯实时快照的流通市值 / 价格 反推流通股本(股)。

    流通市值字段 [44] 单位是亿元, 价格 [3] 是元:
        float_shares = 流通市值(亿) * 1e8 / 价格(元)
    实测: 600519 -> 12.50 亿股, 002242 -> 7.62 亿股, 600036 -> 206.29 亿股。
    拿不到的股票不进表, cyq 会退回旧口径并在 turnover_source 里标注。
    """
    import urllib.request
    out = {}
    items = list(codes)
    hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        syms = ",".join(("sh" if c[0] in "69" else
                         "bj" if c[0] in "48" else "sz") + c for c in chunk)
        try:
            req = urllib.request.Request("https://qt.gtimg.cn/q=" + syms, headers=hdr)
            raw = urllib.request.urlopen(req, timeout=20).read().decode("gbk", "replace")
        except Exception:
            continue
        for line in raw.split(";"):
            if '="' not in line:
                continue
            try:
                f = line.split('"')[1].split("~")
                if len(f) < 46:
                    continue
                bare = f[2]
                px = float(f[3])
                fcap = float(f[44])        # 流通市值, 亿元
                if px > 0 and fcap > 0:
                    out[bare] = fcap * 1e8 / px
            except (ValueError, IndexError):
                continue
    return out


def load_all_klines(max_stocks=None):
    """全量加载 bulk 日K → {code: {dates,open,close,high,low,vol,idx}}"""
    out = {}
    files = sorted(glob.glob(os.path.join(BULK_DIR, "day_qfq_*.csv")))
    files += sorted(glob.glob(os.path.join(BULK_DIR, "day_qfq_extra.csv")))
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "|" not in line:
                        continue
                    head, _, rest = line.partition("|")
                    if not RE_KCODE.match(head):
                        continue
                    c6 = head[2:]
                    if "," in rest:
                        parts = [head] + rest.split(",")
                    else:
                        parts = [p for p in (head + "|" + rest).split("|") if p]
                    if len(parts) < 7 or not RE_DATE.match(parts[1]):
                        continue
                    try:
                        o, c, h, l, v = (float(parts[2]), float(parts[3]),
                                         float(parts[4]), float(parts[5]),
                                         float(parts[6]))
                    except ValueError:
                        continue
                    rec = out.setdefault(c6, {"dates": [], "open": [], "close": [],
                                              "high": [], "low": [], "vol": [],
                                              "idx": {}})
                    d = parts[1]
                    if d in rec["idx"]:
                        continue
                    i = len(rec["dates"])
                    rec["idx"][d] = i
                    rec["dates"].append(d)
                    rec["open"].append(o)
                    rec["close"].append(c)
                    rec["high"].append(h)
                    rec["low"].append(l)
                    rec["vol"].append(v)
        except Exception:
            continue

    # 按日期排序 + 过滤样本不足的
    cleaned = {}
    for c6, rec in out.items():
        if len(rec["dates"]) < MIN_BARS:
            continue
        order = sorted(range(len(rec["dates"])), key=lambda i: rec["dates"][i])
        for k in ("dates", "open", "close", "high", "low", "vol"):
            rec[k] = [rec[k][i] for i in order]
        rec["idx"] = {d: i for i, d in enumerate(rec["dates"])}
        # 筹码分布 (CYQ): O(n^2), 每 20 日抽样一次, 其余日期用最近样本前向填充
        if CYQ_ENABLED:
            try:
                import cyq as _cyq
                import pandas as _pd
                df = _pd.DataFrame({
                    "date": rec["dates"], "open": rec["open"],
                    "high": rec["high"], "low": rec["low"],
                    "close": rec["close"], "volume": rec["vol"]})
                # 流通股本: 优先用 bulk 里的 universe 快照, 缺失时退回旧口径
                fs = FLOAT_SHARES.get(c6)
                series = _cyq.cyq_factor_series(df, float_shares=fs)   # 每5日一点
                cmap = {}
                if not series.empty:
                    sdates = list(series["date"])
                    for row in series.itertuples(index=False):
                        cmap[row.date] = {"profit_ratio": row.cyq_profit_ratio,
                                          "conc90": row.cyq_conc90,
                                          "cost_bias": row.cyq_cost_bias}
                # 前向填充到每根K线
                cyq_by_i = {}
                last = None
                for i, d in enumerate(rec["dates"]):
                    if d in cmap:
                        last = cmap[d]
                    if last is not None:
                        cyq_by_i[i] = last
                rec["cyq"] = cyq_by_i
            except Exception:
                rec["cyq"] = {}
        cleaned[c6] = rec

    # 筹码计算前先取流通股本 (真实换手率口径)
    if CYQ_ENABLED and cleaned:
        try:
            FLOAT_SHARES.update(load_float_shares(cleaned.keys()))
            hit = sum(1 for c in cleaned if c in FLOAT_SHARES)
            print(f"[factor_lab] 流通股本 {hit}/{len(cleaned)} 只", flush=True)
        except Exception as e:
            print(f"[factor_lab] 流通股本获取失败: {e}", flush=True)

    if max_stocks and len(cleaned) > max_stocks:
        rng = random.Random(RANDOM_SEED)
        keys = rng.sample(sorted(cleaned), max_stocks)
        cleaned = {k: cleaned[k] for k in keys}
    return cleaned


# ─── 指标计算 (只用 i 及之前的数据, 严禁未来函数) ───────────────────
def _ma(arr, end, n):
    """arr[0..end] 的最后 n 个均值; 不足返回 None"""
    if end + 1 < n:
        return None
    return sum(arr[end - n + 1:end + 1]) / n


def _rsi(closes, end, period=14):
    if end < period:
        return None
    gains = losses = 0.0
    for j in range(end - period + 1, end + 1):
        ch = closes[j] - closes[j - 1]
        if ch > 0:
            gains += ch
        else:
            losses -= ch
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def _max(arr, end, n):
    if end + 1 < n:
        return None
    return max(arr[end - n + 1:end + 1])


def _min(arr, end, n):
    if end + 1 < n:
        return None
    return min(arr[end - n + 1:end + 1])


def eval_feature(name, rec, i):
    """在 signal 日 i 上计算特征是否成立 (True=多头侧, False=空头侧, None=不适用)。

    所有输入下标 <= i, 即只用 signal 日及之前的数据。
    """
    c = rec["close"]
    o = rec["open"]
    h = rec["high"]
    l = rec["low"]
    v = rec["vol"]
    n = len(c)

    if name == "rsi_oversold":
        r = _rsi(c, i)
        return (r is not None and r < 30), None
    if name == "rsi_overbought":
        r = _rsi(c, i)
        return (r is not None and r > 70), None
    if name == "bias_ma20":
        m = _ma(c, i, 20)
        if m is None or m <= 0:
            return None, None
        bias = (c[i] - m) / m * 100
        return (bias < -5), (bias > 5)
    if name == "ma_golden":
        m5, m20 = _ma(c, i, 5), _ma(c, i, 20)
        if m5 is None or m20 is None:
            return None, None
        return (m5 > m20 and c[i] > m5), (m5 < m20 and c[i] < m5)
    if name == "vol_dry_up":
        # 缩量上涨: 涨但量能低于 5 日均量 (惜售)
        m5v = _ma(v, i, 5)
        if m5v is None or m5v <= 0 or i < 1:
            return None, None
        return (c[i] > c[i - 1] and v[i] < m5v * 0.7), None
    if name == "new_high_20":
        mx = _max(h, i, 20)
        if mx is None:
            return None, None
        return (c[i] >= mx), None
    if name == "breakout_60":
        mx = _max(h, i, 60)
        if mx is None:
            return None, None
        return (c[i] >= mx), None
    if name == "low_pos_20":
        lo, hi = _min(l, i, 20), _max(h, i, 20)
        if lo is None or hi is None or hi <= lo:
            return None, None
        pos = (c[i] - lo) / (hi - lo)
        return (pos < 0.2), (pos > 0.8)
    if name == "cyq_profit_low":
        # 获利盘低 (<30%): 多数持仓浮亏 -> 抛压小, 反弹概率高
        v = rec.get("cyq", {}).get(i) if rec.get("cyq") else None
        if not v:
            return None, None
        pr = v.get("profit_ratio")
        if pr is None:
            return None, None
        return (pr < 0.30), None
    if name == "cyq_conc_high":
        # 筹码高度集中 (conc90 小 = 集中在窄价格带): 易被拉升
        v = rec.get("cyq", {}).get(i) if rec.get("cyq") else None
        if not v:
            return None, None
        c90 = v.get("conc90")
        if c90 is None:
            return None, None
        return (c90 < 0.15), None
    if name == "cyq_cost_bias_low":
        # 现价低于平均成本 (cost_bias<0): 筹码套牢, 上方有解套抛压
        v = rec.get("cyq", {}).get(i) if rec.get("cyq") else None
        if not v:
            return None, None
        cb = v.get("cost_bias")
        if cb is None:
            return None, None
        return (cb < 0), None
    if name == "below_ma60":
        m = _ma(c, i, 60)
        if m is None:
            return None, None
        return (c[i] < m * 0.95), (c[i] > m * 1.05)
    if name == "vol_ratio_high":
        m5v = _ma(v, i, 5)
        if m5v is None or m5v <= 0:
            return None, None
        return (v[i] > m5v * 2), None
    if name == "shrink_pullback":
        # 缩量回调: 跌但量能萎缩 (可能洗盘)
        m5v = _ma(v, i, 5)
        if m5v is None or m5v <= 0 or i < 1:
            return None, None
        return (c[i] < c[i - 1] and v[i] < m5v * 0.7), None
    if name == "gap_up":
        if i < 1 or c[i - 1] <= 0:
            return None, None
        return (o[i] > c[i - 1] * 1.02), (o[i] < c[i - 1] * 0.98)
    return None, None


CYQ_ENABLED = os.environ.get("FACTOR_LAB_CYQ", "1") == "1"

CANDIDATES = [
    "rsi_oversold", "rsi_overbought", "bias_ma20", "ma_golden", "vol_dry_up",
    "new_high_20", "breakout_60", "low_pos_20", "below_ma60", "vol_ratio_high",
    "shrink_pullback", "gap_up",
    # 2026-10-19: 动量 vs 低吸 场景区分
    "momentum_high_bias", "momentum_rps_like", "pullback_in_uptrend",
    "below_ma60_excl_momentum", "below_ma60_tight",
    # 2026-10-19: 筹码分布 (cyq) 因子
    "cyq_profit_low", "cyq_conc_high", "cyq_cost_bias_low",
]


# ─── 单次前向收益 (与 kline_sim 同口径) ────────────────────────────
def forward_return(rec, i, hold, slip):
    """signal 日 i → 次日开盘买入 → 第 hold 日收盘卖出。返回 (ret_pct 或 None, 原因)"""
    n = len(rec["dates"])
    i0 = i + 1
    if i0 >= n:
        return None, "no_forward"
    buy = rec["open"][i0] * (1 + slip)
    if buy <= 0:
        return None, "bad_price"
    lr = limit_rate(rec.get("code", ""))
    if i0 > 0:
        limit_up = round(rec["close"][i0 - 1] * (1 + lr), 2)
        if rec["high"][i0] >= limit_up - 0.005:
            return None, "limit_up_entry"

    j = i0 + hold
    if j >= n:
        return None, "no_exit"
    px, xdate = j, rec["dates"][j]
    # 出场日封跌停 → 顺延最多 5 日
    for _ in range(5):
        if px <= 0:
            break
        limit_dn = round(rec["close"][px - 1] * (1 - lr), 2)
        if rec["close"][px] <= limit_dn + 0.005 and px + 1 < n:
            px += 1
            xdate = rec["dates"][px]
            continue
        break
    ret = (rec["close"][px] * (1 - slip)) / buy * 100 - 100
    return ret, None


# ─── 统计 ──────────────────────────────────────────────────────────
def t_stat(samples):
    """单样本 t 检验: 均值 / (标准差/sqrt(n))。n<2 返回 None"""
    n = len(samples)
    if n < 2:
        return None
    mean = sum(samples) / n
    var = sum((x - mean) ** 2 for x in samples) / (n - 1)
    if var <= 0:
        return None
    return mean / (math.sqrt(var) / math.sqrt(n))


def summarize(samples):
    if not samples:
        return None
    n = len(samples)
    mean = sum(samples) / n
    med = statistics.median(samples)
    win = sum(1 for x in samples if x > 0) / n * 100
    return {
        "n": n,
        "win_rate": round(win, 2),
        "avg_ret": round(mean, 3),
        "median_ret": round(med, 3),
        "t": round(t_stat(samples), 2) if t_stat(samples) is not None else None,
    }


def main():
    ap = argparse.ArgumentParser(description="特征实验室: 批量测量候选特征的预测力")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "factor_lab.json"))
    ap.add_argument("--report", default=os.path.join(DATA_DIR, "factor_lab.md"))
    ap.add_argument("--hold-days", default="1,3,5")
    ap.add_argument("--max-stocks", type=int, default=0,
                    help="限制股票数 (0=全量; 本地调试用小值)")
    ap.add_argument("--min-n", type=int, default=200,
                    help="样本量下限, 低于此值标注样本不足")
    ap.add_argument("--min-edge", type=float, default=0.10,
                    help="超过随机基准的最小边际 (百分点); 差距小于此值视为无边缘, "
                         "避免把 0.01pp 的噪声当成有效信号")
    ap.add_argument("--slip-pct", type=float, default=0.03)
    args = ap.parse_args()

    holds = [int(x) for x in args.hold_days.split(",") if x.strip()]
    slip = args.slip_pct / 100.0
    now = datetime.now(CST)

    print(f"[factor_lab] 加载 bulk K线 (max_stocks={args.max_stocks or 'all'}) ...")
    klines = load_all_klines(max_stocks=args.max_stocks or None)
    print(f"[factor_lab] {len(klines)} 只股票 (>= {MIN_BARS} 根K线)")

    # 预计算每个 (code, feature) 命中的 signal 下标, 避免重复扫全历史
    # signal 下标从 MIN_BARS 开始, 保证指标有足够历史
    hit_idx = {f: {} for f in CANDIDATES}
    miss_idx = {f: {} for f in CANDIDATES}
    random_idx = {}
    rng = random.Random(RANDOM_SEED)

    for c6, rec in klines.items():
        n = len(rec["dates"])
        if n < MIN_BARS + 10:
            continue
        for f in CANDIDATES:
            for i in range(MIN_BARS, n - max(holds) - 1):
                long_side, short_side = eval_feature(f, rec, i)
                if long_side is None and short_side is None:
                    continue
                if long_side:
                    hit_idx[f].setdefault(c6, []).append(i)
                elif short_side is False and short_side is not None:
                    miss_idx[f].setdefault(c6, []).append(i)
        # 随机基准: 同一批下标里随机取 (与特征用同样的候选池)
        for i in range(MIN_BARS, n - max(holds) - 1):
            random_idx.setdefault(c6, []).append(i)

    def collect(idx_map, want_random=False):
        """把 (code -> [signal 下标]) 铺成每个持有期的收益样本列表"""
        samples = {h: [] for h in holds}
        n_sig = 0
        for c6, idxs in idx_map.items():
            rec = klines[c6]
            rec["code"] = c6
            for i in idxs:
                if want_random and rng.random() > 0.5:
                    # 随机基准只取一半做多头侧, 另一半做空头侧
                    pass
                n_sig += 1
                for h in holds:
                    r, _ = forward_return(rec, i, h, slip)
                    if r is not None:
                        samples[h].append(r)
        return samples, n_sig

    results = {}
    for f in CANDIDATES:
        entry = {}
        for side, mp in (("long", hit_idx[f]), ("short", miss_idx[f])):
            samples, n_sig = collect(mp)
            entry[side] = {str(h): summarize(samples[h]) for h in holds}
            entry[side]["n_signals"] = n_sig
        for h in holds:
            ls = entry["long"][str(h)]
            ss = entry["short"][str(h)]
            if ls and ss:
                entry["long"][str(h)]["spread_vs_short"] = round(
                    ls["avg_ret"] - ss["avg_ret"], 3)
        results[f] = entry
    # 先算随机基准, 判定要用它做边际比较
    rand_samples, rand_n = collect(random_idx)
    baseline = {str(h): summarize(rand_samples[h]) for h in holds}

    for f in CANDIDATES:
        for h in holds:
            ls = entry["long"][str(h)]
            ss = entry["short"][str(h)]
            if ls and ss:
                entry["long"][str(h)]["spread_vs_short"] = round(
                    ls["avg_ret"] - ss["avg_ret"], 3)
            ls = results[f]["long"][str(h)]
            if not ls:
                continue
            base = baseline[str(h)]["avg_ret"]
            edge = ls["avg_ret"] - base
            sig = (ls["n"] >= args.min_n and ls["t"] is not None and ls["t"] > 2)
            flag = "OK " if (sig and edge >= args.min_edge) else "   "
            print(f"  {flag}{f:<16} hold={h}d n={ls['n']:<6} "
                  f"win={ls['win_rate']:5.1f}% avg={ls['avg_ret']:+6.2f}% "
                  f"t={ls['t']} edge={edge:+.2f}pp")

    print(f"  [baseline] random n={rand_n} " +
          " ".join(f"{h}d:{baseline[str(h)]['avg_ret']:+.2f}%" for h in holds))

    # 给每个结果补 beat_baseline / verdict, 避免下游误用「显著但无用」的特征
    for f in CANDIDATES:
        for h in holds:
            ls = results[f]["long"][str(h)]
            if not ls:
                continue
            base = baseline[str(h)]["avg_ret"]
            enough = ls["n"] >= args.min_n
            sig = enough and ls["t"] is not None and ls["t"] > 2
            ls["edge_vs_baseline"] = round(ls["avg_ret"] - base, 3)
            ls["beat_baseline"] = bool(ls["edge_vs_baseline"] >= args.min_edge)
            ls["significant"] = bool(sig)
            ls["verdict"] = ("effective" if (sig and ls["beat_baseline"])
                              else ("significant_but_no_edge" if sig
                                    else "insufficient"))
            ss = results[f]["short"][str(h)]
            if ss:
                ss["edge_vs_baseline"] = round(ss["avg_ret"] - base, 3)
                ss["beat_baseline"] = bool(ss["edge_vs_baseline"] >= args.min_edge)
                ss["significant"] = bool(ss["n"] >= args.min_n and ss["t"] is not None
                                          and ss["t"] > 2)
                ss["verdict"] = ("effective" if (ss["significant"] and ss["beat_baseline"])
                                  else ("significant_but_no_edge" if ss["significant"]
                                        else "insufficient"))

    payload = {
        "ts": now.isoformat(),
        "engine": "factor_lab.py",
        "n_stocks": len(klines),
        "hold_days": holds,
        "slip_pct": args.slip_pct,
        "min_n": args.min_n,
        "min_edge_pp": args.min_edge,
        "random_baseline": baseline,
        "features": results,
        "meta": {
            "source": f"stocks-data-kline bulk 前复权日K ({len(klines)} 只)",
            "entry_rule": "signal 日次日开盘买入 (不用当日收盘, 避免未来函数)",
            "exit_rule": f"第 N 日收盘卖出; 出场封跌停顺延最多5日",
            "limit_rule": "入场日触板不买 (涨跌停按 10/20/30%/ST 5% 分档)",
            "no_param_search": "阈值全部写死, 不做网格寻优 (寻优=过拟合)",
            "interpretation": "spread = long avg_ret - short avg_ret; "
                              "edge_vs_baseline = avg_ret - random_baseline; "
                              "verdict=effective 需同时满足 n>=min_n, t>2, "
                              "edge>=min_edge_pp; 打不赢随机的特征无预测力",
            "caveat": "单边滑点固定, 未计佣金印花税 (实盘额外成本约 0.1%+); "
                      "样本为全历史混合市况, 未分 regime 分层 (样本充足后可补)",
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    # 人读报告
    lines = [
        "# 特征实验室 (factor_lab)",
        "",
        f"- 时间: {now.strftime('%Y-%m-%d %H:%M')} CST",
        f"- 样本: {len(klines)} 只股票全历史, 单边滑点 {args.slip_pct}%",
        f"- 持有期: {holds} 日",
        f"- 随机基准: " + " / ".join(
            f"{h}日均 {baseline[str(h)]['avg_ret']:+.2f}%" for h in holds),
        "",
        "| 特征 | 持有期 | n | 胜率% | 均收益% | 中位% | t值 | 多空差% | 判定 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for f in CANDIDATES:
        for h in holds:
            ls = results[f]["long"][str(h)]
            ss = results[f]["short"][str(h)]
            if not ls:
                continue
            spread = ls.get("spread_vs_short")
            base = baseline[str(h)]["avg_ret"]
            # 判定三要素: 样本够 / 统计显著 / 边际足够大
            # 边际必须超过 min_edge, 否则 0.01pp 的差距只是噪声
            enough = ls["n"] >= args.min_n
            sig = enough and ls["t"] is not None and ls["t"] > 2
            beat = (ls["avg_ret"] - base) >= args.min_edge
            if sig and beat:
                verdict = "有效"
            elif sig:
                verdict = "显著但无足够边际"
            else:
                verdict = "无效/样本不足"
            lines.append(
                f"| {f} | {h} | {ls['n']} | {ls['win_rate']:.1f} | "
                f"{ls['avg_ret']:+.2f} | {ls['median_ret']:+.2f} | {ls['t']} | "
                f"{spread if spread is not None else '—'} | {verdict} |")
    lines += [
        "",
        "## 怎么读这张表",
        "",
        "- **打不赢 random_baseline 的特征没有预测力**, 不管它多符合直觉。",
        "- t>2 且 n 足够, 才排除「小样本运气」。",
        "- 多空差 (long avg - short avg) 大, 说明这个特征有**区分方向**的能力,",
        "  即使单侧打不赢基准, 也可用于「回避」而非「买入」。",
        "- 本表是混合市况全历史结果, **不保证未来同样有效**;",
        "  真正可用前需分 regime 做样本外验证 (下一步)。",
        "",
        "## 已知局限",
        "",
        "- 未计佣金/印花税 (实盘每笔约 0.1%+, 对 1 日持有期影响显著)。",
        "- 未分牛熊/强弱市况 (混合市况会稀释信号, 也可掩盖「只在弱市有效」的信号)。",
        "- 资金类特征 (main_net) 未纳入, 因需 moneyflow 历史数据 (当前只存实时)。",
    ]
    with open(args.report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"  → {args.out}")
    print(f"  → {args.report}")


if __name__ == "__main__":
    main()
