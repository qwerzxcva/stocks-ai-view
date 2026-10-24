# intraday_archive.py — 收盘后归档当日强势股分时(供S7横盘突破/分时形态训练) (2026-10-24)
import urllib.request, json, duckdb, datetime
def fetch_minute(sym):
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(
            f'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}',
            headers={'User-Agent': 'Mozilla/5.0'}), timeout=15).read())
        return d['data'][sym]['data']['data']
    except Exception:
        return []
def main():
    con = duckdb.connect('/home/agentuser/stockdb/knowledge.duckdb')
    con.execute("""CREATE TABLE IF NOT EXISTS intraday (
        sym VARCHAR, d DATE, minute VARCHAR, price DOUBLE, vol DOUBLE)""")
    today = con.execute('SELECT max(zt_date) FROM zt_pool').fetchone()[0]
    # 取今日涨停池里的票(未来最强票的分时形态)
    syms = [r[0] for r in con.execute(
        "SELECT DISTINCT code FROM zt_pool WHERE zt_date = ?", [today]).fetchall()]
    n = 0
    for code in syms[:40]:
        sym = ('sh' if code.startswith('6') else 'sz') + code
        if con.execute("SELECT count(*) FROM intraday WHERE sym=? AND d=?", [sym, today]).fetchone()[0]:
            continue
        for line in fetch_minute(sym):
            p = line.split()
            if len(p) >= 3:
                con.execute("INSERT INTO intraday VALUES (?,?,?,?,?)",
                    (sym, today, p[0], float(p[1]), float(p[2])))
        n += 1
    total = con.execute("SELECT count(*) FROM intraday").fetchone()[0]
    con.close()
    print(f'archived {n} syms, total {total} rows')
if __name__ == '__main__':
    main()
