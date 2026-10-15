#!/usr/bin/env python3
"""
gitcode_ai_review.py — GitCode AI 每日复盘 (消耗 GitCode 每月 AI 额度)

模型: deepseek-ai/DeepSeek-V4-Flash (主) / Qwen/Qwen3-30B-A3B-Instruct-2507 (备)
端点: https://gitcode.com/api/v5/chat/completions (Bearer token)

为什么用 GitCode AI 而不是 GitHub Models:
  - 分散配额压力 (GitHub Models 也有额度限制)
  - GitCode 每月送 AI 额度, 不用白不用 (用户明确要求)
  - DeepSeek-V4-Flash 对中文金融文本效果好

输入 (全部本地 JSON, 不发额外请求):
  market_now        盘面/涨跌停/题材
  multidim          十维评分 (技术/资金/高低位/基本/板块/市场/情绪/周期/消息/海外)
  decision          决策卡 (市场环境/仓位/买入信号)
  reflect_latest    历史胜率反思
  self_reflect      引擎自我反思
  overseas_linkage  海外关联性 (corr/beta/滞后/隔夜传导)
  global_markets    全球六大市场行情
  sector_moneyflow  板块资金 (主力净额)
  premarket_picks   今日推股 (A/B/C 三级 + 入场/止损/目标)
  close_review      收盘复盘与昨日推股验证
  news_digest       24h 资讯

输出: data/ai_review.md + data/ai_review.json

用法:
  GITCODE_TOKEN=<token> python3 gitcode_ai_review.py
  python3 gitcode_ai_review.py --dry-run    # 只打印 prompt 不调用
"""
import argparse
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

# GitCode PAT 从环境变量 GITCODE_TOKEN 读 (勿硬编码)
DEFAULT_TOKEN = os.environ.get("GITCODE_TOKEN", "")
MODELS = ["deepseek-ai/DeepSeek-V4-Flash", "Qwen/Qwen3-30B-A3B-Instruct-2507"]
ENDPOINT = "https://gitcode.com/api/v5/chat/completions"

SYSTEM_PROMPT = """你是A股量化复盘助手, 基于算法与引擎的产出写结构化复盘。要求:

1. 每个判断必须引用给定数据, 不得脱离数据臆测; 数据没提的就写"数据未覆盖"。
2. 情绪面/高低位等维度必须做双向解读: 低不一定是坏事(可能是冰点反转机会),
   高也不一定是好事(可能是派发区)。禁止单向断言"高分即优"。
3. 海外关联要用 corr/beta/滞后/隔夜传导四个量说话, 说明传导强度与方向,
   并指出高 beta 在下跌时同样被放大。
4. 复盘昨日推股: 逐条对照 verdict (止盈/止损/平盘), 不回避亏损。
5. 若历史胜率低于随机 (50%), 明确指出模型信号当前失效, 建议降低权重。
6. 弱市 (gate=defensive) 时仍要给出候选与观察名单, 但必须标注逆势风险与轻仓要求。
7. 结构: ①盘面总结 ②主线与题材 ③海外传导 ④昨日推股验证 ⑤明日关注与风险
   ⑥策略建议 (含仓位) ⑦算法自我反思
8. 不超过 700 字; 结尾注明不构成投资建议。"""


def load(name):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


_SI = None


def stock_name(code):
    """推股表部分记录 name 为空 (上游引擎未带), 用 search_index 兜底。"""
    global _SI
    if not code:
        return ""
    if _SI is None:
        _SI = load("search_index.json") or {}
    bare = str(code)
    # 先 sz 再 sh: 000937 在 sz 是冀中能源(股价4.45), 在 sh 是指数「800公用」
    # (2560.53), 弄反会把个股显示成指数名。
    for pre in ("sz", "sh", "bj"):
        v = _SI.get(pre + bare)
        if v and v.get("n"):
            return v["n"]
    return str(code)


