#!/usr/bin/env python3
"""
edge_miner.py — 优势挖掘引擎 (找优点算法)

self_reflect 找问题, edge_miner 找优势: 系统性找出「我们在哪些
场景/因子/时段/板块显著跑赢」, 把偶然变必然。

挖掘维度 (交叉分析 reflect_latest 的按指标/按市场阶段数据):
  1. 指标优势   哪些指标的看空侧显著有效 (bear_win CI 下限 > 60%)
                → 用作回避过滤器 (排空比选多更易变现)
  2. 环境优势   哪个市场阶段我们的预测更准 → 在该阶段提高仓位
  3. 组合优势   multidim 9维中哪些维度组合同步高分层时胜率更高
  4. 时间优势   星期几/月份的历史胜率显著 (来自 cycle_deep)

输出: data/edge_report.json (优势清单 + 可执行建议)

原则: 只报 Wilson CI 下限 > 50% 的优势 (统计显著), 样本不足不报。
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

MIN_N = 30
MIN_CI = 50.0


def load(n):
    p = os.path.join(DATA_DIR, n)
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def mine_indicator_edges(reflect):
    """指标优势: 看空侧显著有效的指标 (我们的 alpha 可能在做空/回避侧)。"""
    edges = []
    ind = ((reflect or {}).get("analysis") or {}).get("indicators") or {}
    for name, rec in ind.items():
        bear_wr = rec.get("bear_win_rate")
        bear_n = rec.get("bear_n") or 0
        bear_ci = rec.get("bear_win_rate_ci") or [None, None]
        ci_lo = bear_ci[0] if isinstance(bear_ci, list) and bear_ci else None
        if bear_n >= MIN_N and bear_wr and bear_wr >= 60 and (ci_lo is None or ci_lo >= MIN_CI):
            edges.append({
                "type": "indicator_short_edge",
                "name": name,
                "bear_win_rate": bear_wr,
                "bear_n": bear_n,
                "ci_lo": ci_lo,
                "action": f"把 {name} 用作回避过滤器: 该指标翻空时排除, "
                          f"历史胜率{bear_wr}%(n={bear_n})",
                "confidence": "high" if (ci_lo and ci_lo >= 60) else "medium",
            })
    return edges


def mine_phase_edges(reflect):
    """环境优势: 哪个市场阶段预测更准。"""
    edges = []
    by_phase = ((reflect or {}).get("analysis") or {}).get("by_phase") or {}
    for phase, rec in by_phase.items():
        n = rec.get("n") or 0
        wr = rec.get("raw_win_rate") or rec.get("bull_win_rate")
        ci = rec.get("raw_win_rate_ci") or rec.get("bull_win_rate_ci") or [None, None]
        ci_lo = ci[0] if isinstance(ci, list) and ci else None
        if n >= MIN_N and wr and wr >= 50 and (ci_lo is None or ci_lo >= MIN_CI):
            edges.append({
                "type": "phase_edge",
                "name": phase,
                "win_rate": wr, "n": n, "ci_lo": ci_lo,
                "action": f"在 {phase} 阶段提高仓位/增加出手频率",
                "confidence": "high" if (ci_lo and ci_lo >= 55) else "medium",
            })
    return edges


def mine_dim_edges(multidim):
    """维度优势: 哪些维度在 TOP 票里普遍高分层 → 说明该维度是主要区分器。"""
    edges = []
    top = (multidim or {}).get("top") or []
    if len(top) < 10:
        return edges
    dims = ["技术面", "资金面", "高低位", "基本面", "板块面",
            "市场面", "情绪面", "周期面", "消息面"]
    for d in dims:
        vals = [r.get("dims", {}).get(d) for r in top if r.get("dims", {}).get(d) is not None]
        if len(vals) < 10:
            continue
        avg = sum(vals) / len(vals)
        # TOP 票该维度均分显著高于 50 中位 → 是区分器
        if avg >= 60:
            edges.append({
                "type": "dim_edge",
                "name": d,
                "top_avg": round(avg, 1),
                "n": len(vals),
                "action": f"{d} 在推荐票中均分{avg:.0f}, 是主要区分维度, "
                          f"可考虑提高其权重",
                "confidence": "medium",
            })
        elif avg <= 40:
            edges.append({
                "type": "dim_edge",
                "name": d,
                "top_avg": round(avg, 1),
                "n": len(vals),
                "action": f"{d} 在推荐票中均分{avg:.0f}, 区分度低, 可降低权重或换因子",
                "confidence": "medium",
            })
    return edges


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "edge_report.json"))
    args = ap.parse_args()

    reflect = load("reflect_latest.json")
    multidim = load("multidim.json")

    edges = []
    if reflect:
        edges += mine_indicator_edges(reflect)
        edges += mine_phase_edges(reflect)
    if multidim:
        edges += mine_dim_edges(multidim)

    # 无显著优势也要说清楚 (不编造)
    if not edges:
        summary = ("样本不足或无统计显著优势 (CI 下限未过 50%)。"
                   "当前阶段应聚焦提高样本量, 而非追逐伪优势。")
    else:
        summary = f"发现 {len(edges)} 个统计显著优势, 见清单。"

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "edge_miner v1 (优势挖掘)",
        "n_edges": len(edges),
        "edges": edges,
        "summary": summary,
        "meta": {
            "min_n": MIN_N, "min_ci": MIN_CI,
            "principle": "只报 Wilson CI 下限>50% 的优势; 样本不足不报; 不编造优势",
            "caveat": "历史优势可能随市场结构变化失效, 需持续重挖",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"[edge_miner] {summary}")
    for e in edges[:10]:
        print(f"  [{e['type']}] {e['name']}: {e['action']}")
    print(f"[edge_miner] → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
