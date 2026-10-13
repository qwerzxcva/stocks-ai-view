#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/engine_fusion.py ═══
# 上游 commit: 939c15aba494 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
# 适配: 2026-10-13 路径规范 (stockdb/data → data/ 本项目产物; picks.duckdb 缺库时记 missing; --out 落盘)
"""
engine_fusion.py — 引擎融合器 (独特算法: 多引擎加权状态机)
五个独立引擎 → 单一状态分 E(-100..+100) + 一致性度量:
  E = 0.30*market_phase + 0.25*cycle_v2 + 0.20*global_momentum + 0.15*win_loop + 0.10*kelly_signal
一致性 = 五引擎同号比例 (高一致→可信, 低一致→观望)
输出: data/engine_fusion.json → decision.py复核

本项目数据映射:
  market_phase.json  ← phase_runner.py
  kline_sim.json     ← kline_sim.py
  global.json        ← global_panel.py (本项目已产出, 含 metrics.global_score)
  picks.duckdb       ← 本项目无此库 (上游对账库), 缺库时该两路记 0 并标注 missing
"""
import argparse
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR, DUCKDB_PATH
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
    DUCKDB_PATH = os.path.join(os.path.dirname(os.path.dirname(HERE)),
                               "stocks-data-live", "data", "stocks.duckdb")

PICKS_DB = os.path.join(os.path.dirname(os.path.dirname(HERE)), "picks.duckdb")


def _load(name):
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def phase_score(missing):
    ph = _load("market_phase.json")
    if ph is None:
        missing.append("market_phase.json")
        return 0
    m = {"ice": -80, "ebb": -50, "repair": -20, "ignite": 40, "rally": 70, "climax": 20}
    return m.get(ph.get("phase", ""), 0)


def cycle_score(missing):
    """周期: kline_sim同状态样本均值(日频) + 连段方向"""
    ks = _load("kline_sim.json")
    if ks is None:
        missing.append("kline_sim.json")
        return 0
    idx = ks.get("index") or {}
    m = idx.get("ret_mean_pct") or 0
    p_up = idx.get("p_up") or 50
    return max(-80, min(80, (p_up - 50) * 2 + m * 20))


def global_score(missing):
    g = _load("global.json") or _load("global_panel.json")
    if g is None:
        missing.append("global.json")
        return 0
    sc = (g.get("metrics") or {}).get("global_score", 0)
    return max(-100, min(100, sc * 2))


def _picks_query(sql):
    """上游从 picks.duckdb 读对账闭环; 本项目无该库 → 记 missing 返回 None。"""
    if not os.path.exists(PICKS_DB):
        return None
    try:
        import duckdb
        con = duckdb.connect(PICKS_DB, read_only=True)
        row = con.execute(sql).fetchone()
        con.close()
        return row
    except Exception:
        return None


def winloop_score(missing):
    r = _picks_query("SELECT COUNT(*), AVG(CASE WHEN verdict LIKE '止盈%' THEN 1.0 ELSE 0.0 END) "
                     "FROM pick_log WHERE settled=true")
    if r is None:
        missing.append("picks.duckdb (win_loop)")
        return 0
    n, w = r
    if not n:
        return 0
    return (w - 0.45) * 400  # 45%基准, 每1%差=4分


def kelly_score(missing):
    r = _picks_query("""SELECT COUNT(*),
        AVG(CASE WHEN verdict LIKE '止盈%' THEN 1.0 ELSE 0.0 END),
        AVG(CASE WHEN verdict LIKE '止盈%' THEN ABS(ret_open_close) END),
        AVG(CASE WHEN verdict LIKE '止损%' THEN ABS(ret_open_close) END) FROM pick_log WHERE settled=true""")
    if r is None:
        missing.append("picks.duckdb (kelly)")
        return 0
    _n, _w, _aw, _al = r
    W = _w or 0.45; R = (_aw or 0.02)/(_al or 0.02) if _al else 2.0
    f = W - (1-W)/R
    return max(-50, min(50, f * 200))


def main():
    ap = argparse.ArgumentParser(description="引擎融合器 (多引擎加权状态机)")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "engine_fusion.json"))
    args = ap.parse_args()

    missing = []
    parts = {"market_phase": (phase_score(missing), 0.30), "cycle_v2": (cycle_score(missing), 0.25),
             "global": (global_score(missing), 0.20), "win_loop": (winloop_score(missing), 0.15),
             "kelly": (kelly_score(missing), 0.10)}
    E = round(sum(v * w for v, w in parts.values()), 1)
    signs = [1 if v > 5 else -1 if v < -5 else 0 for v, _ in parts.values()]
    nz = [s for s in signs if s != 0]
    agree = (max(nz.count(1), nz.count(-1)) / len(nz)) if nz else 0
    state = ("进攻" if E > 30 and agree >= 0.6 else "防守" if E < -30 and agree >= 0.6 else
             "进攻(分歧)" if E > 30 else "防守(分歧)" if E < -30 else "中性观望")
    out = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
           "E": E, "state": state, "agreement": round(agree, 2),
           "parts": {k: {"score": v, "weight": w} for k, (v, w) in parts.items()},
           "missing_inputs": missing,
           "note": "多引擎加权融合, 一致性低→观望, 不构成投资建议"}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"E={E} state={state} 一致性={agree:.0%} | " + " ".join(f"{k}:{v}" for k, (v, _) in parts.items()))
    if missing:
        print("missing:", ", ".join(missing))
    print("->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
