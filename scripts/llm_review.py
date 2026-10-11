#!/usr/bin/env python3
"""LLM 辅助复盘生成器 (GitHub Actions 版, 零本地依赖)

两种模式:
  1. --llm ghc: 用 GitHub Models (ghc model run, GITHUB_TOKEN 免费额度) 调 gpt-4o-mini
     生成当日复盘; 需要 actions/setup-ghc + 模型访问权限
  2. --llm none (默认): 不调 LLM, 用规则引擎 hot_impact 的输出直接生成确定性复盘 MD
     (保证 Actions 永不因 LLM 配额失败)

输入: latest.json + hot_impact.json (同仓 data/ 目录)
输出: review/YYYY-MM-DD.md
"""
import argparse, datetime, json, os, subprocess, sys, textwrap


def load_data(data_dir):
    latest = json.load(open(os.path.join(data_dir, "latest.json"), encoding="utf-8"))
    try:
        hi = json.load(open(os.path.join(data_dir, "hot_impact.json"), encoding="utf-8"))
    except FileNotFoundError:
        hi = {"reports": []}
    return latest, hi


def rule_review(latest, hi):
    """确定性复盘: 情绪/主线/热点推演 (无 LLM 依赖)"""
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    m = latest["market"]
    ph = latest.get("phase", {})
    lines = [
        f"# 每日复盘 {now.strftime('%Y-%m-%d')} (TSP v4 规则引擎)",
        "",
        f"- 情绪: **{ph.get('stage','?')}** {ph.get('score','?')}分 · {ph.get('desc','')}",
        f"- 涨停 {m['limit_up']} / 跌停 {m['limit_down']} / 炸板 {m['broken']} · 封板率 {m['zt_rate']}%",
        f"- 晋级率 {ph.get('promo_rate','?'):.0%} · 最高 {ph.get('max_boards','?')} 板",
        "",
        "## 主线",
    ]
    for ml in (latest.get("mainlines") or [])[:8]:
        lines.append(f"- {ml.get('name','?')} (分 {ml.get('score','?')}, 领涨 {ml.get('leading_name','—')})")
    lines += ["", "## 热点→走势推演 TOP"]
    for r in (hi.get("reports") or [])[:6]:
        sigs = [s["signal"] for s in r["stocks"]]
        lines.append(f"- **{r['theme']}** ({r.get('news_count')}条): " +
                     ", ".join(f"{s['name']}[{x}]" for s, x in zip(r["stocks"], sigs)))
    lines += ["", "## 风险提示", "- 预期区间由 0.5×5日动量+0.3×热点强度+0.2×涨停热度 估计, 仅供研究"]
    return "\n".join(lines)


def llm_review(latest, hi, model="meta-llama/llama-3.1-8b-instant"):
    """GitHub Models 调 LLM 生成复盘 (ghc model run)"""
    ctx = json.dumps({"market": latest["market"], "phase": latest.get("phase"),
                      "themes": [t["name"] for t in (latest.get("themes") or [])][:10],
                      "hot_impact": [
                          {"theme": r["theme"], "stocks": [
                              {"code": s["code"], "name": s["name"], "signal": s["signal"],
                               "ret_5d": s["ret_5d"], "pos20": s["pos20"]} for s in r["stocks"]]}
                          for r in (hi.get("reports") or [])[:6]],
                      "funds_top5": [(f["code"], f["name"], f.get("ret_1y")) for f in (latest.get("fund_rank") or [])[:5]]},
                     ensure_ascii=False)
    prompt = textwrap.dedent(f"""
        你是 A 股复盘分析师。基于以下当日数据 (JSON), 写一份 300 字内的复盘:
        1) 市场情绪与主线判断 2) 热点推演中哪些信号最值得关注 3) 明日情景推演 (进攻/防守两种)。
        直接输出 markdown, 不要免责声明。
        数据:
        {ctx}
    """)
    r = subprocess.run(["ghc", "model", "run", "--model", model, "--size", "large",
                        "--prompt", prompt], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print(f"[llm] ghc failed: {r.stderr[:300]}", file=sys.stderr)
        return None
    return r.stdout


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="data")
    p.add_argument("--out", default=None)
    p.add_argument("--llm", choices=["ghc", "none"], default="none")
    p.add_argument("--model", default="meta-llama/llama-3.1-8b-instant")
    args = p.parse_args()

    latest, hi = load_data(args.data_dir)
    md = None
    if args.llm == "ghc":
        md = llm_review(latest, hi, args.model)
    if not md:
        md = rule_review(latest, hi)
    ts = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)).strftime("%Y-%m-%d")
    out = args.out or f"review/{ts}.md"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    print(f"written {out} (llm={args.llm})", flush=True)


if __name__ == "__main__":
    main()
