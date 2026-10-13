#!/usr/bin/env python3
"""
build_daily_brief.py — 盘前日报 (只在非交易时段生成)

用户需求:
  「每日总结开盘到收盘时间不进行更新, 只通过不在开盘的时间内进行实时热点、
    情绪、财经、政事等多方面的数据分析和总结, 加上去开盘前的分析、推股、
    昨日的推股策略和今日的胜率分析做出的今日总结」

设计:
  - 仅在非交易时段生成 (避免与盘中实时数据混) — 检测当前时间,
    盘中调用直接退出 (除非 --force)
  - 内容 = 4 个板块:
      1. 盘前分析   大盘周期位置 + 情绪周期 + 季节性 + 外围/隔夜
      2. 今日推股   ai_picks TOP N (附理由与共振度)
      3. 昨日复盘   昨日推股 vs 今日实际 → 胜率 (从 reflect_engine 取)
      4. 热点与情绪  实时热点 + 涨跌家数 + 成交额 + 两融
  - 输出 Markdown (renderSummary 已支持 md → HTML) + JSON

用法:
  python3 build_daily_brief.py --out data/daily_brief.json
  python3 build_daily_brief.py --force     # 盘中强制执行 (调试用)
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
from paths import AI_VIEW_DATA
DATA = AI_VIEW_DATA


def load(name):
    p = os.path.join(DATA, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def in_trading_hours(now=None):
    """CST 09:15-15:05 算交易时段 (含盘前集合竞价)。"""
    now = now or datetime.now(CST)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 555 <= mins <= 905


def build(now):
    """组装日报内容, 返回 (markdown, sections)。"""
    lines = []
    sec = {}

    # ── 1. 盘前分析 ──
    cd = load("cycle_deep.json") or {}
    cy = load("cycle.json") or {}
    status = cd.get("status") or {}
    dims = status.get("dims") or []
    phase = cy.get("market_phase") or status.get("bias") or "—"
    mkt_score = cy.get("market_score")

    s1 = []
    s1.append(f"## 一、盘前分析")
    s1.append("")
    s1.append(f"- 大盘周期位置: **{phase}**"
              + (f" (评分 {mkt_score})" if mkt_score is not None else ""))
    if status:
        s1.append(f"- 周期深析: {status.get('index', '上证指数')} "
                  f"{status.get('date', '')} 收盘 {status.get('close')}, "
                  f"综合 {status.get('composite'):+d} → {status.get('bias')}")
    if dims:
        s1.append("- 五维信号:")
        for d in dims:
            b = {1: "偏多", -1: "偏空", 0: "中性"}.get(d.get("bias", 0), "—")
            s1.append(f"  - {d['dim']}: {d['now']} → {b} ({d['exp']})")
    # 季节性
    season = (cd.get("season") or {}).get("month") or {}
    if season:
        m = str(now.month)
        cur = season.get(m)
        if cur:
            s1.append(f"- {m}月季节性 (全史): 平均 {cur['avg']:+.3f}%/日, "
                      f"胜率 {cur['win']}% (n={cur['n']})")
    # 昨日涨跌停/成交
    lt = load("latest.json") or {}
    mk = lt.get("market") or {}
    if mk:
        s1.append(f"- 最近收盘: 涨停 {mk.get('limit_up', '—')} / 跌停 {mk.get('limit_down', '—')} / "
                  f"炸板 {mk.get('broken', '—')}")
        adv, dec = mk.get("advance"), mk.get("decline")
        if adv is not None and dec is not None:
            s1.append(f"- 涨跌家数: 上涨 {adv} / 下跌 {dec} (上涨占比 {mk.get('up_pct', '—')}%)")
        to = (mk.get("turnover") or {})
        if to.get("total_yi"):
            s1.append(f"- 两市成交额: {to['total_yi']} 亿元 "
                      f"(沪 {to.get('parts', {}).get('沪市', '—')} / 深 {to.get('parts', {}).get('深市', '—')})")
    # 两融
    mg = lt.get("margin") or []
    if len(mg) >= 2:
        r = mg[0]
        prev = mg[1]
        if r.get("rzye") and prev.get("rzye"):
            chg = (r["rzye"] - prev["rzye"]) / prev["rzye"] * 100
            s1.append(f"- 两融: 融资余额 {r['rzye']/1e8:.0f} 亿, 环比 {chg:+.3f}%")
    s1.append("")
    sec["premarket"] = "\n".join(s1)
    lines += s1

    # ── 2. 今日推股 ──
    ap = load("ai_picks.json") or {}
    picks = ap.get("picks") or []
    s2 = [f"## 二、今日推股 (AI 多指标共振)", ""]
    if picks:
        s2.append(f"筛选规则: {ap.get('anchor', '')}")
        s2.append(f"扫描 {ap.get('global', {}).get('n_scanned', '—')} 只, "
                  f"入围 {ap.get('global', {}).get('n_kept', '—')} 只, 取 TOP{len(picks)}")
        s2.append("")
        s2.append("| 代码 | 名称 | 现价 | 涨跌 | 综合分 | 共振度 | 多/空 | 行业 | 核心信号 |")
        s2.append("|------|------|------|------|--------|--------|-------|------|----------|")
        for p in picks[:12]:
            chg = f"{p['chg_pct']:+.2f}%" if p.get("chg_pct") is not None else "—"
            sig = "; ".join(p.get("signals", [])[:2])[:40]
            s2.append(f"| {p['code']} | {str(p.get('name'))[:8]} | {p.get('price')} | {chg} | "
                      f"{p.get('score')} | {p.get('resonance', 0):.2f} | "
                      f"{p.get('bull')}/{p.get('bear')} | {str(p.get('industry'))[:8]} | {sig} |")
    else:
        s2.append("暂无推股 (ai_picks 未生成或全被过滤)")
    s2.append("")
    sec["picks"] = "\n".join(s2)
    lines += s2

    # ── 3. 昨日推股胜率 ──
    rf = load("reflect_latest.json") or {}
    an = (rf.get("analysis") or {})
    overall = an.get("overall") or {}
    s3 = ["## 三、昨日推股 · 今日胜率验证", ""]
    if overall and any(v.get("n") for v in overall.values()):
        for h, v in sorted(overall.items()):
            if not v.get("n"):
                continue
            s3.append(f"- {h} 日后: 看多胜率 **{v.get('bull_win_rate')}%** (n={v.get('bull_n')}) | "
                      f"看空胜率 {v.get('bear_win_rate')}% (n={v.get('bear_n')}) | "
                      f"平均收益 {v.get('avg_ret_pct')}%")
        # 指标质量
        ranked = an.get("ranked") or []
        if ranked:
            ok_ind = [r for r in ranked if r.get("evidence") == "ok" and (r.get("quality") or 0) > 55]
            weak = [r for r in ranked if r.get("evidence") == "ok" and (r.get("quality") or 0) < 45]
            if ok_ind:
                s3.append(f"- 有效指标: " + ", ".join(f"{r['indicator']}({r['quality']:.0f})" for r in ok_ind[:5]))
            if weak:
                s3.append(f"- 噪声指标 (建议降权): " + ", ".join(f"{r['indicator']}({r['quality']:.0f})" for r in weak[:5]))
        s3.append(f"- 样本量: {rf.get('n_records', 0)} 条 / {rf.get('n_pred_files', 0)} 个预测快照")
    else:
        s3.append("暂无可验证样本 (需要历史预测快照 + 对应未来K线)")
    s3.append("")
    sec["winrate"] = "\n".join(s3)
    lines += s3

    # ── 4. 热点与情绪 ──
    hi = load("hot_impact.json") or {}
    reports = hi.get("reports") or []
    cm = lt.get("commentary") or {}
    s4 = ["## 四、热点与市场情绪", ""]
    if cm.get("summary"):
        s4.append("盘面解读:")
        for x in cm["summary"]:
            s4.append(f"- {x}")
    if cm.get("hotspots"):
        s4.append("- 题材热度: " + ", ".join(
            f"{h['theme']}({h.get('heat')})" for h in cm["hotspots"][:6]))
    if reports:
        s4.append(f"- 热点推演: {len(reports)} 个主题 ("
                  + ", ".join(r.get("theme", "") for r in reports[:5]) + ")")
    if cm.get("risks"):
        s4.append("- 风险提示:")
        for r in cm["risks"]:
            s4.append(f"  - ⚠ {r}")
    s4.append("")
    sec["hot"] = "\n".join(s4)
    lines += s4

    # 头部
    header = [
        f"# A股盘前日报 {now:%Y-%m-%d %A}",
        "",
        f"> 生成时间: {now:%Y-%m-%d %H:%M CST} · 非交易时段生成 · 数据源见各节",
        f"> 免责声明: 本报告由规则引擎基于公开数据自动生成, 仅供研究参考, 不构成投资建议。",
        "",
    ]
    md = "\n".join(header + lines)
    return md, sec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--force", action="store_true", help="盘中强制执行")
    args = ap.parse_args()

    now = datetime.now(CST)
    if in_trading_hours(now) and not args.force:
        print(f"[brief] 当前为交易时段 ({now:%H:%M} CST), 盘前日报不生成。", flush=True)
        print("        日报只在非交易时段生成 (09:15 前 / 15:05 后 / 周末)。")
        return 0

    # 09:15 前 = 盘前 (为今天做准备); 15:05 后 = 盘后 (总结今天)
    trading = in_trading_hours(now)
    session = "盘前" if (now.hour < 9 or (now.hour == 9 and now.minute < 15)) else "盘后"
    if trading:
        session = "盘中(强制)"
    md, sec = build(now)

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "session": session,
        "text": md,
        "sections": {k: v for k, v in sec.items()},
        "meta": {
            "generated_in": "非交易时段" if not in_trading_hours(now) else "交易时段(--force)",
            "sources": ["cycle_deep.json", "cycle.json", "latest.json",
                        "ai_picks.json", "reflect_latest.json", "hot_impact.json"],
            "sections_desc": {
                "premarket": "大盘周期位置 + 五维信号 + 季节性 + 昨日涨跌停/成交/两融",
                "picks": "AI 多指标共振推股 TOP N (含理由)",
                "winrate": "昨日推股 vs 今日实际的胜率验证 + 指标质量排名",
                "hot": "实时热点 + 题材热度 + 盘面解读 + 风险提示",
            },
            "caveat": "规则引擎自动生成, 无人工审核; 胜率受样本量限制, "
                      "样本不足时结论标注 insufficient",
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    md_path = args.out.replace(".json", ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"[brief] {now:%Y-%m-%d %H:%M} CST ({session}) 生成完成")
    print(f"  章节: {list(sec.keys())}")
    print(f"  → {args.out}")
    print(f"  → {md_path}")
    print()
    print(md[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
