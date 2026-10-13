#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/pick_tracker.py ═══
# 上游 commit: 5c6582845e7a · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
"""
pick_tracker.py — 推荐闭环对账引擎 (2026-10-13)
1) pick_log 表: 每日 ai_pick+tailpick 结构化落库 (symbol/entry/price/规则标签)
2) 次日自动对账: 用 wh/T+1 日K 计算收益 (开盘买→14:45冲高/收盘/T+2三口径)
3) winrate: 按规则标签分组胜率 → 回流优化 ai_pick 权重
T+1纪律: 14:45为决策点 — 止盈+2%/止损-2%
用法: pick_tracker.py [log|settle|stats]
"""
import json, os, sys, datetime, duckdb

HOME = os.path.expanduser("~")
STOCKDB = os.environ.get("STOCKS_STOCKDB") or os.path.join(HOME, "stockdb")
os.makedirs(os.path.join(STOCKDB, "data"), exist_ok=True)
DB = os.path.join(STOCKDB, "picks.duckdb")  # 独立库避开server锁
WH = os.path.join(STOCKDB, "wh")

def init(con):
    con.execute("""CREATE TABLE IF NOT EXISTS pick_log(
        id INTEGER PRIMARY KEY, ts TIMESTAMP DEFAULT now(), pick_date VARCHAR,
        symbol VARCHAR, name VARCHAR, entry_price DOUBLE, kind VARCHAR,
        rule VARCHAR, next_date VARCHAR, ret_open_close DOUBLE, ret_open_high DOUBLE,
        ret_open_low DOUBLE, ret_t2 DOUBLE, verdict VARCHAR, settled INTEGER DEFAULT 0)""")

def cmd_log():
    """今日 ai_picks.json + tailpick → pick_log(去重)"""
    if not os.path.exists(DB):
        print(f"picks.duckdb not found at {DB}; run on the runner that owns it")
        return
    con = duckdb.connect(DB)
    init(con)
    today = datetime.date.today().isoformat()
    n = con.execute("SELECT count(*) FROM pick_log WHERE pick_date=?", [today]).fetchone()[0]
    if n > 0:
        print("already logged", today, n); return
    rows = []
    try:
        ap = json.load(open(f"{STOCKDB}/data/ai_picks.json"))
        for p in ap.get("picks", []):
            rows.append((p["symbol"], p.get("name", ""), p.get("close"), "ai_pick",
                         ap.get("rule", "")))
    except Exception as e:
        print("ai_picks err:", e)
    try:
        tp = json.load(open(f"{STOCKDB}/data/tailpick.json"))
        for p in tp.get("picks", []):
            rows.append((p["symbol"], p.get("name", ""), p.get("price"), "tailpick",
                         tp.get("rule", "")))
    except Exception:
        try:
            # tailpick在analysis表里
            con2 = duckdb.connect(f"{STOCKDB}/stock.duckdb", read_only=True)
            for (pl,) in con2.execute("SELECT payload FROM analysis WHERE kind='tailpick' ORDER BY id DESC LIMIT 1").fetchall():
                con2.close()
                break
                j = json.loads(pl)
                for p in j.get("picks", []):
                    rows.append((p["symbol"], p.get("name", ""), p.get("price"), "tailpick", j.get("rule", "")))
        except Exception as e:
            print("tailpick err:", e)
    if not rows:
        print("no picks today"); return
    mx = con.execute("SELECT COALESCE(MAX(id),0) FROM pick_log").fetchone()[0]
    for i, (sym, name, price, kind, rule) in enumerate(rows):
        con.execute("INSERT INTO pick_log VALUES (?,now(),?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,0)",
                    [mx+1+i, today, sym, name, price, kind, rule])
    con.commit()
    print(f"logged {len(rows)} picks for {today}")

def _daily_map(symbol):
    """从 wh daily 读该票日K {date: (open,close,high,low)}"""
    import pyarrow.parquet as pq, glob
    out = {}
    for f in sorted(glob.glob(f"{WH}/daily/*.parquet")):
        try:
            t = pq.read_table(f, columns=["symbol","date","open","close","high","low"])
            d = t.to_pydict()
            for s, dt, o, c, h, l in zip(d["symbol"], d["date"], d["open"], d["close"], d["high"], d["low"]):
                if s == symbol: out[str(dt)] = (o, c, h, l)
        except Exception: pass
    if not out:
        # 兜底: 腾讯fqkline拉20日
        import urllib.request
        UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
        import time as _t
        for attempt in range(4):
            try:
                for url in (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,20,qfq",
                            f"https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param={symbol},day,,,20"):
                    try:
                        d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15))["data"][symbol]
                        arr = d.get("qfqday") or d.get("day")
                        if not arr: continue
                        for r in arr:
                            out[r[0]] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                        if out: break
                    except Exception: continue
                if out: break
            except Exception:
                _t.sleep(2)
    return out

