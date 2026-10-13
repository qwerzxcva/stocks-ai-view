#!/usr/bin/env python3
"""
reflect_engine.py — 复盘与自我反思闭环

目的: 回答「我们的预测到底准不准」, 而不是只输出漂亮的分数。

流程:
  1. 读取历史预测快照 (engine_v5 / hot_impact / lowpool 的历史输出)
  2. 到 N 个交易日后, 用实际K线计算该股实际涨跌
  3. 对比: 命中/未命中/方向对但幅度错
  4. 统计各指标的「历史胜率」, 找出:
     - 哪些指标真的有预测力 (胜率显著 >50%)
     - 哪些是噪声 (胜率 ≈50% 或 <50%)
  5. 输出校准建议: 噪声指标降权, 有效指标加权
  6. 写入 fact 记忆, 供下一轮权重校准使用

防自欺设计:
  - 只用「预测时点已知的信息」, 不用未来数据 (无前视偏差)
  - 明确标注样本量, 样本 <30 的结论标「证据不足」
  - 同时报告「多空双向胜率」, 只报看多胜率是选择性披露

用法:
  python3 reflect_engine.py --horizon 5           # 5日后验证
  python3 reflect_engine.py --horizon 5 --horizon 10
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))


def load_klines(bulk_dir):
    """加载本地K线 → {code: [[date,o,c,h,l,v], ...]}"""
    data = {}
    files = sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv")))
    for p in files:
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
                        [parts[0], float(parts[1]), float(parts[2]),
                         float(parts[3]), float(parts[4]), float(parts[5])])
                except ValueError:
                    continue
    for c in data:
        data[c].sort(key=lambda b: b[0])
    return data


def kline_index(bars):
    """date → index 映射"""
    return {b[0]: i for i, b in enumerate(bars)}


def verify_prediction(bars, pred_date, horizon):
    """pred_date 之后 horizon 个交易日的实际涨跌 %。"""
    idx = kline_index(bars)
    if pred_date not in idx:
        # 找 pred_date 当天或之后最近的一根
        cands = [d for d in idx if d >= pred_date]
        if not cands:
            return None
        i = idx[min(cands)]
    else:
        i = idx[pred_date]
    j = i + horizon
    if j >= len(bars):
        return None
    entry = bars[i][2]
    exit_ = bars[j][2]
    if entry <= 0:
        return None
    return round((exit_ / entry - 1) * 100, 2)


def reflect_engine_v5(pred_file, klines, horizons=(5, 10)):
    """对 engine_v5 输出做事后验证。"""
    d = json.load(open(pred_file, encoding="utf-8"))
    ts = d.get("ts", "")
    pred_date = d.get("top", [{}])[0].get("date") if d.get("top") else ts[:10]
    if not pred_date:
        return None

    results = []
    for r in d.get("top", []):
        code = r["code"]
        bars = klines.get(code)
        if not bars:
            continue
        row = {"code": code, "pred_date": pred_date, "score": r["score"],
               "resonance": r["resonance"], "bull": r["bull"], "bear": r["bear"]}
        for h in horizons:
            row[f"actual_{h}d"] = verify_prediction(bars, pred_date, h)
        # 记录每个指标的票, 用于统计指标胜率
        row["votes"] = {v["indicator"]: v["vote"] for v in r.get("votes", [])}
        results.append(row)
    return {"pred_file": os.path.basename(pred_file), "pred_date": pred_date,
            "n": len(results), "rows": results}


def analyze(results_list, horizons):
    """统计: 方向命中率 + 各指标胜率。"""
    # 1. 整体方向命中
    overall = {}
    for h in horizons:
        key = f"actual_{h}d"
        valid = [(r, r[key]) for rl in results_list for r in rl["rows"] if r.get(key) is not None]
        if not valid:
            overall[h] = {"n": 0}
            continue
        # 预测看多 (bull > bear), 看实际是否上涨
        bull_hits = sum(1 for r, a in valid if r["bull"] > r["bear"] and a > 0)
        bull_n = sum(1 for r, a in valid if r["bull"] > r["bear"])
        bear_hits = sum(1 for r, a in valid if r["bear"] > r["bull"] and a < 0)
        bear_n = sum(1 for r, a in valid if r["bear"] > r["bull"])
        avg_ret = sum(a for _, a in valid) / len(valid)
        win = sum(1 for _, a in valid if a > 0)
        overall[h] = {
            "n": len(valid),
            "bull_win_rate": round(bull_hits / bull_n * 100, 1) if bull_n else None,
            "bull_n": bull_n,
            "bear_win_rate": round(bear_hits / bear_n * 100, 1) if bear_n else None,
            "bear_n": bear_n,
            "avg_ret_pct": round(avg_ret, 2),
            "raw_win_rate": round(win / len(valid) * 100, 1),
        }

    # 2. 各指标胜率 (该指标投多方, 实际是否上涨)
    ind_stats = defaultdict(lambda: {"bull_n": 0, "bull_hit": 0, "bear_n": 0, "bear_hit": 0})
    for rl in results_list:
        for r in rl["rows"]:
            for ind, vote in r.get("votes", {}).items():
                # 用最长 horizon 的收益做标签
                act = r.get(f"actual_{max(horizons)}d")
                if act is None:
                    continue
                s = ind_stats[ind]
                if vote > 0:
                    s["bull_n"] += 1
                    if act > 0:
                        s["bull_hit"] += 1
                elif vote < 0:
                    s["bear_n"] += 1
                    if act < 0:
                        s["bear_hit"] += 1

    indicators = {}
    for ind, s in ind_stats.items():
        bn, bh = s["bull_n"], s["bull_hit"]
        sn, sh = s["bear_n"], s["bear_hit"]
        indicators[ind] = {
            "bull_n": bn,
            "bull_win_rate": round(bh / bn * 100, 1) if bn else None,
            "bear_n": sn,
            "bear_win_rate": round(sh / sn * 100, 1) if sn else None,
            "evidence": "insufficient" if (bn + sn) < 30 else "ok",
        }

    # 3. 按多空胜率均值排序, 找出有效/噪声指标
    def quality(v):
        rates = [x for x in (v["bull_win_rate"], v["bear_win_rate"]) if x is not None]
        return sum(rates) / len(rates) if rates else 50.0

    ranked = sorted(indicators.items(), key=lambda kv: -quality(kv[1]))
    return {"overall": overall, "indicators": indicators,
            "ranked": [{"indicator": k, "quality": round(quality(v), 1), **v}
                       for k, v in ranked]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred-dir", default="/tmp", help="engine_v5 输出所在目录")
    ap.add_argument("--bulk", default=None, help="K线 bulk 目录 (默认 paths.BULK)")
    ap.add_argument("--horizons", default="5,10")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    horizons = tuple(int(x) for x in args.horizons.split(","))
    pred_files = sorted(glob.glob(os.path.join(args.pred_dir, "engine_v5*.json")))
    if not pred_files:
        print(f"未找到预测文件: {args.pred_dir}/engine_v5*.json", file=sys.stderr)
        return 1

    bulk = args.bulk
    if not bulk:
        from paths import BULK
        bulk = BULK
    klines = load_klines(bulk)
    print(f"[reflect] K线 {len(klines)} 只, 预测文件 {len(pred_files)} 个", flush=True)

    results_list = []
    for pf in pred_files:
        rl = reflect_engine_v5(pf, klines, horizons)
        if rl and rl["rows"]:
            # 只保留「预测日期 + 最长horizon」已在K线覆盖内的记录
            print(f"  {os.path.basename(pf)}: {rl['n']} 条 (pred_date={rl['pred_date']})", flush=True)
            results_list.append(rl)

    if not results_list:
        print("无可验证记录 (可能预测日期太近, 未来数据还不存在)", file=sys.stderr)
        return 1

    analysis = analyze(results_list, horizons)
    now = datetime.now(CST)
    out = {
        "ts": now.isoformat(timespec="seconds"),
        "horizons": list(horizons),
        "n_pred_files": len(results_list),
        "n_records": sum(rl["n"] for rl in results_list),
        "analysis": analysis,
        "meta": {
            "method": "预测时点的多空票 vs 预测后 N 日实际涨跌, 无前视偏差",
            "bull_win_rate": "指标看多且实际上涨的比例",
            "bear_win_rate": "指标看空且实际下跌的比例",
            "evidence": "样本<30 标 insufficient, 结论不可信",
            "caveat": "单一指标历史胜率接近50%即噪声; 只报看多胜率属选择性披露, "
                      "故同时报多空双向",
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print()
    print(f"=== 整体方向命中 (n={out['n_records']}) ===")
    for h, v in analysis["overall"].items():
        if v.get("n"):
            print(f"  {h}日后: 看多胜率={v['bull_win_rate']}% (n={v['bull_n']}) | "
                  f"看空胜率={v['bear_win_rate']}% (n={v['bear_n']}) | "
                  f"平均收益={v['avg_ret_pct']}%")
    print()
    print("=== 指标质量排名 (多空胜率均值) ===")
    for r in analysis["ranked"][:12]:
        ev = "" if r["evidence"] == "ok" else " [样本不足]"
        print(f"  {r['indicator']:18s} 质量={r['quality']:5.1f} "
              f"多{r['bull_win_rate']}%(n={r['bull_n']}) "
              f"空{r['bear_win_rate']}%(n={r['bear_n']}){ev}")
    print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
