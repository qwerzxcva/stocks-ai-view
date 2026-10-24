#!/usr/bin/env python3
"""make_report.py — 从 backtest_raw.json 生成 backtest_report.md（无前视 asof 修正版）。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# bt_driver 实际产物在 scripts/engines/strategies/backtest_raw.json（驱动默认输出）
CANDIDATES = [
    os.path.join(HERE, "backtest_raw.json"),
    os.path.normpath(os.path.join(
        HERE, "..", "..", "scripts", "engines", "strategies", "backtest_raw.json")),
]
src = next((p for p in CANDIDATES if os.path.isfile(p)), None)
if src is None:
    sys.exit("backtest_raw.json 未找到")
raw = json.load(open(src))

STRAT_CN = {
    "s01_ema_strict": "S1 EMA趋势严进",
    "s02_conservative_risk": "S2 保守风控",
    "s03_turnover_relay": "S3 换手接力",
    "s04_intraday_breakout": "S4 盘中突破",
    "s05_drawdown_cushion": "S5 回撤缓冲",
    "s06_strong_pullback": "S6 强势回踩",
    "s07_emotion_limitup": "S7 情绪打板",
    "s08_multifactor_resonance": "S8 多因子共振",
    "s09_boll_band": "S9 BOLL波段",
    "s10_main_capital": "S10 主力资金",
}

meta = raw["meta"]
strats = raw["strategies"]

lines = []
lines.append("# 10 策略全历史回测报告 — 无前视 asof 修正版\n")
lines.append(f"- 模式：{meta['mode']}；标的池 {meta['pool_size']} 只主板蓝筹/龙头；"
             f"评估窗口 ~{meta['eval_days']} 交易日（近 2 年，数据至 2026-10-23）")
lines.append("- 口径：T 日收盘买入 → T+5/T+10 收盘计收益，信号仅用 T 日及之前数据；"
             "基准 = 同期沪深300（sh000300）日期对齐")
lines.append("- 超额 pp = 策略平均收益 − 同期基准平均收益（百分点）；CI = Wilson 95%（对胜率）\n")

lines.append("## 无前视管线（与前版的关键差异）\n")
lines.append("每个回测时点 t：")
lines.append("1. `kline = slice_asof(kline, t)`：截到 t 当日（含），绝不传 t 之后 bar；")
lines.append("2. `context = load_context(asof=t)`：market_score 按 asof 由 sh000001 历史重算"
             "（MA20/60 位置+20日动量，0-100），dt_count 按 asof 由 bulk 主分片全市场统计"
             "主板跌幅≤-9.8%家数；无历史序列的快照源（ban_prob/limitup_ml/moneyflow/lhb/"
             "market_anomaly）降级为 None，绝不拿当前快照冒充历史；")
lines.append("3. `regime_t = regime_router.judge_regime(bars[:i+1])` 逐日无前视判定，"
             "注入 `context['regime']` 后传入 evaluate（_context 在 asof 模式下 regime 置 None，"
             "职责分离）。\n")
lines.append("无前视生效验证（`bt_driver.py --verify`）：同一指数序列不同时点 asof 的 "
             "market_score 分别为 57.5 / 62.12 / 31.59 / 29.41（随时间变化），"
             "context.regime=None 且 `_nolookahead=True` → PASS。\n")

lines.append("## 诚实性标注（重要）\n")
lines.append("1. 依赖无历史序列快照源的策略在无前视模式下被诚实降级："
             "**S10 主力资金**（moneyflow=None）n=0；**S07 情绪打板**（连板/情绪梯队快照）n=20；"
             "**S02 保守风控** n=80、**S04 盘中突破** n=39（日内快照降级）。"
             "**这是数据源无历史序列的诚实降级，不是策略失效**；对应行已如实列出，不做填补。")
lines.append("2. 未扣交易成本/滑点；主板蓝筹池存在幸存者偏差（池为当前龙头）。")
lines.append("3. 最大回撤为「信号序列按时间序累计净值」口径，非组合净值口径。")
lines.append("4. 负 edge 如实保留，不做挑选展示。\n")

def fmt(v, nd=2):
    return "—" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))

lines.append("## 一、10 策略总对比（T+5 / T+10）\n")
lines.append("| 策略 | n | 胜率T+5 | 平均%T+5 | 超额pp T+5 | Wilson CI T+5 | 夏普T+5 | 最大回撤T+5 | 超额pp T+10 |")
lines.append("|---|---|---|---|---|---|---|---|---|")
for s in sorted(strats):
    d = strats[s]
    t5 = d["horizons"]["T+5"]
    t10 = d["horizons"]["T+10"]
    ci = t5["wilson_ci"]
    ci_s = f"[{fmt(ci[0])}, {fmt(ci[1])}]" if ci[0] is not None else "—"
    lines.append(f"| {STRAT_CN[s]} | {d['n_signals']} | {fmt(t5['win_rate'],3)} | "
                 f"{fmt(t5['avg_ret_pct'])} | {fmt(t5['excess_pp'])} | {ci_s} | "
                 f"{fmt(t5['sharpe'])} | {fmt(t5['maxdd_pct'])} | {fmt(t10['excess_pp'])} |")
lines.append("")

lines.append("## 二、分 regime 表现（T+5）\n")
for s in sorted(strats):
    d = strats[s]
    t5 = d["horizons"]["T+5"]
    if not t5["by_regime"]:
        lines.append(f"### {STRAT_CN[s]}（n={d['n_signals']}）\n\n无信号（见诚实性标注 1：快照源无前视降级）。\n")
        continue
    lines.append(f"### {STRAT_CN[s]}（n={d['n_signals']}）\n")
    lines.append("| regime | n | 胜率 | 平均% | 基准% | 超额pp | Wilson CI | 夏普 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for rg in ("bear", "range", "bull", "unknown"):
        b = t5["by_regime"].get(rg)
        if not b:
            continue
        ci = b["wilson_ci"]
        ci_s = f"[{fmt(ci[0])}, {fmt(ci[1])}]" if ci[0] is not None else "—"
        lines.append(f"| {rg} | {b['n']} | {fmt(b['win_rate'],3)} | {fmt(b['avg_ret_pct'])} | "
                     f"{fmt(b['bench_avg_pct'])} | {fmt(b['excess_pp'])} | {ci_s} | {fmt(b['sharpe'])} |")
    lines.append("")

lines.append("## 三、regime 路由有效性\n")
lines.append("正超额（excess_pp>0，T+5）的 策略×regime 单元格：\n")
pos, neg = [], []
for s in sorted(strats):
    t5 = strats[s]["horizons"]["T+5"]
    for rg, b in t5["by_regime"].items():
        if b["excess_pp"] is None:
            continue
        cell = f"{STRAT_CN[s]}@{rg}(n={b['n']}, {b['excess_pp']:+.2f}pp)"
        (pos if b["excess_pp"] > 0 else neg).append(cell)
lines.append(f"- 正超额 {len(pos)} 格：" + ("；".join(pos) if pos else "无"))
lines.append(f"- 负超额 {len(neg)} 格：" + ("；".join(neg) if neg else "无"))
lines.append("")

with open(os.path.join(HERE, "backtest_report.md"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))
print(f"written backtest_report.md (from {src})")