def build_context():
    """把算法/引擎产出压成 prompt 上下文 (控制 token 用量)。"""
    mn = load("market_now.json") or {}
    md = load("multidim.json") or {}
    dec = load("decision.json") or {}
    sr = load("self_reflect.json") or {}
    rl = load("reflect_latest.json") or {}
    ol = load("overseas_linkage.json") or {}
    gm = load("global_markets.json") or {}
    pm = load("premarket_picks.json") or {}
    cr = load("close_review.json") or {}
    nd = load("news_digest.json") or {}

    m = mn.get("market") or {}
    P = []
    P.append("【盘面】日期 %s" % mn.get("date", datetime.now(CST).strftime("%Y%m%d")))
    idx = m.get("indices") or []
    if idx:
        P.append("指数: " + ", ".join(
            "%s%s(%+.2f%%)" % (i.get("name"), i.get("price"), i.get("chg_pct") or 0)
            for i in idx[:4]))
    P.append("涨跌停: 涨停%s 跌停%s 炸板%s 封板率%s%%" % (
        m.get("limit_up"), m.get("limit_down"), m.get("broken"), m.get("zt_rate")))
    P.append("涨跌家数: 涨%s 跌%s 上涨占比%s%% 成交%s亿" % (
        m.get("advance"), m.get("decline"), m.get("up_pct"),
        (m.get("turnover") or {}).get("total_yi")))

    # 十维 TOP5 (含海外面)
    top = (md.get("top") or [])[:5]
    if top:
        P.append("【十维评分TOP5】(技术/资金/高低位/基本/板块/市场/情绪/周期/消息/海外)")
        for r in top:
            d = r.get("dims") or {}
            P.append("  %s 综合%s | 技%s 资%s 位%s 基%s 板%s 市%s 情%s 周%s 消%s 海%s" % (
                r.get("name"), r.get("composite"),
                d.get("技术面"), d.get("资金面"), d.get("高低位"), d.get("基本面"),
                d.get("板块面"), d.get("市场面"), d.get("情绪面"), d.get("周期面"),
                d.get("消息面"), d.get("海外面")))
            ov = (r.get("why") or {}).get("海外面")
            if ov:
                P.append("     海外面依据: %s" % str(ov)[:90])

    # 决策引擎
    if dec:
        buys = [c for c in (dec.get("cards") or []) if c.get("action") == "buy"]
        P.append("【决策引擎】%s市场 / %s / 买入信号 %d 只%s" % (
            dec.get("market_regime"),
            (dec.get("position_advice") or {}).get("label"),
            len(buys),
            (": " + ", ".join("%s(%s)" % (c.get("name"), c.get("composite")) for c in buys[:5]))
            if buys else ""))
        if dec.get("reason"):
            P.append("  理由: %s" % str(dec.get("reason"))[:120])

    # 海外关联性 (corr/beta/滞后/隔夜)
    pairs = (ol.get("pairs") or [])
    if pairs:
        P.append("【海外关联性】大盘与全球市场的传导 (corr/beta/最优滞后/隔夜传导)")
        for r in pairs[:8]:
            if r.get("target_name") in ("沪深300", "上证指数", "创业板指"):
                P.append("  %s ← %s: corr %s beta %s 滞后%s 隔夜corr %s (%s)" % (
                    r.get("target_name"), r.get("driver_name"),
                    r.get("corr"), r.get("beta"), r.get("best_lag"),
                    r.get("overnight_corr"), r.get("level")))

    # 全球六大市场
    gs = (gm.get("global_sentiment") or {})
    if gs:
        P.append("【全球市场】情绪 %s (%s%%) — %s" % (
            gs.get("sentiment_label"), gs.get("avg_chg_pct"), gs.get("interpretation")))
        for mkt, lst in (gm.get("markets") or {}).items():
            seg = []
            for x in lst[:3]:
                if x.get("chg_pct") is not None:
                    seg.append("%s%+.2f%%" % (x.get("name"), x.get("chg_pct")))
            if seg:
                P.append("  %s: %s" % (mkt, "  ".join(seg)))

    # 板块资金
    smf = ((mn.get("sector_moneyflow") or {}).get("data")) or []
    if smf:
        inflow = [x for x in smf if (x.get("main_net_yi") or 0) > 0]
        outflow = [x for x in smf if (x.get("main_net_yi") or 0) < 0]
        outflow.sort(key=lambda x: x.get("main_net_yi") or 0)
        P.append("【板块资金】净流入 %d / %d 个板块" % (len(inflow), len(smf)))
        if inflow:
            P.append("  流入前列: " + ", ".join(
                "%s%+.2f亿" % (x.get("name"), x.get("main_net_yi")) for x in
                sorted(inflow, key=lambda x: -(x.get("main_net_yi") or 0))[:4]))
        if outflow:
            P.append("  流出前列: " + ", ".join(
                "%s%.2f亿" % (x.get("name"), x.get("main_net_yi")) for x in outflow[:4]))

    # 今日推股 (含 gate 与分级)
    if pm:
        gate = pm.get("gate") or {}
        P.append("【今日推股】闸门 %s (仓位上限 %s%%) — %s" % (
            gate.get("state"), round((gate.get("pos_cap") or 0) * 100), gate.get("reason")))
        P.append("  候选 %s 只; 全球情绪 %s (%s)" % (
            pm.get("n_candidates"),
            (pm.get("global") or {}).get("score"), (pm.get("global") or {}).get("mood")))
        for tk, lb in (("A_buy", "A级"), ("B_watch", "B级"), ("C_avoid", "C级")):
            arr = (pm.get("tiers") or {}).get(tk) or []
            if arr:
                P.append("  %s (%d): " % (lb, len(arr)) + "; ".join(
                    "%s 入场%s 止损%s 目标%s 评分%s" % (
                        r.get("name") or stock_name(r.get("code")),
                        r.get("entry_zone"), r.get("stop"),
                        r.get("target"), r.get("score")) for r in arr[:4]))

    # 昨日推股验证
    vd = (cr.get("verification") or cr.get("picks") or [])
    if vd:
        P.append("【昨日推股验证】")
        for r in vd[:8]:
            P.append("  %s %s → %s (昨收%s 今%s)" % (
                r.get("name"), r.get("verdict") or r.get("result"),
                r.get("pnl_pct") if r.get("pnl_pct") is not None else "",
                r.get("entry"), r.get("exit") or r.get("close")))

    # 历史胜率: reflect_latest.analysis.overall = {"1":{...}, "3":{...}}
    # 注意 horizons 只是持有天数列表 [1,3,5,10], 不是胜率数据, 别读错。
    ov = (rl.get("analysis") or {}).get("overall") or {}
    if ov:
        segs = []
        for k, v in list(ov.items())[:4]:
            if not isinstance(v, dict):
                continue
            ci = v.get("bull_win_rate_ci")
            segs.append("%s日 看多胜率%.1f%%%s 看空%.1f%% 均收益%+.2f%% (n=%s)" % (
                k, (v.get("bull_win_rate") or 0),
                (" CI[%.1f,%.1f]" % tuple(ci)) if ci else "",
                (v.get("bear_win_rate") or 0),
                (v.get("avg_ret_pct") or 0), v.get("n")))
        if segs:
            P.append("【历史胜率】" + "; ".join(segs))
    # 指标质量排名 (哪些技术信号真的有效)
    rk = (rl.get("analysis") or {}).get("ranked") or []
    if rk:
        best = sorted(rk, key=lambda x: -(x.get("quality") or 0))[:4]
        worst = sorted(rk, key=lambda z: (z.get("quality") or 0))[:3]
        P.append("【指标有效性】质量最高: " + "; ".join(
            "%s(%.1f, 看多胜率%.1f%%)" % (x.get("indicator"), x.get("quality"),
                                      x.get("bull_win_rate") or 0) for x in best))
        P.append("  质量最低: " + "; ".join(
            "%s(%.1f, 看多胜率%.1f%%)" % (x.get("indicator"), x.get("quality"),
                                      x.get("bull_win_rate") or 0) for x in worst))
    if sr.get("verdict") or sr.get("summary"):
        vs = sr.get("verdict") or {}
        if isinstance(vs, dict) and vs:
            P.append("【引擎自我反思】" + "; ".join(
                "%s: %s" % (k, str(vv)[:80]) for k, vv in list(vs.items())[:4]))
        else:
            P.append("【引擎自我反思】%s" % str(sr.get("summary"))[:200])

    # 消息传导 (坏消息利好谁 / 好消息利空谁)
    nt = load("news_transmission.json") or {}
    for r in (nt.get("transmissions") or [])[:6]:
        seg = []
        if r.get("inverse_bull"):
            seg.append("反向受益: " + ",".join(
                "%s(%d只)" % (x.get("sector"), len(x.get("stocks") or []))
                for x in r["inverse_bull"][:3]))
        if r.get("inverse_bear"):
            seg.append("反向受害: " + ",".join(
                "%s(%d只)" % (x.get("sector"), len(x.get("stocks") or []))
                for x in r["inverse_bear"][:3]))
        if r.get("hedge"):
            seg.append("避险去向: " + ",".join(
                x.get("sector") for x in r["hedge"][:3]))
        if seg:
            P.append("【消息传导】[%s] %s → %s" % (
                r.get("label"), (r.get("title") or "")[:40], " ; ".join(seg)))

    # 资讯
    news = []
    for cat, arr in (nd.get("categories") or {}).items():
        if isinstance(arr, list):
            news.extend(arr[:3])
    if news:
        P.append("【重要资讯】" + " | ".join(str(n.get("title", ""))[:36] for n in news[:6]))

    return "\n".join(P)