def cmd_settle():
    """对账: pick_date 之后第一个交易日的 T+1 表现"""
    if not os.path.exists(DB):
        print(f"picks.duckdb not found at {DB}; run on the runner that owns it")
        return
    con = duckdb.connect(DB)
    init(con)
    pend = con.execute("SELECT id, pick_date, symbol, entry_price FROM pick_log WHERE settled=0 AND kind<>'bcard'").fetchall()
    print(f"pending: {len(pend)}")
    for pid, pd, sym, ep in pend:
        if not sym or not ep: continue
        dm = _daily_map(sym)
        dates = sorted(d for d in dm if d > pd)
        if not dates:
            print(f"  {sym} no future bar yet"); continue
        d1 = dates[0]
        o, c, h, l = dm[d1]
        ret_oc = (c/o-1)*100; ret_oh = (h/o-1)*100; ret_ol = (l/o-1)*100
        # T+1纪律: 14:45 冲高+2%止盈 / 破-2%止损 / 否则收盘
        if ret_oh >= 2: verdict, ret = "止盈+2%", 2.0
        elif ret_ol <= -2: verdict, ret = "止损-2%", -2.0
        else: verdict, ret = "收盘", ret_oc
        d2 = dates[1] if len(dates) > 1 else None
        ret_t2 = (dm[d2][1]/o-1)*100 if d2 else None
        con.execute("""UPDATE pick_log SET next_date=?, ret_open_close=?, ret_open_high=?,
                       ret_open_low=?, ret_t2=?, verdict=?, settled=1 WHERE id=?""",
                    [d1, round(ret_oc,2), round(ret_oh,2), round(ret_ol,2),
                     round(ret_t2,2) if ret_t2 is not None else None, verdict, pid])
        print(f"  {sym} {pd}→{d1}: {verdict} {ret:+.2f}% (OC {ret_oc:+.2f} OH {ret_oh:+.2f})")
    con.commit()

def cmd_stats():
    if not os.path.exists(DB):
        print(f"picks.duckdb not found at {DB}; run on the runner that owns it")
        return
    con = duckdb.connect(DB, read_only=True)
    total = con.execute("SELECT count(*), sum(CASE WHEN ret_open_close>0 THEN 1 ELSE 0 END) FROM pick_log WHERE settled=1").fetchone()
    if not total[0]:
        print("no settled picks"); return
    print(f"=== 总体: {total[0]}笔 胜率{100*total[1]/total[0]:.0f}% ===")
    by_rule = con.execute("""SELECT kind, count(*), avg(ret_open_close), 
        sum(CASE WHEN ret_open_close>0 THEN 1 ELSE 0 END),
        avg(ret_open_high), sum(CASE WHEN verdict='止盈+2%' THEN 1 ELSE 0 END),
        sum(CASE WHEN verdict='止损-2%' THEN 1 ELSE 0 END)
        FROM pick_log WHERE settled=1 GROUP BY kind""").fetchall()
    for k, n, avg, win, aoh, tp, sl in by_rule:
        print(f"  {k}: {n}笔 胜率{100*win/n:.0f}% 均收益{avg:+.2f}% 冲高均值{aoh:+.2f}% 止盈{tp}/止损{sl}")
    # 明细
    print("\n最近10笔:")
    for r in con.execute("""SELECT pick_date, symbol, name, entry_price, next_date, verdict,
        ret_open_close, ret_t2 FROM pick_log WHERE settled=1 ORDER BY pick_date DESC, id DESC LIMIT 10""").fetchall():
        print(" ", r[0], r[1], r[2], f"买{r[3]}→{r[4]}", r[5], f"{r[6]:+.2f}%", f"T+2 {r[7]:+.2f}%" if r[7] is not None else "")
    # 输出json供网站
    out = {"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
    out["overall"] = {"n": total[0], "win_rate": round(100*total[1]/total[0], 1)}
    out["by_kind"] = [{"kind": k, "n": n, "win_rate": round(100*win/n,1), "avg_ret": round(avg,2),
                       "avg_high": round(aoh,2), "tp": tp, "sl": sl} for k, n, avg, win, aoh, tp, sl in by_rule]
    out["recent"] = [{"date": r[0], "symbol": r[1], "name": r[2], "entry": r[3], "next": r[4],
                      "verdict": r[5], "ret": r[6], "t2": r[7]}
                     for r in con.execute("""SELECT pick_date, symbol, name, entry_price, next_date,
                      verdict, ret_open_close, ret_t2 FROM pick_log WHERE settled=1
                      ORDER BY pick_date DESC, id DESC LIMIT 20""").fetchall()]
    json.dump(out, open(f"{STOCKDB}/data/pick_stats.json", "w"), ensure_ascii=False)
    print("saved pick_stats.json")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    {"log": cmd_log, "settle": cmd_settle, "stats": cmd_stats}[cmd]()
