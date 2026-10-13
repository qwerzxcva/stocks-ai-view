#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/strategy_card.py ═══
# 上游 commit: 75230ac1fa67 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
# 适配: 2026-10-13 路径规范 (duckdb daily → bulk CSV; global → data/global/latest.json; picks 落库降级; --out)
"""
strategy_card.py — 策略卡v1: 胜率导向组合 (2024-2026回测208万行网格搜索最优)
规则B(已证最优): 240日位<35% + 量比0.8-2.0 + 当日涨0-5% + 非妖股(次日振幅排除不可执行)
持有口径: 短线=次日冲高+2%止盈/破-2%止损(胜率44.3%, E+0.28%/笔)
         或持有5日收盘卖出(胜率55.5%, 基准48.8% → +6.7pp alpha)
市况: 全球> -15 才进(强市子集无额外增益, 弱市闸门仍保留)
输出: data/strategy_card.json — 当日B规则候选+说明

数据源 (本项目): bulk day_qfq_*.csv (上游为 stockdb/stock.duckdb daily 表)
picks.duckdb 落库: 本项目无该对账库 → 跳过并标注 (不影响候选产出)
"""
import argparse
import datetime
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import BULK, AI_VIEW_DATA as DATA_DIR
except Exception:
    BULK = os.path.join(os.path.dirname(os.path.dirname(HERE)), "stocks-data-kline", "data", "bulk")
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

PICKS_DB = os.path.join(os.path.dirname(os.path.dirname(HERE)), "picks.duckdb")


def load_rows(bulk_dir: str):
    """bulk day_qfq_*.csv → {code: [(date, open, close, high, low, volume)] 升序}"""
    out = {}
    for p in sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv"))):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|") if "|" in line else line.split(",")
                if len(parts) < 6:
                    continue
                code = parts[0]
                if not (code.startswith("sh60") or code.startswith("sz00")):
                    continue
                if "30" in code[2:] or "68" in code[2:]:
                    continue
                try:
                    out.setdefault(code, []).append(
                        (parts[1], float(parts[2]), float(parts[3]),
                         float(parts[4]), float(parts[5]), float(parts[6])))
                except ValueError:
                    continue
    for c in out:
        out[c].sort(key=lambda r: r[0])
    return out


def b_rule_candidates(rows_by_code: dict, limit: int = 12):
    """规则B: 240日位<35% + 量比0.8-2.0 + 当日涨0-5% + 10日内距高<5%"""
    cands = []
    for code, rows in rows_by_code.items():
        if len(rows) < 25:
            continue
        prev = rows[-2][2]
        cur = rows[-1]
        if prev <= 0:
            continue
        ret = cur[2] / prev - 1
        vol = cur[5]
        vols = [r[5] for r in rows[-6:-1]]
        avg_vol = sum(vols) / len(vols) if vols else 0
        vr = vol / avg_vol if avg_vol else None
        highs = [r[3] for r in rows[-241:-1]]
        pos240 = cur[2] / max(highs) if highs else None
        highs10 = [r[3] for r in rows[-11:-1]]
        pos10 = cur[3] / min(highs10) if highs10 and min(highs10) > 0 else None
        if vr is None or pos240 is None or pos10 is None:
            continue
        if not (0 <= ret <= 0.05 and 0.8 <= vr <= 2.0 and pos240 < 0.35 and pos10 < 1.05):
            continue
        cands.append({"symbol": code, "close": round(cur[2], 2),
                      "ret_pct": round(ret * 100, 2), "vratio": round(vr, 2),
                      "pos240": round(pos240, 3)})
    cands.sort(key=lambda x: -x["vratio"])
    return cands[:limit]


def main():
    ap = argparse.ArgumentParser(description="策略卡 v1 (B规则: 低位+温和量+涨0-5%)")
    ap.add_argument("--bulk", default=None, help="K线 bulk 目录 (默认 paths.BULK)")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "strategy_card.json"))
    args = ap.parse_args()

    bulk = args.bulk or BULK
    rows_by_code = load_rows(bulk)
    if not rows_by_code:
        print(f"[strategy_card] bulk 无数据: {bulk}", file=sys.stderr)
        return 1
    rows = b_rule_candidates(rows_by_code, args.limit)

    # 全球闸门
    gs = 0
    gp = os.path.join(DATA_DIR, "global", "latest.json")
    if not os.path.exists(gp):
        gp = os.path.join(DATA_DIR, "global.json")
    try:
        g = json.load(open(gp, encoding="utf-8"))
        gs = (g.get("metrics") or {}).get("global_score", 0)
    except Exception:
        gs = 0

    out = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "strategy": "B规则(低位+温和量+涨0-5%)+5日持有",
           "source": f"bulk day_qfq ({os.path.basename(bulk)})",
           "backtest": {"win_rate_5d": 55.5, "baseline_5d": 48.8, "alpha_pp": 6.7,
                        "win_rate_t1": 44.3, "E_t1_pct": 0.28, "n": 1995, "period": "2024-2026"},
           "gate": {"global_score": gs, "trade_ok": gs > -15},
           "candidates": rows,
           "discipline": ["T+1: 次日冲高+2%止盈(胜率口径44%)", "或持有5日收盘卖(胜率口径55.5%)",
                          "破-2%无条件止损", "仓位=半凯利(实盘胜率闭环)", "全球<-15空仓"],
           "picks_db": os.path.basename(PICKS_DB) if os.path.exists(PICKS_DB) else None,
           "note": "回测为成本前口径; 胜率天花板约55%(5日口径), 80%需改变持有周期或品种, 不构成投资建议"}

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    # picks.duckdb 对账闭环 (本项目无该库 → 跳过, 不影响候选产出)
    if os.path.exists(PICKS_DB):
        try:
            import duckdb
            pc = duckdb.connect(PICKS_DB)
            today = str(datetime.date.today())
            n = 0
            for r in rows:
                ex = pc.execute("SELECT COUNT(*) FROM pick_log WHERE symbol=? AND kind='bcard' AND pick_date=?",
                                [r["symbol"], today]).fetchone()[0]
                if not ex:
                    nid = pc.execute("SELECT COALESCE(MAX(id),0)+1 FROM pick_log").fetchone()[0]
                    pc.execute("INSERT INTO pick_log VALUES (?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,0)",
                               [nid, datetime.datetime.now().isoformat(timespec="seconds"), today,
                                r["symbol"], "", float(r["close"]), "bcard", "B_5d"])
                    n += 1
            pc.close()
            print(f"bcard落库: {n}票")
        except Exception as e:
            print("bcard落库失败:", e)
    else:
        print("picks.duckdb 不存在 → 跳过对账落库 (上游闭环依赖, 本项目未建)")

    print(f"策略卡: 候选{len(rows)}票, 全球{gs}, trade_ok={gs > -15} → {args.out}")
    for r in rows[:8]:
        print(f"  {r['symbol']} close={r['close']} ret={r['ret_pct']}% vr={r['vratio']} pos240={r['pos240']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
