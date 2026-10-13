#!/usr/bin/env python3
"""
gitcode_ai_review.py — GitCode AI 每日复盘 (消耗 GitCode 每月 AI 额度)

模型: deepseek-ai/DeepSeek-V4-Flash (主) / Qwen/Qwen3-30B-A3B-Instruct-2507 (备)
端点: https://gitcode.com/api/v5/chat/completions (Bearer token)

为什么用 GitCode AI 而不是 GitHub Models:
  - 分散配额压力 (GitHub Models 也有额度限制)
  - GitCode 每月送 AI 额度, 不用白不用 (用户明确要求)
  - DeepSeek-V4-Flash 对中文金融文本效果好

输入: data/ 下的 market_now / multidim / decision / reflect / self_reflect / news_digest
输出: data/ai_review.md + data/ai_review.json

用法:
  GITCODE_TOKEN=<token> python3 gitcode_ai_review.py
  python3 gitcode_ai_review.py --dry-run    # 只打印 prompt 不调用
"""
import argparse
import glob
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

# GitCode AI 默认 token (用户提供; 也可用环境变量 GITCODE_TOKEN 覆盖)
# GitCode PAT 从环境变量 GITCODE_TOKEN 读 (勿硬编码)
DEFAULT_TOKEN = os.environ.get("GITCODE_TOKEN", "")
MODELS = ["deepseek-ai/DeepSeek-V4-Flash", "Qwen/Qwen3-30B-A3B-Instruct-2507"]
ENDPOINT = "https://gitcode.com/api/v5/chat/completions"


def load(name):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def build_context():
    """把关键数据压成 prompt 上下文 (控制 token 用量)。"""
    mn = load("market_now.json") or {}
    md = load("multidim.json") or {}
    dec = load("decision.json") or {}
    sr = load("self_reflect.json") or {}
    nd = load("news_digest.json") or {}

    m = mn.get("market") or {}
    parts = [
        f"日期: {mn.get('date', datetime.now(CST).strftime('%Y%m%d'))}",
        f"指数: " + ", ".join(f"{i.get('name')}{i.get('price')}({i.get('chg_pct'):+.2f}%)"
                              for i in (m.get("indices") or [])[:4]),
        f"涨跌停: 涨停{m.get('limit_up')} 跌停{m.get('limit_down')} 炸板{m.get('broken')} "
        f"封板率{m.get('zt_rate')}%",
        f"涨跌家数: 涨{m.get('advance')} 跌{m.get('decline')} 上涨占比{m.get('up_pct')}%",
        f"两市成交: {(m.get('turnover') or {}).get('total_yi')}亿",
    ]
    # 题材热度
    ths = mn.get("ths_strong") or []
    if ths:
        themes = {}
        for s in ths[:30]:
            for r in str(s.get("reason", "")).split("+"):
                r = r.strip()
                if r:
                    themes[r] = themes.get(r, 0) + 1
        top_themes = sorted(themes.items(), key=lambda x: -x[1])[:8]
        parts.append("涨停题材: " + ", ".join(f"{k}×{v}" for k, v in top_themes))
    # 多维 TOP5
    top = (md.get("top") or [])[:5]
    if top:
        parts.append("多维评分TOP5: " + "; ".join(
            f"{r.get('name')}(综合{r.get('composite')}, "
            f"资{(r.get('dims') or {}).get('资金面')}, 位{(r.get('dims') or {}).get('高低位')})"
            for r in top))
    # 决策卡
    cards = dec.get("cards") or []
    if cards:
        buys = [c for c in cards if c.get("action") == "buy"][:5]
        parts.append(f"决策引擎: {dec.get('market_regime')}市场, {dec.get('position_advice', {}).get('label')}, "
                     f"买入信号 {len(buys)} 只: " + ", ".join(f"{c.get('name')}({c.get('composite')})" for c in buys))
    # 反思结论
    v = sr.get("verdict") or {}
    if v.get("conclusion"):
        parts.append(f"历史胜率反思: {v['conclusion']}")
    # 新闻要点
    news = []
    for cat, arr in (nd.get("categories") or {}).items():
        if isinstance(arr, list):
            news.extend(arr[:3])
    if news:
        parts.append("重要资讯: " + " | ".join(str(n.get("title", ""))[:40] for n in news[:6]))
    return "\n".join(parts)


def ask_gitcode_ai(prompt, token, max_tokens=1200):
    """调 GitCode AI, 主模型失败自动切备用模型。返回 (模型名, 内容)。"""
    for model in MODELS:
        try:
            body = json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content":
                        "你是A股量化复盘助手。基于给定数据写结构化复盘: "
                        "①今日盘面总结(情绪/资金/结构) ②主线与题材 ③明日关注与风险 ④策略建议。"
                        "要求: 数据驱动, 每个判断引用给定数据; 不超过400字; 结尾注明不构成投资建议。"},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            }).encode()
            req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            r = json.load(urllib.request.urlopen(req, timeout=150))
            content = ((r.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
            if content:
                return model, content
        except Exception as e:
            print(f"[ai_review] {model} 失败: {str(e)[:120]}", file=sys.stderr)
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-md", default=os.path.join(DATA_DIR, "ai_review.md"))
    ap.add_argument("--out-json", default=os.path.join(DATA_DIR, "ai_review.json"))
    ap.add_argument("--dry-run", action="store_true", help="只打印 prompt 不调用")
    args = ap.parse_args()

    token = os.environ.get("GITCODE_TOKEN") or DEFAULT_TOKEN
    ctx = build_context()
    prompt = f"以下是今日A股量化数据, 请写复盘:\n\n{ctx}"

    if args.dry_run:
        print(prompt)
        return 0

    print(f"[ai_review] 调用 GitCode AI ({MODELS[0]}) ...", flush=True)
    model, content = ask_gitcode_ai(prompt, token)

    now = datetime.now(CST)
    out = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "gitcode_ai_review (GitCode AI 额度)",
        "model": model,
        "ok": bool(content),
        "review": content or "(AI 调用失败, 保留规则引擎复盘)",
        "context_len": len(ctx),
        "meta": {
            "endpoint": ENDPOINT,
            "models_tried": MODELS,
            "note": "消耗 GitCode 每月 AI 额度; 失败时不影响其他管线",
            "caveat": "AI 生成内容仅供研究参考, 不构成投资建议",
        },
    }
    for p in (args.out_md, args.out_json):
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    md = (f"# AI 每日复盘 {now:%Y-%m-%d %H:%M} CST\n\n"
          f"模型: `{model}` (GitCode AI)\n\n{content or '(调用失败)'}\n\n"
          f"---\n\n<details><summary>数据上下文</summary>\n\n```\n{ctx}\n```\n\n</details>\n\n"
          f"*AI 生成, 仅供研究参考, 不构成投资建议。*\n")
    with open(args.out_md, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"[ai_review] 模型={model} 长度={len(content or '')} 字")
    print(f"[ai_review] → {args.out_md} / {args.out_json}")
    return 0 if content else 1


if __name__ == "__main__":
    sys.exit(main())
