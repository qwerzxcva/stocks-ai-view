#!/usr/bin/env python3
"""
premarket_picks.py — 早盘推股引擎 (开盘前 09:15 前产出)

用户核心诉求: 「早盘、午盘开盘前推股给我, 看收盘和明天开盘能赚多少」。
本引擎专注「早盘」这一枪: 在 09:15 集合竞价前给出可执行清单。

与已有引擎的分工:
  ai_pick_v4 / engine_v5 / multidim  → 昨收后的全市场扫描 (夜间跑)
  entry_planner                      → 把 ranked picks 转成入场价/止损/目标
  build_today_picks                  → 汇总展示 (今日 picks)
  本引擎 premarket_picks             → 【开盘前最后一步】:
    1. 读上述所有产出
    2. 叠加「隔夜增量信息」: 全球市场 (美股收盘/日韩/欧股)、A50 期货、
       离岸人民币、VIX、财联社早间电报、Newsnow 隔夜热点
    3. 应用市况闸门 (全球偏空 → 只观察不买入)
    4. 给出分级清单: A 级(可开盘买入) / B 级(观察等回踩) / C 级(回避)
    5. 每只带: 建议入场区间、止损、目标位、预期收益、核心逻辑、风险点
    6. 输出明日/后日展望 (用户要求「看到明天和后天的表现」)

输出 data/premarket_picks.json
"""
import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone, timedelta

from decision_contract import decision_quotes, normalize_code
from horizon_features import build_features
from paths import BULK

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")


def load(name, default=None):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return default
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default


def load_global():
    for rel in (("global", "latest.json"), ("global.json",)):
        p = os.path.join(DATA_DIR, *rel)
        if os.path.exists(p):
            try:
                return json.load(open(p, encoding="utf-8"))
            except Exception:
                pass
    return None


def buyability(code, quote, latest):
    """Do not call stale or already-limit-up data an executable buy."""
    digits = str(code).replace("sh", "").replace("sz", "").replace("bj", "")
    for row in (latest.get("limit_up_pool") or []):
        if isinstance(row, dict):
            row_code = str(row.get("code", "")).replace("sh", "").replace("sz", "").replace("bj", "")
            if row_code == digits:
                return False, "上一快照已在涨停池；开盘前不列为可买，需竞价/盘口复核"
    if quote is None or quote.status != "ok":
        return None, "行情快照缺失或过期，不能确认开盘可成交"
    if quote.session not in ("closed", "preopen"):
        return None, "当前不在收盘/盘前复核状态，需竞价复核"
    return True, "收盘/盘前行情快照有效，未标记涨停；下单前仍需盘口复核"


def horizon_outlook():
    """明日/后日展望 — 基于历史同星期/月相统计 + 当前市场相位。"""
    out = {}
    ph = load("market_phase.json") or {}
    cd = load("cycle_deep.json") or {}
    wf = load("walkforward.json") or {}
    rl = load("reflect_latest.json") or {}

    # 市场相位
    out["phase"] = {
        "stage": ph.get("stage") or ph.get("phase"),
        "score": ph.get("score"),
        "desc": ph.get("desc") or ph.get("note"),
    }
    # 周期深析 (上证历史同期)
    if cd:
        s = cd.get("summary") or cd
        out["cycle"] = {
            "index": s.get("index"), "date": s.get("date"),
            "bias": s.get("bias"), "composite": s.get("composite"),
            "n_days": s.get("n_days") or (cd.get("meta") or {}).get("n_days"),
        }
    # walkforward 泛化能力
    if wf:
        out["walkforward"] = {
            "is": (wf.get("is") or {}).get("win_rate"),
            "oos": (wf.get("oos") or {}).get("win_rate"),
            "decay_pct": wf.get("decay_pct"),
            "verdict": wf.get("verdict"),
        }
    # 1 日胜率 (最近对账)
    if rl:
        an = rl.get("analysis") or {}
        ov = an.get("overall") or {}
        out["recent_winrate"] = {
            "h1_bull": ov.get("bull_win_rate"), "h1_bear": ov.get("bear_win_rate"),
            "n": rl.get("n_records"),
        }
    return out


