#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/intraday_ledger.py ═══
# 上游 commit: 03e845e2de11 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""intraday_ledger.py — 把日内信号两榜落picks.duckdb闭环(kind=chase/dip), 复用pick_tracker自动对账"""
import json, duckdb, datetime

def main():
    sig = json.load(open("/home/agentuser/stockdb/data/intraday_signal.json"))
    d1 = sig["date"]
    con = duckdb.connect("/home/agentuser/stockdb/stock.duckdb", read_only=True)
    px = {r[0]: (r[1], r[2]) for r in con.execute(
        f"SELECT symbol, close, open FROM daily WHERE date='{d1}'").fetchall()}
    con.close()
    try:
        si = json.load(open("/home/agentuser/stockdb/data/search_index.json"))
        names = {k: v.get("n", "") for k, v in si.items()}
    except Exception:
        names = {}
    con = duckdb.connect("/home/agentuser/stockdb/picks.duckdb")
    n = 0
    for item in sig.get("chase_list", []) + sig.get("dip_watch", []):
        sym = item["symbol"]
        kind = "chase" if item in sig.get("chase_list", []) or item.get("hint", "").startswith("隔日延续") else "dip"
        kind = "chase" if item.get("hint", "").startswith("隔日延续") else "dip"
        if sym not in px or not px[sym][0]:
            continue
        exists = con.execute("SELECT 1 FROM pick_log WHERE pick_date=? AND symbol=? AND kind=?",
                             [d1, sym, kind]).fetchone()
        if exists:
            continue
        nid = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM pick_log").fetchone()[0]
        con.execute("INSERT INTO pick_log (id, ts, pick_date, symbol, name, entry_price, kind, rule, settled) VALUES (?,?,?,?,?,?,?,?,false)",
                    [nid, datetime.datetime.now().isoformat(timespec="seconds"), d1, sym,
                     names.get(sym, ""), px[sym][0], kind,
                     f"intraday_{item.get('strength', 0)}"])
        n += 1
    con.close()
    print(f"ledger +{n} (chase/dip @ {d1})")

if __name__ == "__main__":
    main()
