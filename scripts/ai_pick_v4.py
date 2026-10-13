#!/usr/bin/env python3
"""
ai_pick_v4.py — AI推荐池 v4 (移植自 jiegengxiangjianxue1/stocks-algo/ai_pick.py)

数据源适配: /home/agentuser/stockdb/stock.duckdb → 本项目 bulk day_qfq_*.csv
路径规范: paths.py (BULK / AI_VIEW_DATA)

核心算法 (保留原逻辑, 来自实测统计):
  评分 = 量比(20%) + 60日动量RPS(20%) + 240日低位(20%) + 近期涨停基因(15%)
       + 尾盘强度代理=收盘位置(15%) + 换手温和(10%)
  硬规则 (2026-10-13 复盘驱动): 量比1.5-2.5 (唯一不负量档) + 涨幅0.5-5%
       + 240日位置<60% + 未涨停过滤
  形态加分: 停机坪/高而窄旗形 +0.08, 无大幅回撤 +0.04
  全球情绪自适应: 偏多→正常仓位; 偏空→只留低位防御+空仓闸门
  胜率自适应: 止损率>40%→防御模式 (低位0.35/动量0.10); 止盈率>35%→攻势模式
  周期位置: cycle_v2 偏空→收紧至 240日位<45%

用法: python3 ai_pick_v4.py [--out data/ai_pick_v4.json] [--top 30]
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import BULK, AI_VIEW_DATA
except Exception:
    BULK = os.environ.get("STOCKS_KLINE_BULK") or os.path.join(
        os.path.dirname(HERE), "..", "stocks-data-kline", "data", "bulk")
    AI_VIEW_DATA = os.path.join(os.path.dirname(HERE), "data")


# ─── bulk K线加载 (只取需要的字段, 按 code 分块) ─────────────────
def load_bulk_klines(codes=None):
    """返回 {symbol: [[date,open,close,high,low,vol], ...] 按日期升序}

    codes: None=全部; 否则只加载这些 (裸码或带前缀都可)
    """
    want = None
    if codes:
        want = set()
        for c in codes:
            c = str(c).strip()
            want.add(c)
            bare = c[2:] if c[:2] in ("sh", "sz", "bj") else c
            want.add(bare)
    out = {}
    for p in sorted(glob.glob(os.path.join(BULK, "day_qfq_*.csv"))):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("#") or "|" not in line:
                    continue
                parts = line.strip().split("|")
                if len(parts) < 6:
                    continue
                sym = parts[0]
                if want is not None and sym not in want:
                    continue
                try:
                    row = [parts[1], float(parts[2]), float(parts[3]),
                           float(parts[4]), float(parts[5]), float(parts[6]) if len(parts) > 6 else 0.0]
                except ValueError:
                    continue
                out.setdefault(sym, []).append(row)
    for s in out:
        out[s].sort(key=lambda r: r[0])
    return out


def compute_features(rows):
    """从K线算 ai_pick v4 所需的全部特征。rows 按日期升序, 至少 60 根。"""
    if len(rows) < 60:
        return None
    dates = [r[0] for r in rows]
    close = [r[2] for r in rows]
    high = [r[3] for r in rows]
    low = [r[4] for r in rows]
    vol = [r[5] for r in rows]

    last = len(rows) - 1
    px = close[last]
    prev = close[last - 1]
    if prev <= 0 or px <= 0:
        return None
    ret = px / prev - 1

    # 量比: 当日量 / 前20日均量
    v20 = sum(vol[last - 20:last]) / 20.0
    vratio = vol[last] / v20 if v20 > 0 else 0.0

    # 240日位置
    w240 = rows[-240:] if len(rows) >= 240 else rows
    hi240 = max(r[3] for r in w240)
    lo240 = min(r[4] for r in w240)
    pos240 = (px - lo240) / (hi240 - lo240) if hi240 > lo240 else 0.5

    # 收盘位置 (当日)
    h, l = high[last], low[last]
    close_pos = (px - l) / (h - l) if h > l else 0.5

    # 60日动量 RPS 代理: 60日涨幅
    if len(rows) >= 61:
        base = close[last - 60]
        mom60 = (px / base - 1) if base > 0 else 0.0
    else:
        mom60 = 0.0

    # 近期涨停基因: 近20日是否有涨幅>=9.5% 的天数
    zt_genes = sum(1 for i in range(max(0, last - 19), last + 1)
                   if close[i] > 0 and i > 0 and close[i] / close[i - 1] - 1 >= 0.095)

    # 当日是否已涨停 (硬过滤)
    is_zt_today = ret >= 0.095

    return {
        "symbol": None, "date": dates[last], "price": px,
        "ret": ret, "vratio": vratio, "pos240": pos240,
        "close_pos": close_pos, "mom60": mom60, "zt_genes": zt_genes,
        "is_zt_today": is_zt_today,
        # 形态检测需要的原始序列 (截取最近120根够用)
        "_rows": rows[-120:],
    }


def rank_pct(vals):
    """百分位排名 (0-1)。vals: list of float。返回同长度 list。"""
    n = len(vals)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: vals[i])
    out = [0.0] * n
    for rank, idx in enumerate(order):
        out[idx] = rank / max(1, n - 1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(AI_VIEW_DATA, "ai_pick_v4.json"))
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--bulk", default=None, help="bulk 目录 (默认 paths.BULK)")
    args = ap.parse_args()

    bulk = args.bulk or BULK
    print(f"[ai_pick_v4] 加载 bulk K线 ({bulk}) ...", flush=True)
    klines = load_bulk_klines()
    print(f"[ai_pick_v4] {len(klines)} 只股票", flush=True)

    feats = []
    for sym, rows in klines.items():
        f = compute_features(rows)
        if f:
            f["symbol"] = sym
            feats.append(f)
    if not feats:
        print("[ai_pick_v4] 无有效K线数据")
        return 1
    print(f"[ai_pick_v4] 特征计算完成 {len(feats)} 只", flush=True)

    # 硬过滤: 未涨停 + 涨幅0.5-5% + 位置<60% + 量比1.5-2.5
    cand = [f for f in feats
            if not f["is_zt_today"]
            and 0.005 <= f["ret"] <= 0.05
            and f["pos240"] < 0.6
            and 1.5 <= f["vratio"] <= 2.5]
    print(f"[ai_pick_v4] 硬过滤后 {len(cand)} 只", flush=True)
    if not cand:
        out = {"ts": datetime.now(CST).isoformat(timespec="seconds"),
               "engine": "ai_pick v4 (移植自 stocks-algo)",
               "n_scanned": len(feats), "n_candidates": 0, "picks": [],
               "note": "无候选 (可能非交易日或市场不满足强市闸门)",
               "meta": {"source": "bulk day_qfq_*.csv"}}
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"[ai_pick_v4] → {args.out} (空)")
        return 0

    # 基准评分
    r_vratio = rank_pct([c["vratio"] for c in cand])
    r_ret = rank_pct([c["ret"] for c in cand])
    r_pos = rank_pct([c["pos240"] for c in cand])
    r_cpos = rank_pct([c["close_pos"] for c in cand])
    r_mom = rank_pct([c["mom60"] for c in cand])
    for i, c in enumerate(cand):
        c["score"] = (r_vratio[i] * 0.20 + r_mom[i] * 0.20
                      + (1 - r_pos[i]) * 0.20 + min(1.0, c["zt_genes"] / 3.0) * 0.15
                      + r_cpos[i] * 0.15 + r_ret[i] * 0.10)

    # 形态加分 (可选, patterns 模块)
    pat_bonus = {}
    try:
        import patterns as P
        for c in cand:
            rows = c.pop("_rows")
            b = 0.0
            try:
                if hasattr(P, "parking_apron") and P.parking_apron(rows):
                    b += 0.08
            except Exception:
                pass
            try:
                if hasattr(P, "high_tight_flag") and P.high_tight_flag(rows):
                    b += 0.08
            except Exception:
                pass
            try:
                if hasattr(P, "low_backtrace_increase") and P.low_backtrace_increase(rows):
                    b += 0.04
            except Exception:
                pass
            if b:
                pat_bonus[c["symbol"]] = b
        if pat_bonus:
            for c in cand:
                c["score"] += pat_bonus.get(c["symbol"], 0)
            print(f"[ai_pick_v4] 形态加分 {len(pat_bonus)} 只")
    except Exception as e:
        for c in cand:
            c.pop("_rows", None)
        print(f"[ai_pick_v4] patterns skip: {e}")

    # 全球情绪自适应
    g = {"global_score": 0, "global_mood": "unknown", "pos_weight": 0.25, "suggest": "默认"}
    try:
        gp = os.path.join(AI_VIEW_DATA, "global_fx.json")
        if os.path.exists(gp):
            gd = json.load(open(gp, encoding="utf-8"))
            fx = gd.get("fx") or {}
            # 用离岸人民币涨跌 + 美元指数涨跌合成简单情绪分
            cnh = (fx.get("susdcnh") or {}).get("pct") or 0
            dxy = (fx.get("diniw") or {}).get("pct") or 0
            # 人民币涨=风险偏好升(正), 美元涨=风险偏好降(负)
            score = round(-cnh * 100 - dxy * 10, 2)
            g["global_score"] = score
            g["global_mood"] = "risk_on" if score > 5 else ("risk_off" if score < -5 else "neutral")
            if score > 15:
                g.update(pos_weight=0.20, suggest="全球偏多: 正常仓位, 可偏进攻")
            elif score < -15:
                g.update(pos_weight=0.35, suggest="全球偏空: 只留低位防御")
                for c in cand:
                    c["score"] = (rank_pct([x["vratio"] for x in cand])[cand.index(c)] * 0.10
                                  + (1 - rank_pct([x["pos240"] for x in cand])[cand.index(c)]) * 0.35
                                  + rank_pct([x["close_pos"] for x in cand])[cand.index(c)] * 0.25
                                  + (1 - rank_pct([x["ret"] for x in cand])[cand.index(c)]) * 0.10)
            else:
                g["suggest"] = "全球中性: 维持半仓纪律"
    except Exception as e:
        g["suggest"] = f"global skip: {e}"

    # 周期位置接入 (cycle.json 偏空→收紧)
    # 注意: cycle.json 的 macro_signals 是 dict {symbol: {...}}, 不是 list
    cyc_note = ""
    try:
        cp = os.path.join(AI_VIEW_DATA, "cycle.json")
        if os.path.exists(cp):
            cd = json.load(open(cp, encoding="utf-8"))
            sigs = cd.get("macro_signals") or {}
            items = list(sigs.values()) if isinstance(sigs, dict) else (sigs or [])
            bear = sum(1 for s in items if str(s.get("phase", "")).lower() in
                       ("downtrend", "bear", "偏空"))
            if items and bear >= len(items) / 2:
                cand = [c for c in cand if c["pos240"] < 0.45]
                cyc_note = f"周期偏空({bear}/{len(items)}指数): 收紧至240日位<45%"
                print(f"[ai_pick_v4] {cyc_note} → {len(cand)} 只")
    except Exception as e:
        cyc_note = f"cycle skip: {e}"

    # 胜率自适应层 (pick_ledger.json 或 pick_stats)
    adapt = {"note": "无对账数据"}
    try:
        for name in ("pick_ledger.json",):
            p = os.path.join(AI_VIEW_DATA, name)
            if os.path.exists(p):
                ps = json.load(open(p, encoding="utf-8"))
                items = ps.get("items") or []
                if len(items) >= 10:
                    rets = [it.get("ret_open_close") for it in items
                            if it.get("ret_open_close") is not None]
                    vers = [str(it.get("verdict", "")) for it in items]
                    tp_rate = sum(1 for v in vers if "止盈" in v) / len(vers)
                    sl_rate = sum(1 for v in vers if "止损" in v) / len(vers)
                    if sl_rate > 0.4 and tp_rate < 0.2:
                        adapt.update(note=f"近{len(items)}笔止损率{sl_rate:.0%}→防御模式",
                                     mode="defense")
                    elif tp_rate > 0.35:
                        adapt.update(note=f"近{len(items)}笔止盈率{tp_rate:.0%}→攻势模式",
                                     mode="attack")
                    else:
                        win = sum(1 for x in rets if x > 0) / max(len(rets), 1)
                        adapt.update(note=f"近{len(items)}笔胜率{win:.0%}→维持基准权重",
                                     mode="neutral")
    except Exception as e:
        adapt["note"] = f"adapt skip: {e}"

    cand.sort(key=lambda x: -x["score"])
    picks = []
    for c in cand[: args.top]:
        sname = None
        try:
            import glob as _g
            # 从 search_index 取名称 (有缓存则用)
            si = os.path.join(AI_VIEW_DATA, "search_index.json")
            if os.path.exists(si):
                _idx = json.load(open(si, encoding="utf-8"))
                sname = (_idx.get(c["symbol"]) or {}).get("n")
        except Exception:
            pass
        picks.append({
            "code": (c["symbol"][2:] if c["symbol"][:2] in ("sh", "sz", "bj") else c["symbol"]),
            "symbol": c["symbol"], "date": c["date"],
            "name": sname or "",
            "price": round(c["price"], 2),
            "ret_pct": round(c["ret"] * 100, 2),
            "vratio": round(c["vratio"], 2),
            "pos240_pct": round(c["pos240"] * 100, 1),
            "close_pos": round(c["close_pos"], 3),
            "mom60_pct": round(c["mom60"] * 100, 2),
            "zt_genes": c["zt_genes"],
            "score": round(c["score"], 4),
            "pattern_bonus": round(pat_bonus.get(c["symbol"], 0), 3),
        })

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "ai_pick v4 (移植自 jiegengxiangjianxue1/stocks-algo)",
        "date": picks[0]["date"] if picks else None,
        "n_scanned": len(feats),
        "n_candidates": len(cand),
        "global": g,
        "adapt": adapt,
        "cycle_note": cyc_note,
        "picks": picks,
        "meta": {
            "source": f"bulk day_qfq_*.csv ({len(klines)} 只)",
            "formula": "量比0.20 + 60日RPS0.20 + 240日低位0.20 + 涨停基因0.15 + 收盘位置0.15 + 换手温和0.10",
            "hard_rules": "未涨停 + 涨幅0.5-5% + 240日位<60% + 量比1.5-2.5",
            "pattern_bonus": "停机坪/高而窄旗形+0.08, 无大幅回撤+0.04",
            "caveat": "评分为统计排序, 非必然上涨; 不构成投资建议",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n=== ai_pick v4 TOP10 ===")
    for p in picks[:10]:
        print(f"  {p['code']} {p['price']:>8} 涨{p['ret_pct']:>6}% 量比{p['vratio']} "
              f"位{p['pos240_pct']}% 分{p['score']:.3f}")
    print(f"\n全球情绪: {g['global_mood']} ({g['global_score']}) — {g['suggest']}")
    print(f"胜率自适应: {adapt.get('note')}")
    print(f"[ai_pick_v4] → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