def market_gate(gm):
    """市况闸门: 返回 (gate, reason, pos_cap)。

    闸门只决定仓位上限与风险标注, **不阻断出票**。弱市同样给票,
    否则用户在没有信号的市道里完全拿不到候选; 空仓建议写进 reason,
    由用户自己决定执不执行。
    """
    if not gm:
        return "unknown", "无全球面板数据, 按中性处理", 0.5
    m = gm.get("metrics") or {}
    s = m.get("global_score", 0)
    vix = m.get("vix", 0) or 0
    if s < -15:
        return ("defensive",
                "全球情绪 %.1f (偏空) — 建议空仓/轻仓; 仍给出候选, "
                "单票试错、严设止损" % s, 0.1)
    if vix > 25:
        return ("defensive",
                "VIX %.1f 极端恐慌 — 建议观察为主; 仍给出候选, "
                "仅小幅试错" % vix, 0.1)
    if s > 15:
        return "open", "全球情绪 %.1f (偏多) — 可正常进攻" % s, 1.0
    return "half", "全球情绪 %.1f (中性) — 半仓纪律" % s, 0.5


def collect_candidates():
    """从各引擎产出汇总候选股 (带去重 + 来源标注)。"""
    cand = {}
    def add(code, name, price, score, src, extra=None):
        if not code:
            return
        # 统一 code 为 6 位裸代码 (flow_divergence 带 sh/sz 前缀,
        # 其他引擎不带, 不去前缀会导致同一只股票被算作两个候选)。
        # 保留交易所信息: sh/sz 前缀决定估值表取值 (000937 在 sh 是指数
        # 「800公用」, 在 sz 是「冀中能源」, 取错就是 570 倍价差)。
        raw = str(code)
        mkt = raw[:2] if raw[:2] in ("sh", "sz", "bj") else ""
        bare = raw[-6:]
        if not bare.isdigit():
            return
        c = cand.setdefault(bare, {"code": bare, "name": name or "", "price": price,
                                   "mkt": mkt, "sources": [], "scores": {}, "extra": {}})
        if mkt and not c["mkt"]:
            c["mkt"] = mkt
        if name and not c["name"]:
            c["name"] = name
        if price and not c["price"]:
            c["price"] = price
        if src not in c["sources"]:
            c["sources"].append(src)
        if score is not None:
            c["scores"][src] = score
        if extra:
            c["extra"].update({k: v for k, v in extra.items() if v is not None})

    # 1. engine_v5 (共振打分)
    v5 = load("engine_v5.json") or {}
    for r in (v5.get("top") or [])[:100]:
        add(r.get("code"), r.get("name"), r.get("price"), r.get("score"), "engine_v5",
            {"resonance": r.get("resonance"), "chg_pct": r.get("chg_pct")})

    # 2. ai_pick_v4 (AI 推荐)
    v4 = load("ai_pick_v4.json") or {}
    for r in (v4.get("picks") or [])[:50]:
        add(r.get("code"), r.get("name"), r.get("price"), r.get("score"), "ai_pick_v4",
            {"intrinsic_value": r.get("intrinsic_value"), "value_dev_pct": r.get("value_dev_pct"),
             "value_tag": r.get("value_tag"), "pos240_pct": r.get("pos240_pct"),
             "ret_pct": r.get("ret_pct"), "vratio": r.get("vratio")})

    # 3. multidim (9 维因子) — dims/why 一起带走, 供前端做多维分析展开
    md = load("multidim.json") or {}
    for r in (md.get("top") or [])[:100]:
        add(r.get("code"), r.get("name"), r.get("price"), r.get("composite"), "multidim",
            {"composite": r.get("composite"), "resonance": r.get("resonance"),
             "intrinsic_value": r.get("intrinsic_value"), "value_dev_pct": r.get("value_dev_pct"),
             "value_tag": r.get("value_tag"), "chg_pct": r.get("chg_pct"),
             "dims": r.get("dims"), "why": r.get("why")})

    # 4. lowpool (低位启动)
    lp = load("lowpool.json") or {}
    for r in (lp.get("pool") or lp.get("items") or [])[:50]:
        add(r.get("code"), r.get("name"), r.get("price"), r.get("score"), "lowpool",
            {"pos240_pct": r.get("pos240_pct") or r.get("pos")})

    # 5. flow_divergence (资金背离)
    fd = load("flow_divergence.json") or {}
    for side in ("buy_side", "sell_side"):
        for r in (fd.get(side) or [])[:20]:
            add(r.get("code"), r.get("name"), r.get("price"), r.get("score"),
                "flow_" + ("div_buy" if side == "buy_side" else "div_sell"),
                {"signal": r.get("signal")})

    # 6. ban_pick (打板)
    bp = load("ban_pick.json") or {}
    for r in (bp.get("picks") or [])[:30]:
        add(r.get("code"), r.get("name"), r.get("price"), r.get("score"), "ban_pick",
            {"verdict": r.get("verdict")})

    # 7. scenario (场景推股)
    sc = load("scenario.json") or {}
    for r in (sc.get("picks") or sc.get("stocks") or [])[:30]:
        add(r.get("code"), r.get("name"), r.get("price"), r.get("score"), "scenario")

    return cand


