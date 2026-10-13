#!/usr/bin/env python3
"""
ai_commentary.py — GitHub AI (Models) 盘面解读

设计原则: AI 是「增强」而非「依赖」
  - 无 token / 无额度 / 调用失败 → 自动降级为规则引擎解读 (build_commentary)
  - 有 token → 用 GitHub Models 生成更自然的盘面解读
  - 输出永远带 meta.ai_used 标记, 用户能分辨是 AI 还是规则生成的

GitHub Models 接入:
  endpoint: https://models.github.ai/inference/chat/completions
  认证:     Bearer <GITHUB_TOKEN>  (需要 models:read 或 Copilot 额度)
  模型:     openai/gpt-4o-mini (免费额度内)

用法:
  GITHUB_TOKEN=ghp_xxx python3 ai_commentary.py --out data/ai_commentary.json
  python3 ai_commentary.py --fallback-only    # 只跑规则引擎 (测试降级)
"""
import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

MODELS_URL = "https://models.github.ai/inference/chat/completions"
MODEL = "openai/gpt-4o-mini"


def call_ai(prompt, token, timeout=60):
    """调 GitHub Models, 失败返回 None (调用方降级)"""
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content":
             "你是A股资深复盘分析师。基于给定数据写盘面解读, "
             "要求: 1) 只依据给定数据, 不编造 2) 给出明确多空倾向和依据 "
             "3) 列出2-3个主要风险 4) 300字内 5) 用markdown"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 800,
    }
    req = urllib.request.Request(
        MODELS_URL,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
            return (d.get("choices") or [{}])[0].get("message", {}).get("content")
    except urllib.error.HTTPError as e:
        print(f"[ai] HTTP {e.code}: {e.read()[:200]}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[ai] {type(e).__name__}: {e}", file=sys.stderr)
        return None


def build_prompt(data):
    """把当日数据组织成 prompt"""
    m = data.get("market", {})
    ph = data.get("phase", {})
    to = m.get("turnover") or {}
    lines = [
        f"日期: {data.get('date')}",
        f"指数: " + ", ".join(
            f"{i.get('name')} {i.get('price')}({i.get('chg_pct'):+.2f}%)"
            for i in (m.get("indices") or [])[:5]),
        f"涨停 {m.get('limit_up')} 家, 跌停 {m.get('limit_down')} 家, "
        f"炸板 {m.get('broken')} 家, 封板率 {m.get('zt_rate')}%",
        f"上涨 {m.get('advance')} 家 / 下跌 {m.get('decline')} 家 "
        f"(上涨占比 {m.get('up_pct')}%)",
        f"两市成交额 {to.get('total_yi', '—')} 亿元",
        f"情绪周期: {ph.get('stage')} (评分 {ph.get('score')})",
        "题材热度: " + ", ".join(
            f"{t.get('name')}({t.get('news_count')}条)"
            for t in (data.get("themes") or [])[:6]),
    ]
    return "\n".join(lines)


def rule_based(data):
    """规则引擎降级解读 (复用 daily_brief 的逻辑)"""
    m = data.get("market", {})
    ph = data.get("phase", {})
    up = m.get("advance") or 0
    down = m.get("decline") or 0
    ratio = m.get("up_pct") or 0
    zt, dt = m.get("limit_up") or 0, m.get("limit_down") or 0

    parts = []
    if ratio >= 60:
        parts.append(f"普涨格局, 上涨占比 {ratio}%, 赚钱效应强")
    elif ratio >= 40:
        parts.append(f"涨跌互现, 上涨占比 {ratio}%")
    else:
        parts.append(f"跌多涨少, 上涨占比仅 {ratio}%, 亏钱效应明显")
    parts.append(f"涨停 {zt} / 跌停 {dt}, 情绪处「{ph.get('stage')}」")
    if dt >= zt:
        parts.append("跌停超过涨停, 短期风险释放中")
    return {
        "text": "\n".join(f"- {p}" for p in parts),
        "ai_used": False,
        "model": None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/root/workspace/stocks-ai-view/data/latest.json")
    ap.add_argument("--out", required=True)
    ap.add_argument("--fallback-only", action="store_true")
    args = ap.parse_args()

    now = datetime.now(CST)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    try:
        data = json.load(open(args.data, encoding="utf-8"))
    except Exception as e:
        print(f"读取数据失败: {e}", file=sys.stderr)
        return 1

    result = None
    err = None
    if token and not args.fallback_only:
        print(f"[ai] 调用 GitHub Models ({MODEL})...", flush=True)
        text = call_ai(build_prompt(data), token)
        if text:
            result = {"text": text, "ai_used": True, "model": MODEL}
        else:
            err = "AI 调用失败, 降级规则引擎"
    elif not token:
        err = "未提供 GITHUB_TOKEN, 降级规则引擎"

    if result is None:
        result = rule_based(data)
        if err:
            result["note"] = err

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "date": data.get("date"),
        **result,
        "meta": {
            "ai_used": result["ai_used"],
            "model": result.get("model"),
            "fallback": not result["ai_used"],
            "source": "GitHub Models (gpt-4o-mini)" if result["ai_used"]
                      else "规则引擎 (无 AI)",
            "prompt_data": ["indices", "limit_up/down/broken", "advance/decline",
                            "turnover", "phase", "themes"],
            "caveat": "AI 解读基于当日公开数据, 可能存在幻觉, 需人工复核; "
                      "不构成投资建议",
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print(f"ai_used={out['meta']['ai_used']} → {args.out}")
    print(out["text"][:400])
    return 0


if __name__ == "__main__":
    sys.exit(main())
