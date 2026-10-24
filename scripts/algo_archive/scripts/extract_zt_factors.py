#!/usr/bin/env python3
"""情绪筹码因子: 东财涨停池/炸板池 → 个股连板高度/封单强度"""
import urllib.request, json, duckdb, sys
UT = "7eea3edcaed734bea9cbfc24409ed989"
def fetch(date):
    out = {}
    for name, ep in [("zt","getTopicZTPool"), ("zb","getTopicZBPool"), ("dt","getTopicDTPool")]:
        try:
            url = f"https://push2ex.eastmoney.com/{ep}?ut={UT}&dpt=wz.ztzt&Pageindex=0&pagesize=500&sort=fbt%3Aasc&date={date}"
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/"})
            d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
            out[name] = (d.get('data') or {}).get('pool') or []
        except Exception:
            out[name] = []
    return out
def main(date="20261020"):
    pools = fetch(date)
    con = duckdb.connect('/home/agentuser/stockdb/factor_matrix.duckdb')
    con.execute("""create table if not exists zt_factors(
      symbol varchar, as_of date,
      is_zt int, lianban int, fengdan_wan double, zhaban int,
      is_zb int, is_dt int)""")
    n = 0
    asof = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    for p in pools.get('zt', []):
        sym = ("sh" if p['m']==1 else "sz") + p['c']
        con.execute("delete from zt_factors where symbol=? and as_of=?", [sym, asof])
        con.execute("insert into zt_factors values (?,?,?,?,?,?,?,?)",
          [sym, asof, 1, p.get('lbc',1), p.get('fund',0)/1e4, p.get('zbc',0), 0, 0])
        n += 1
    for p in pools.get('zb', []):
        sym = ("sh" if p['m']==1 else "sz") + p['c']
        con.execute("delete from zt_factors where symbol=? and as_of=? and is_zt=0", [sym, asof])
        con.execute("insert into zt_factors values (?,?,?,?,?,?,?,?)",
          [sym, asof, 0, 0, 0, 0, 1, 0])
    for p in pools.get('dt', []):
        sym = ("sh" if p['m']==1 else "sz") + p['c']
        con.execute("delete from zt_factors where symbol=? and as_of=? and is_zt=0 and is_zb=0", [sym, asof])
        con.execute("insert into zt_factors values (?,?,?,?,?,?,?,?)",
          [sym, asof, 0, 0, 0, 0, 0, 1])
    con.execute("checkpoint")
    print(f"zt={len(pools['zt'])} zb={len(pools['zb'])} dt={len(pools['dt'])} rows={n}")
    # 市场级情绪读数
    zt = pools['zt']
    if zt:
        lb_dist = {}
        for p in zt: lb_dist[p.get('lbc',1)] = lb_dist.get(p.get('lbc',1),0)+1
        print("连板分布:", dict(sorted(lb_dist.items())))
        print("最高连板:", max(p.get('lbc',1) for p in zt))
    con.close()
if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv)>1 else "20261020")