def score_candidate(c):
    """综合评分: 多引擎共振 + 估值安全垫 + 位置 + 资金。"""
    sc = c["scores"]
    ex = c["extra"]
    parts = {}

    # 多引擎共振 (3+ 个源提名 = 强信号)
    n_src = len(c["sources"])
    parts["resonance"] = min(n_src * 12, 36)

    # 引擎分归一 (各引擎满分不同, 取 0-100 后平均)
    vals = [v for v in sc.values() if isinstance(v, (int, float)) and 0 < v <= 100]
    parts["engine_avg"] = (sum(vals) / len(vals) * 0.3) if vals else 0

    # 估值安全垫 (低估加分, 严重高估扣分)
    dev = ex.get("value_dev_pct")
    if dev is not None:
        parts["valuation"] = max(-15, min(15, -dev * 0.3))
    else:
        parts["valuation"] = 0

    # 位置 (20 日位越低越有空间, 但太低说明弱)
    pos = ex.get("pos240_pct")
    if pos is not None:
        parts["position"] = 15 - abs(pos - 55) * 0.25
    else:
        parts["position"] = 0

    # 资金背离加成
    if "flow_div_buy" in c["sources"]:
        parts["flow"] = 12
    elif "flow_div_sell" in c["sources"]:
        parts["flow"] = -10
    else:
        parts["flow"] = 0

    total = sum(parts.values())
    return round(total, 1), parts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "premarket_picks.json"))
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = datetime.now(CST)
    if now.weekday() >= 5 or (now.hour, now.minute) >= (9, 15):
        print("[premarket] outside pre-open trading session; preserving existing snapshot")
        return 0
    gm = load_global()
    gate, gate_reason, pos_cap = market_gate(gm)

    cand = collect_candidates()
    print("[premarket] 候选汇总: %d 只 (来自 %d 个引擎)" % (
        len(cand), len({s for c in cand.values() for s in c["sources"]})))

    ranked = []
    # 价格兜底: flow_divergence 等源不带 price, 从 valuation.table 补
    # 注意用 mkt 前缀精确取 (000937 在 sh 是指数, 在 sz 是冀中能源)
    vt = (load("valuation.json") or {}).get("table") or {}
    for code, c in cand.items():
        if not c["price"]:
            keys = ([c["mkt"] + code] if c["mkt"] else
                    ["sh" + code, "sz" + code, "bj" + code])
            for k in keys:
                v = vt.get(k)
                if v and v.get("price"):
                    c["price"] = v["price"]
                    break
    for code, c in cand.items():
        total, parts = score_candidate(c)
        ex = c["extra"]
        price = c.get("price")
        # 入场区间: 基于昨收与波动 (无实时价时用昨收)
        entry_lo = round(price * 0.985, 2) if price else None
        entry_hi = round(price * 1.005, 2) if price else None
        stop = round(price * 0.955, 2) if price else None      # -4.5%
        target = round(price * 1.06, 2) if price else None     # +6%
        reasons = []
        n_src = len(c["sources"])
        if n_src >= 2:
            reasons.append("%d 个引擎共振提名 (%s)" % (n_src, "/".join(c["sources"])))
        if ex.get("value_dev_pct") is not None and ex["value_dev_pct"] <= -10:
            reasons.append("低估 %.1f%%, 有安全垫" % ex["value_dev_pct"])
        if ex.get("value_dev_pct") is not None and ex["value_dev_pct"] >= 30:
            reasons.append("⚠️ 高估 %.1f%%" % ex["value_dev_pct"])
        if "flow_div_buy" in c["sources"]:
            reasons.append("资金吸筹背离")
        if ex.get("pos240_pct") is not None:
            reasons.append("20日位 %.0f%%" % ex["pos240_pct"])
        if ex.get("verdict"):
            reasons.append("打板判断: %s" % ex["verdict"])

        ranked.append({
            "code": code, "name": c["name"], "price": price,
            "score": total, "parts": parts,
            "sources": c["sources"],
            "entry_zone": [entry_lo, entry_hi], "stop": stop, "target": target,
            "risk_reward": round((target - entry_hi) / (entry_hi - stop), 2)
            if price and entry_hi is not None and stop is not None and entry_hi > stop else None,
            "intrinsic_value": ex.get("intrinsic_value"),
            "value_dev_pct": ex.get("value_dev_pct"),
            "value_tag": ex.get("value_tag"),
            "pos240_pct": ex.get("pos240_pct"),
            # 多维分析: 技术/资金/高低位/基本/板块/市场/情绪/周期/消息 九维
            "dims": ex.get("dims"),
            "dims_why": ex.get("why"),
            "reasons": reasons,
        })

    ranked.sort(key=lambda x: -x["score"])

    selected = ranked[:max(args.top * 5, 60)]
    quote_map = decision_quotes([r["code"] for r in selected], now=now,
                                max_age_seconds=18 * 3600)
    history = build_features(BULK, [r["code"] for r in selected])
    stock_names = load("stock_names.json") or {}
    for r in ranked:
        digits, exchange = normalize_code(r["code"])
        symbol = exchange + digits
        quote = quote_map.get(symbol)
        feature = history["features"].get(symbol) or {}
        r["decision_quote"] = asdict(quote) if quote else None
        r["engine_price"] = r.get("price")
        if quote and quote.price and quote.status == "ok" and quote.session in ("closed", "preopen"):
            r["price"] = quote.price
            r["name"] = r["name"] or stock_names.get(digits) or ""
            r["entry_zone"] = [round(quote.price * 0.985, 2), round(quote.price * 1.005, 2)]
            r["stop"] = round(quote.price * 0.955, 2)
            r["target"] = round(quote.price * 1.06, 2)
            upper = r["entry_zone"][1]
            r["risk_reward"] = round((r["target"] - upper) / (upper - r["stop"]), 2)
        r["history"] = {key: feature.get(key) for key in
                         ("ma20", "ma60", "ma120", "ma252", "length", "last_date", "conflicting_rows")}
        r["buyable"], r["buyability_reason"] = buyability(r["code"], quote, load("latest.json") or {})
        missing = []
        if len(r["sources"]) < 2:
            missing.append("independent_engine_count")
        if not quote or quote.status != "ok" or quote.session not in ("closed", "preopen"):
            missing.append("quote_snapshot")
        if r["buyable"] is not True:
            missing.append("not_buyable_now")
        if not feature.get("coverage", {}).get("ma20"):
            missing.append("history_20d")
        if feature.get("conflicting_rows"):
            missing.append("conflicting_kline")
        if r.get("risk_reward") is None or r["risk_reward"] < 1.5:
            missing.append("risk_reward")
        if r.get("name") in (None, "", r["code"]):
            missing.append("stock_name")
        if r.get("engine_price") and quote and quote.price and abs(r["engine_price"] / quote.price - 1) > 0.015:
            missing.append("snapshot_price_deviation")
        r["missing_evidence"] = missing
        r["data_quality"] = "insufficient" if missing else "verified"

    # defensive 市况: 不降级, 但在每只票上标注「弱市持仓」,
    # 让用户看到票的同时知道这是逆市单, 该压仓位、严止损。
    if gate == "defensive":
        for r in ranked:
            r["weak_market"] = True
            r["reasons"].insert(0, "⚠️ 弱市逆势标的 — 建议轻仓试错, 止损 "
                                 + (("%.2f" % r["stop"]) if r["stop"] else "严设"))

    def tier(r, idx):
        if r.get("buyable") is False:
            return "C"
        if r.get("buyable") is None:
            return "B"
        if "flow_div_sell" in r["sources"]:
            return "C"
        if r["data_quality"] != "verified":
            return "C"
        if r["score"] >= 60 and gate != "defensive":
            return "A"
        if r["score"] >= 40:
            return "B"
        return "C"

    for i, r in enumerate(ranked):
        r["tier"] = tier(r, i)

    a_list = [r for r in ranked if r["tier"] == "A"][:args.top]
    b_list = [r for r in ranked if r["tier"] == "B"][:args.top]
    c_list = [r for r in ranked if r["tier"] == "C"][:args.top]

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "premarket_picks v1 (多引擎共振 + 隔夜增量 + 市况闸门)",
        "date": now.strftime("%Y-%m-%d"),
        "session": "premarket",
        "gate": {"state": gate, "reason": gate_reason, "pos_cap": pos_cap},
        "global": {
            "score": (gm or {}).get("metrics", {}).get("global_score"),
            "mood": (gm or {}).get("metrics", {}).get("global_mood"),
            "vix": (gm or {}).get("metrics", {}).get("vix"),
            "signals": (gm or {}).get("signals"),
        },
        "n_candidates": len(cand),
        "tiers": {
            "A_buy": a_list,
            "B_watch": b_list,
            "C_avoid": c_list,
        },
        "outlook": {
            "tomorrow": horizon_outlook(),
            "day_after": {
                "note": "后日展望由次日盘后的 reflect/winrate 闭环修正, 此处给基准预期",
                "basis": "当前市场相位 + walkforward OOS 胜率",
            },
        },
        "meta": {
            "sources_used": sorted({s for c in cand.values() for s in c["sources"]}),
            "scoring": "共振36 + 引擎均分30 + 估值±15 + 位置15 + 资金±12",
            "entry_rule": "入场区间=昨收-1.5%至+0.5%；止损-4.5%；目标+6%；盈亏比=(目标-入场上沿)/(入场上沿-止损)",
            "caveat": "Research reference only, not investment advice. "
                      "历史胜率不代表未来; 空仓也是一种决策。",
        },
    }

    if args.dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print("[premarket] 闸门: %s — %s" % (gate, gate_reason))
    print("[premarket] A 级(可买) %d / B 级(观察) %d / C 级(回避) %d" % (
        len(a_list), len(b_list), len(c_list)))
    print("[premarket] TOP5:")
    for r in (a_list or ranked)[:5]:
        print("  [%s] %s %s score=%.1f 入场%s 止损%s 目标%s" % (
            r["tier"], r["code"], r["name"], r["score"],
            r["entry_zone"], r["stop"], r["target"]))
        for x in r["reasons"][:2]:
            print("       - %s" % x)
    print("[premarket] -> %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
