#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/decision.py ═══
# 上游 commit: 391e0699d872 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""decision.py — 交易决策引擎 (2026-10-13, 数据库驱动, 不依赖网站)
输入: ai_picks.json + global/latest.json + cycle/stats.json + picks.duckdb(胜率闭环)
输出: data/decision.json — 仓位建议/买卖清单/风控线/执行检查单
原则: 防守优先(弱市空仓), T+1纪律(14:45唯一决策点), 数据库回测证据说话
"""
import json, duckdb, datetime, os

BASE = "/home/agentuser/stockdb"
OUT = f"{BASE}/data/decision.json"

def load(name, path):
    try: return json.load(open(path))
    except Exception: return {}

def main():
    ai = load("ai", f"{BASE}/data/ai_picks.json")
    gl = load("gl", f"{BASE}/data/global/latest.json")
    cyc = load("cyc", f"{BASE}/data/cycle/stats.json")
    gscore = (gl.get("metrics") or {}).get("global_score", 0)
    mood = (gl.get("metrics") or {}).get("global_mood", "中性")
    # 胜率闭环: picks.duckdb最近30笔纪律口径胜率 → 仓位系数
    win_rate, n_recent, avg_ret = 0.45, 0, 0.0
    try:
        con = duckdb.connect(f"{BASE}/picks.duckdb", read_only=True)
        r = con.execute("""SELECT COUNT(*), AVG(CASE WHEN verdict LIKE '止盈%' THEN 1.0 ELSE 0.0 END), AVG(ret_oc*100)
                           FROM (SELECT verdict, ret_open_close AS ret_oc FROM pick_log WHERE settled=true ORDER BY ts DESC LIMIT 30)""").fetchone()
        if r and r[0]:
            n_recent, win_rate, avg_ret = r[0], r[1] or 0.45, r[2] or 0.0
        con.close()
    except Exception:
        pass
    # 凯利仓位(独特算法): f* = W - (1-W)/R, 半凯利执行, 上限60%
    try:
        con2 = duckdb.connect(f"{BASE}/picks.duckdb", read_only=True)
        _n, _w, _aw, _al = con2.execute("""SELECT COUNT(*),
            AVG(CASE WHEN verdict LIKE '止盈%' THEN 1.0 ELSE 0.0 END),
            AVG(CASE WHEN verdict LIKE '止盈%' THEN ABS(ret_open_close) END),
            AVG(CASE WHEN verdict LIKE '止损%' THEN ABS(ret_open_close) END)
            FROM pick_log WHERE settled=true""").fetchone()
        con2.close()
        W = _w if _w else 0.45
        R = (_aw or 0.02) / (_al or 0.02) if _al else 2.0
        kelly = max(0.0, min(0.6, (W - (1 - W) / R) * 0.5))
        kelly_info = {"W": round(W, 3), "R": round(R, 2), "half_kelly": round(kelly, 3), "n": _n}
    except Exception:
        kelly, kelly_info = 0.25, {}
    # 仓位决策树 (回测证据: 弱市选票救不了→空仓; ADAPT: 胜率<40%降半仓)
    capital = 25000
    pos = 0.0; action = ""; reasons = []
    if gscore < -15:
        pos, action = 0.0, "空仓"
        reasons.append(f"全球偏空({gscore}) — 弱市巨量票9/12止损实证, 空仓是最优策略")
    elif gscore > 15:
        pos, action = min(0.6, max(kelly, 0.3)), "正常仓(凯利)"
        reasons.append(f"全球偏多({gscore}) — 可进攻, 温和量票为主")
        if win_rate < 0.40 and n_recent >= 10:
            pos, action = 0.3, "半仓(胜率不足)"
            reasons.append(f"近30笔纪律胜率{win_rate:.0%}<40% — ADAPT降半仓")
    else:
        pos, action = min(0.25, kelly) if kelly > 0 else 0.0, "轻仓试探(凯利)"
        reasons.append(f"全球中性({gscore}) — 轻仓, 温和量+低位双过滤")
        if win_rate < 0.40 and n_recent >= 10:
            pos, action = 0.0, "空仓(胜率不足)"
            reasons.append(f"近30笔胜率{win_rate:.0%}<40% — 中性市也空仓等强信号")
    # 引擎融合复核
    try:
        ef = json.load(open(f"{BASE}/data/engine_fusion.json"))
        reasons.append(f"引擎融合: E={ef['E']} {ef['state']} (一致性{int(ef['agreement']*100)}%)")
        if "防守" in ef["state"] and pos > 0.2:
            pos = 0.2
        if "中性观望" in ef["state"] and pos > 0.25:
            pos = 0.25
    except Exception:
        pass
    # 亏损归因注入
    try:
        la = json.load(open(f"{BASE}/data/loss_attr.json"))
        if la.get("total_losses", 0) >= 5:
            reasons.append(f"亏损归因({la['total_losses']}笔): {la['advice']}")
            if "空仓优先" in la["advice"] and pos > 0.2:
                pos = 0.2
    except Exception:
        pass
    cycle_bias = cyc.get("bias", "?") if isinstance(cyc, dict) else "?"
    if cycle_bias == "偏空" and pos > 0.3:
        pos = 0.3; reasons.append("周期v2偏空 — 仓位压至30%上限")
    # B规则策略卡信号(5日口径55.5%胜率) — 信号独立于仓位闸门: 闸门开才给买入清单
    sc = load("sc", f"{BASE}/data/strategy_card.json")
    bs = load("bs", f"{BASE}/data/bcard_stats.json")
    bcands = sc.get("candidates", [])
    if bcands:
        reasons.append(f"B规则候选{len(bcands)}票(5日口径胜率55.5%): " + ",".join(c["symbol"] for c in bcands[:5]))
    if bs.get("bcard_stats"):
        reasons.append("B规则实盘对账: " + "; ".join(f"{s['verdict']}×{s['n']}均{s['avg_ret5']}%" for s in bs["bcard_stats"]))
    # 买卖清单
    picks = ai.get("picks") or []
    observe = ai.get("observe_only", False)
    buy_list = [] if (observe or pos == 0) else [
        {"symbol": p["symbol"], "name": p.get("name",""), "price": p["close"],
         "budget": round(capital * pos / min(5, len(picks[:5])), 0), "stop": round(p["close"]*0.98, 2),
         "tp": round(p["close"]*1.02, 2)} for p in picks[:5]]
    out = {
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        "decision": {"action": action, "position_pct": pos, "capital": capital,
                     "budget": round(capital*pos), "cycle": cycle_bias,
                     "global": {"score": gscore, "mood": mood}, "kelly": kelly_info,
                     "win_rate_recent": round(win_rate, 3), "n_recent": n_recent, "avg_ret_recent": round(avg_ret, 2)},
        "reasons": reasons,
        "buy_list": buy_list,
        "rules": ["T+1: 次日14:45唯一决策点", "冲高+2%止盈", "破-2%止损",
                  "弱市(全球<-15)空仓 — 回测: 弱市选票次日均-0.8%",
                  "纪律胜率<40%自动降仓 — ADAPT自适应"],
        "note": "不构成投资建议",
    }
    json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1)
    print(f"决策: {action} 仓位{pos:.0%} 预算{capital*pos:.0f} | 近{n_recent}笔胜率{win_rate:.0%}")
    for r_ in reasons: print(" ·", r_)
    print(f"买入清单: {len(buy_list)}笔" if buy_list else "买入清单: 无(空仓/观察)")

if __name__ == "__main__":
    main()
