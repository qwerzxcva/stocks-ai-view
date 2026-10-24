"""涨停池采集 (同花顺 dataapi, 替代东财 push2ex 403) — 回补 10-21/10-22 + 每日定时"""
import urllib.request, json, duckdb, time, sys
from datetime import date, timedelta
H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0', 'Referer': 'https://data.10jqka.com.cn/'}

def fetch_day(yyyymmdd):
    out = []
    page = 1
    while True:
        url = (f'https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool?page={page}&limit=100'
               f'&field=199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004'
               f'&filter=HS,GEM2STAR&order_field=330324&order_type=0&date={yyyymmdd}')
        d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=15).read())
        info = (d.get('data') or {}).get('info') or []
        for r in info:
            code = r.get('code', '')
            sym = ('sh' if code.startswith('6') or code.startswith('68') else 'sz') + code
            hd = r.get('high_days')
            try: lianban = int(hd)
            except (TypeError, ValueError): lianban = 1 if (hd and '首' in str(hd)) else 1
            out.append({'symbol': sym, 'lianban': lianban, 'fengdan_wan': (r.get('limit_up_amount') or 0)/1e4,
                        'zhaban': r.get('open_num') or 0, 'is_zb': False, 'is_dt': False})
        total = ((d.get('data') or {}).get('page') or {}).get('total') or 0
        if len(out) >= total or not info: break
        page += 1
        time.sleep(1)
    return out

def main():
    con = duckdb.connect('/home/agentuser/stockdb/factor_matrix.duckdb')
    con.execute("SET memory_limit='2GB'")
    have = {r[0] for r in con.execute("SELECT DISTINCT as_of FROM zt_factors").fetchall()}
    today = date.today()
    for back in range(0, 6):
        d = today - timedelta(days=back)
        ds = d.strftime('%Y%m%d')
        if d.weekday() >= 5: continue
        if d in have: continue
        try:
            rows = fetch_day(ds)
        except Exception as e:
            print(ds, 'fetch err', str(e)[:50]); continue
        for r in rows:
            con.execute("INSERT INTO zt_factors VALUES (?,?,?,?,?,?,?,?)",
                        (r['symbol'], d, True, r['lianban'], r['fengdan_wan'], r['zhaban'], r['is_zb'], r['is_dt']))
        # 市场日汇总
        con.execute("DELETE FROM zt_market_ts WHERE date=?", (d,))
        con.execute("INSERT INTO zt_market_ts VALUES (?,?,?,?,?)", (d, len(rows), 0, max([r['lianban'] for r in rows] or [0]),
                    sum(1 for r in rows if r['lianban'] >= 2)))
        print(ds, len(rows), '只涨停 已入库')
    con.close()

if __name__ == '__main__':
    main()
