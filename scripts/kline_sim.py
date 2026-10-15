#!/usr/bin/env python3
"""
kline_sim.py — K线模拟回测框架

从已有选股结果 (multi_v4.json / ai_picks.json) 读取信号,
用 stocks-data-kline 的 bulk 日K (前复权) 数据做走势模拟:

  入场 = 信号日之后第一个有K线数据交易日的开盘价
  出场 = 入场日起第 N 个交易日 (N = 1/3/5/10) 的收盘价
  滑点单边计入: 买入 open*(1+s), 卖出 close*(1-s)
  涨跌停限制 (可选): 入场日触板不买入; 出场日封跌停则顺延 (最多5日)

只读输入: data/*.json 选股结果 + ../stocks-data-kline/data/bulk/day_qfq_*.csv
输出: data/kline_sim.json

用法:
  python3 scripts/kline_sim.py --out data/kline_sim.json --days 1,3,5
  python3 scripts/kline_sim.py --days 1,3,5,10 --max-pos 30 --slip-pct 0.03 --limit-up
"""
import argparse
import glob
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from paths import BULK as BULK_DIR, AI_VIEW_DATA as DATA_DIR
except Exception:
    BULK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "..", "stocks-data-kline", "data", "bulk")
    DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RE_KCODE = re.compile(r"^[a-z]{2}\d{6}$")


def norm_date(s):
    s = str(s)
    if re.match(r"^\d{8}$", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def code_to_bulk_prefixes(code):
    """A股裸代码 → bulk 带交易所前缀代码 (6位代码, 按常见规则多备一个前缀兜底)。"""
    code = str(code).zfill(6)
    if code[0] in "69":
        return [f"sh{code}", f"bj{code}"]
    if code[0] in "03":
        return [f"sz{code}", f"bj{code}"]
    return [f"bj{code}", f"sz{code}"]


def limit_rate(code, name=""):
    """涨跌停幅度: 创业板/科创板 20%, 北交所 30%, ST 5%, 其余 10%。"""
    code = str(code).zfill(6)
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("4", "8")):
        return 0.30
    if "ST" in str(name).upper():
        return 0.05
    return 0.10


def load_klines(codes):
    """按需加载 bulk 日K: 只保留 codes (裸6位) 命中的行, 省内存。

    返回 {code: {"dates":[], "open":[], "close":[], "high":[], "idx":{date:i}}}
    行格式: code|date|open|close|high|low|vol (主分片, 全竖线)
            code|date,open,close,high,low,vol (extra/recent 分片, 日期后为逗号)
    """
    out = {c: {"dates": [], "open": [], "close": [], "high": [], "idx": {}} for c in codes}
    # 预建 prefix -> 裸码 映射: 内层循环原先对每一行都调用
    # code_to_bulk_prefixes(c6) 遍历全部目标代码 (O(行数 x 代码数)),
    # 实测占 kline_sim 总耗时的大头。bulk 行数约 200 万, 目标代码约 3000。
    prefix_to_code = {}
    for c in codes:
        for p in code_to_bulk_prefixes(c):
            prefix_to_code.setdefault(p, c)
    wanted = set(prefix_to_code)
    files = sorted(glob.glob(os.path.join(BULK_DIR, "day_qfq_*.csv")))
    n_rows = 0
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#"):
                    continue
                if "|" not in line:
                    continue
                head, _, rest = line.partition("|")
                c6 = prefix_to_code.get(head)
                if c6 is None:
                    continue
                if "," in rest:
                    parts = [head] + rest.strip().split(",")
                else:
                    parts = [p for p in (head + "|" + rest).strip().split("|")
                             if p]
                if len(parts) < 5 or not RE_DATE.match(parts[1]):
                    continue
                try:
                    o, c, h = float(parts[2]), float(parts[3]), float(parts[4])
                except ValueError:
                    continue
                date = parts[1]
                rec = out[c6]
                i = rec["idx"].get(date)
                if i is None:
                    i = len(rec["dates"])
                    rec["dates"].append(date)
                    rec["idx"][date] = i
                    rec["open"].append(o)
                    rec["close"].append(c)
                    rec["high"].append(h)
                elif i < len(rec["open"]):
                    # 同一 (code,date) 在多个分片重复出现: 后写的覆盖,
                    # 与原先 "found 即 break" 的语义一致 (先到者胜)
                    pass
                n_rows += 1
    for c6, rec in out.items():
        order = sorted(range(len(rec["dates"])), key=lambda i: rec["dates"][i])
        rec["open"] = [rec["open"][i] for i in order]
        rec["close"] = [rec["close"][i] for i in order]
        rec["high"] = [rec["high"][i] for i in order]
        rec["dates"] = [rec["dates"][i] for i in order]
        rec["idx"] = {d: i for i, d in enumerate(rec["dates"])}
    return out, n_rows


