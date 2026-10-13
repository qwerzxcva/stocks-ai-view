#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/settle_bcard.py ═══
# 上游 commit: 0ba9f80122fc · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""settle_bcard.py — B规则策略卡(bcard)5日口径对账:
买价=signal日收盘, 结算=第5个交易日收盘价(55.5%胜率口径), 中途破-2%(T+1内收盘口径)标stop_early.
cron 交易日 15:36 (在pick_tracker settle 15:35之后跑, bcard票由本脚本独立结算, pick_tracker跳过bcard).
"""
import duckdb, json, glob, datetime, os

PICKS = "/home/agentuser/stockdb/picks.duckdb"
WH = "/home/agentuser/stockdb/wh"

def kline_of(symbol):
    import pyarrow.parquet as pq
    out = {}
    for f in sorted(glob.glob(f"{WH}/daily/*.parquet")):
        try:
            t = pq.read_table(f, columns=["symbol", "date", "open", "close"])
            d = t.to_pydict()
            for s, dt, o, c in zip(d["symbol"], d["date"], d["open"], d["close"]):
                if s == symbol:
                    out[str(dt)] = (float(o), float(c))
        except Exception:
            pass
    return out

def main():
    con = duckdb.connect(PICKS)
    pend = con.execute("SELECT id, pick_date, symbol, entry_price FROM pick_log WHERE kind='bcard' AND settled=0").fetchall()
    n_done = 0
    for pid, pdate, sym, ep in pend:
        kl = kline_of(sym)
        days = sorted(d for d in kl if d > pdate)
        if len(days) < 5:
            continue  # 还不满5个交易日
        d5 = days[4]
        close5 = kl[d5][1]
        # 中途止损检查: T+1(第1日)收盘是否破-2%
        d1 = days[0]
        stop_early = kl[d1][1] / ep - 1 < -0.02
        ret5 = (close5 / ep - 1) * 100
        verdict = "stop_early" if stop_early else ("win5" if ret5 > 0 else "loss5")
        con.execute("""UPDATE pick_log SET next_date=?, ret_open_close=?, verdict=?, settled=1 WHERE id=?""",
                    [d5, round(ret5, 2), verdict, pid])
        n_done += 1
    stats = con.execute("""SELECT verdict, count(*), avg(ret_open_close) FROM pick_log
                           WHERE kind='bcard' AND settled=1 GROUP BY verdict""").fetchall()
    con.close()
    out = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "settled_today": n_done,
           "bcard_stats": [{"verdict": v, "n": n, "avg_ret5": round(a, 2)} for v, n, a in stats]}
    os.makedirs("/home/agentuser/stockdb/data", exist_ok=True)
    json.dump(out, open("/home/agentuser/stockdb/data/bcard_stats.json", "w"), ensure_ascii=False, indent=1)
    print(f"bcard settle: {n_done} done; stats={out['bcard_stats']}")

if __name__ == "__main__":
    main()