def ask_gitcode_ai(prompt, token, max_tokens=1600):
    """调 GitCode AI, 主模型失败自动切备用模型。返回 (模型名, 内容)。"""
    for model in MODELS:
        try:
            body = json.dumps({
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            }).encode()
            req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
                "Authorization": "Bearer %s" % token,
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0",
            })
            r = json.load(urllib.request.urlopen(req, timeout=180))
            content = ((r.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
            if content:
                return model, content
        except Exception as e:
            print("[ai_review] %s 失败: %s" % (model, str(e)[:120]), file=sys.stderr)
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-md", default=os.path.join(DATA_DIR, "ai_review.md"))
    ap.add_argument("--out-json", default=os.path.join(DATA_DIR, "ai_review.json"))
    ap.add_argument("--dry-run", action="store_true", help="只打印 prompt 不调用")
    ap.add_argument("--force", action="store_true",
                    help="忽略输入 hash 缓存, 强制重新调用 LLM")
    args = ap.parse_args()

    token = os.environ.get("GITCODE_TOKEN") or DEFAULT_TOKEN
    if not token and not args.dry_run:
        print("[ai_review] 未提供 GITCODE_TOKEN, 跳过 (不影响其他管线)")
        return 1

    ctx = build_context()
    prompt = "以下是今日A股算法与引擎产出, 请按系统要求写复盘:\n\n" + ctx

    if args.dry_run:
        print(prompt)
        return 0

    # ── Replay caching (架构借鉴 Lumiwealth/lumibot, GPL-3.0 仅学设计) ──
    #  lumibot 的 agentic backtesting 对每次 agent 调用按
    #  (prompt, context, model, tool surface, timestamp) 的 SHA-256 做缓存,
    #  重跑同一组合直接复用决策, 零 LLM 调用。GitCode AI 每月有额度限制,
    #  而我们的复盘输入在同一天内基本不变 —— 不缓存等于反复为同样的数据付费。
    #  这里按「输入内容 hash」缓存: 输入未变则直接复用上次结果。
    import hashlib
    ctx_hash = hashlib.sha256(
        (prompt + "|" + (MODELS[0] if MODELS else "")).encode("utf-8")).hexdigest()[:16]
    cache_path = os.path.join(DATA_DIR, "ai_review_cache.json")
    cached = None
    try:
        if os.path.exists(cache_path):
            cached = json.load(open(cache_path, encoding="utf-8"))
    except Exception:
        cached = None
    if (cached and cached.get("ctx_hash") == ctx_hash and cached.get("review")
            and not args.force):
        print(f"[ai_review] 输入未变 (hash={ctx_hash}), 复用缓存, 跳过 LLM 调用 "
              f"(缓存生成于 {cached.get('ts')})")
        out = dict(cached)
        out["from_cache"] = True
        out["cache_age_hours"] = round(
            (datetime.now(CST) - datetime.fromisoformat(cached["ts"])).total_seconds() / 3600, 1)
        for p in (args.out_json,):
            with open(p, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
        md_path = args.out_md
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("# AI 每日复盘 %s CST (复用缓存)\n\n%s\n\n---\n\n"
                    "*输入未变, 复用 %s 的结果; 加 --force 强制重新调用。*\n" % (
                        out["ts"][:16], out.get("review", ""), out["ts"][:16]))
        return 0

    print("[ai_review] 调用 GitCode AI (%s) ..." % MODELS[0], flush=True)
    model, content = ask_gitcode_ai(prompt, token)

    now = datetime.now(CST)
    out = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "gitcode_ai_review (GitCode AI 额度 + 算法/引擎产出)",
        "model": model,
        "ok": bool(content),
        "review": content or "(AI 调用失败, 保留规则引擎复盘)",
        "context_len": len(ctx),
        "meta": {
            "endpoint": ENDPOINT,
            "models_tried": MODELS,
            "inputs": ["market_now", "multidim(十维)", "decision", "reflect_latest",
                       "self_reflect", "overseas_linkage", "global_markets",
                       "sector_moneyflow", "premarket_picks", "close_review",
                       "news_transmission", "news_digest"],
            "note": "消耗 GitCode 每月 AI 额度; 失败时不影响其他管线; "
                   "输入未变时按 hash 复用缓存 (replay caching, 借鉴 lumibot 设计)",
            "ctx_hash": ctx_hash,
            "caveat": "AI 生成内容仅供研究参考, 不构成投资建议",
        },
    }
    for p in (args.out_md, args.out_json):
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    with open(args.out_json, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    md = ("# AI 每日复盘 %s CST\n\n模型: `%s` (GitCode AI)\n\n%s\n\n---\n\n"
          "<details><summary>算法/引擎数据上下文</summary>\n\n```\n%s\n```\n\n</details>\n\n"
          "*AI 生成, 仅供研究参考, 不构成投资建议。*\n" % (
              now.strftime("%Y-%m-%d %H:%M"), model,
              content or "(调用失败)", ctx))
    with open(args.out_md, "w", encoding="utf-8") as f:
        f.write(md)
    # 写入 replay cache (输入 hash → 结果), 供下次输入未变时复用
    if content:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
            print(f"[ai_review] 已写缓存 (hash={ctx_hash})")
        except Exception as e:
            print(f"[ai_review] 缓存写入失败 (不影响本次产出): {e}", file=sys.stderr)
    print("[ai_review] 模型=%s 长度=%d 字" % (model, len(content or "")))
    print("[ai_review] → %s / %s" % (args.out_md, args.out_json))
    return 0 if content else 1


if __name__ == "__main__":
    sys.exit(main())