def load_signals(data_dir, sources):
    """读取选股结果, 返回 (signals, meta)。signals: [{code,name,score,source,signal_date}]"""
    signals = []
    meta = {}
    for src in sources:
        # 历史预测快照: data/predictions/engine_v5_*.json (复盘闭环的输入)
        if src == "predictions":
            pred_dir = os.path.join(data_dir, "predictions")
            if not os.path.isdir(pred_dir):
                meta[src] = {"dir": pred_dir, "missing": True}
                continue
            n_total = 0
            for fp in sorted(glob.glob(os.path.join(pred_dir, "engine_v5_*.json"))):
                base = os.path.basename(fp)
                m = re.match(r"engine_v5_(\d{4}-\d{2}-\d{2})\.json", base)
                pdate = m.group(1) if m else norm_date(json.load(open(fp)).get("ts", "")[:10])
                try:
                    d = json.load(open(fp, encoding="utf-8"))
                except Exception:
                    continue
                for it in (d.get("top") or []):
                    code = str(it.get("code") or "")
                    if not code:
                        continue
                    signals.append({
                        "code": code[2:] if code[:2] in ("sh", "sz", "bj") else code,
                        "name": it.get("name", ""),
                        "score": it.get("score", 0),
                        "source": "predictions",
                        "signal_date": pdate,
                    })
                    n_total += 1
            meta[src] = {"dir": pred_dir, "files": len(glob.glob(os.path.join(pred_dir, "engine_v5_*.json"))), "n": n_total}
            continue

        if src == "ai_picks":
            path = os.path.join(data_dir, "ai_picks.json")
            arr_key, date_key = "picks", "date"
        else:
            path = os.path.join(data_dir, f"{src}.json")
            arr_key, date_key = "buy", "date"
        if not os.path.exists(path):
            meta[src] = {"file": path, "missing": True}
            continue
        d = json.load(open(path, encoding="utf-8"))
        sdate = norm_date(d.get(date_key, ""))
        items = d.get(arr_key) or []
        meta[src] = {"file": path, "signal_date": sdate, "n": len(items)}
        for it in items:
            code = str(it.get("code") or "")
            if not code:
                continue
            signals.append({
                "code": code.zfill(6),
                "name": it.get("name", ""),
                "score": it.get("score", 0),
                "source": src,
                "signal_date": sdate,
            })
    return signals, meta


def simulate(sig, kl, days, slip, limit_on):
    """单个信号的各持有期模拟。返回 (结果dict或None, 原因)。"""
    k = kl.get(sig["code"])
    if not k or not k["dates"]:
        return None, None, "no_kline"
    d = sig["signal_date"]
    i0 = next((i for i, dd in enumerate(k["dates"]) if dd > d), None)
    if i0 is None:
        return None, None, "no_forward_data"
    e_date = k["dates"][i0]
    open_e = k["open"][i0]
    high_e = k["high"][i0]
    if open_e <= 0:
        return None, None, "bad_price"

    buy = open_e * (1 + slip)
    res = {"entry_date": e_date, "entry_price": round(buy, 3)}

    if limit_on and i0 > 0:
        prev_close = k["close"][i0 - 1]
        lr = limit_rate(sig["code"], sig["name"])
        limit_up = round(prev_close * (1 + lr), 2)
        if high_e >= limit_up - 0.005:
            return None, None, "limit_up_at_entry"

    out = {}
    for h in days:
        j = i0 + h
        if j >= len(k["dates"]):
            out[str(h)] = None
            continue
        close_x = k["close"][j]
        x_date = k["dates"][j]
        pinned = False
        if limit_on:
            px = j
            lr = limit_rate(sig["code"], sig["name"])
            while px + 1 < len(k["dates"]) and px < j + 5:
                if px > 0:
                    limit_dn = round(k["close"][px - 1] * (1 - lr), 2)
                    if k["close"][px] <= limit_dn + 0.005:
                        px += 1
                        continue
                break
            if px > j:
                x_date = k["dates"][px]
                close_x = k["close"][px]
                pinned = True
        res[f"exit_{h}"] = {"date": x_date, "close": close_x, "pinned": pinned,
                             "ret_pct": round((close_x * (1 - slip)) / buy * 100 - 100, 3)}
        out[str(h)] = res[f"exit_{h}"]["ret_pct"]
    return res, out, None


def main():
    ap = argparse.ArgumentParser(description="K线模拟回测框架")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "kline_sim.json"))
    ap.add_argument("--days", default="1,3,5,10", help="持有期列表, 逗号分隔")
    ap.add_argument("--max-pos", type=int, default=20, help="最大持仓数 (按 score 取前 N)")
    ap.add_argument("--max-signals", type=int, default=3000,
                    help="最多模拟多少信号 (全量回溯后信号数万, 全算太慢; 按 score 取前 N)")
    ap.add_argument("--slip-pct", type=float, default=0.03, help="单边滑点 %% (默认 0.03)")
    ap.add_argument("--limit-up", action="store_true", help="启用涨跌停限制 (触板不买/封板顺延)")
    ap.add_argument("--source", default="multi_v4,ai_picks", help="选股来源, 逗号分隔; 含 predictions 则读 data/predictions/ 历史预测快照")
    args = ap.parse_args()

    days = sorted({int(x) for x in args.days.split(",") if x.strip()})
    slip = args.slip_pct / 100.0
    sources = [s.strip() for s in args.source.split(",") if s.strip()]

    signals, src_meta = load_signals(DATA_DIR, sources)
    if not signals:
        print("[kline_sim] 无信号 (各来源 picks/buy 为空或文件缺失)", file=sys.stderr)
    # 全量回溯后信号数万, 按 score 截断再模拟 (统计上取头部信号已足够)
    if len(signals) > args.max_signals:
        signals.sort(key=lambda s: -s.get("score", 0))
        print(f"[kline_sim] 信号 {len(signals)} → 截断至 {args.max_signals} (按 score)", flush=True)
        signals = signals[: args.max_signals]
    codes = sorted({s["code"] for s in signals})
    kl, n_rows = load_klines(codes) if codes else ({}, 0)

    sim_results = []
    skipped = {"no_kline": 0, "no_forward_data": 0, "limit_up_at_entry": 0, "bad_price": 0}
    for sig in signals:
        res, per_horizon, reason = simulate(sig, kl, days, slip, args.limit_up)
        if reason in skipped:
            skipped[reason] += 1
            continue
        sim_results.append({**sig, "entry_date": res["entry_date"],
                            "entry_price": res["entry_price"], "ret": per_horizon})

    sim_results.sort(key=lambda s: -s.get("score", 0))
    held = sim_results[:args.max_pos]

    # ── 分持有期统计 (全部可模拟信号) ──
    by_horizon = {}
    all_rets = []
    for h in days:
        vals = [s["ret"][str(h)] for s in sim_results if s["ret"].get(str(h)) is not None]
        wins = sum(1 for v in vals if v > 0)
        entry = {
            "n": len(vals),
            "win_rate": round(wins / len(vals), 4) if vals else None,
            "avg_ret_pct": round(statistics.mean(vals), 3) if vals else None,
            "median_ret_pct": round(statistics.median(vals), 3) if vals else None,
            "stdev_pct": round(statistics.stdev(vals), 3) if len(vals) > 1 else None,
        }
        by_horizon[str(h)] = entry
        all_rets.extend(vals)
    overall_win = round(sum(1 for v in all_rets if v > 0) / len(all_rets), 4) if all_rets else None

    # ── 组合净值曲线与最大回撤 (前 max_pos 只, 等权) ──
    curve = [1.0]
    tmax = max(days)
    held_idx = []
    for s in held:
        rec = kl.get(s["code"])
        i0 = rec["idx"].get(s["entry_date"]) if rec else None
        if i0 is not None:
            held_idx.append((s["entry_price"], i0, rec))
    for t in range(1, tmax + 1):
        vals = [rec["close"][i0 + t] / ep for ep, i0, rec in held_idx
                if i0 + t < len(rec["close"])]
        curve.append(statistics.mean(vals) if vals else curve[-1])
    peak, mdd = 1.0, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, 1 - v / peak) if peak > 0 else mdd

    # ── Top 10 明细 ──
    top = []
    for s in held[:10]:
        top.append({
            "code": s["code"], "name": s["name"], "score": s.get("score"),
            "source": s["source"], "signal_date": s["signal_date"],
            "entry_date": s["entry_date"], "entry_price": s["entry_price"],
            "ret_by_day": {str(h): s["ret"].get(str(h)) for h in days},
        })

    bulk_dates = [d for rec in kl.values() for d in ([rec["dates"][-1]] if rec["dates"] else [])]

    # ── Analyzer 指标 (借鉴 backtrader Analyzer 架构, 见 scripts/analyzers.py) ──
    #  为什么加: 此前 overall 只有胜率+均收益+最大回撤, 缺风险调整收益。
    #  胜率 50% 但盈亏比 2:1 的策略是赚钱的, 单看胜率会漏掉 ——
    #  analyzers 的 TradeAnalyzer 给 profit_loss_ratio, Sharpe/Sortino/Calmar/SQN
    #  给风险调整后的视图。
    analyzer_stats = None
    if all_rets:
        try:
            from analyzers import run_all
            trades = [{"ret_pct": r, "exit_date": None, "hold_bars": h}
                      for r, h in zip(all_rets, [days[0]] * len(all_rets))]
            az = run_all(trades)
            analyzer_stats = {
                "sharpe": az.get("sharpe"),
                "sortino": az.get("sortino"),
                "calmar": az.get("calmar"),
                "drawdown": az.get("drawdown"),
                "trades": az.get("trades"),
                "sqn": az.get("sqn"),
                "origin": "scripts/analyzers.py, 架构借鉴 cloudQuant/backtrader (GPL-3.0,"
                          " 仅借鉴 Analyzer 设计, 数学式独立实现)",
            }
            print(f"[kline_sim] analyzers: sharpe={az['sharpe'].get('sharperatio')} "
                  f"sqn={az['sqn'].get('sqn')} "
                  f"盈亏比={az['trades'].get('profit_loss_ratio')}")
        except Exception as e:
            print(f"[kline_sim] analyzers skipped: {e}", file=sys.stderr)

    result = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "sources": src_meta,
        "params": {
            "days": days, "max_positions": args.max_pos,
            "slippage_pct": args.slip_pct, "limit_up_down": args.limit_up,
            "entry": "信号日后首个K线交易日开盘价", "exit": "入场日起第N个交易日收盘价",
        },
        "n_signals_total": len(signals),
        "n_simulated": len(sim_results),
        "n_held": len(held),
        "skipped": skipped,
        "overall": {
            "win_rate": overall_win,
            "avg_return_pct": round(statistics.mean(all_rets), 3) if all_rets else None,
            "max_drawdown_pct": round(mdd * 100, 3),
            "n_pairs": len(all_rets),
        },
        "by_horizon": by_horizon,
        "analyzers": analyzer_stats,
        "equity_curve": {"max_day": tmax, "values": [round(v, 4) for v in curve]},
        "top_signals": top,
        "coverage": {
            "bulk_max_date": max(bulk_dates) if bulk_dates else None,
            "bulk_rows_loaded": n_rows,
        },
        "note": None,
    }
    if not all_rets:
        result["note"] = (f"无有效收益样本: 可入场信号 {len(sim_results)} 个, 但各持有期出场日超出 bulk K线数据范围 "
                          f"(截至 {result['coverage']['bulk_max_date']})。更新 bulk K线后重跑。")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[kline_sim] {args.out}  信号={len(signals)} 可模拟={len(sim_results)} "
          f"持仓={len(held)}  整体胜率={result['overall']['win_rate']} "
          f"最大回撤={result['overall']['max_drawdown_pct']}%")
    for h in days:
        e = by_horizon[str(h)]
        print(f"  持有{h:>2d}日: n={e['n']} 胜率={e['win_rate']} 均值={e['avg_ret_pct']}%")
    if result["note"]:
        print(f"[kline_sim] {result['note']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
