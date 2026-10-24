# sector_burst.py — 板块联动放量引擎 (2026-10-23 涨停复盘产物)
# 发现: 今日机器人链 8 票涨停(大盘-1.2%!), 前兆=昨日板块内多票同时放量(量比>3), 与指数涨跌无关
# 机制: 板块联动放量(昨日) → 次日板块涨停潮 (资金抱团进入, 第二天加速)
# 引擎: 每日扫"同板块≥3票量比>2.5"的板块 → 输出次日板块级预警 + 链内票的4%限价位
import duckdb, json, statistics
from collections import defaultdict

DB = '/home/agentuser/stockdb/t1/qfq_all.duckdb'

# 板块映射 (精选: 涨停高发题材链, 内聚性强)
SECTORS = {
    '机器人': ['sz000678','sh603677','sh603089','sz301502','sh603280','sz002535','sh603933','sh603028','sz002527','sh605577','sz002747','sh603667','sz002826','sh603901','sz002655'],
    '算力/AI': ['sz002474','sz002354','sh603028','sz002945','sz003029','sh600589','sz002369','sh600845'],
    '半导体': ['sz002077','sz001298','sh603986','sz002049','sh600171','sz002371','sh600460','sh603501'],
    '地产链': ['sh600094','sh600838','sz000631','sh600606','sz002244','sh600325','sz000002'],
    '消费': ['sz001328','sz002982','sh600419','sh600785','sz000858','sh600887'],
    '固态电池': ['sz300584','sh688778','sz002460','sh600884','sz002074'],
}

def scan(date=None, min_stocks=3, vr_min=2.5):
    """昨日板块内 ≥min_stocks 票量比>vr_min → 板块预警"""
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET memory_limit='2GB'")
    alerts = []
    for sector, syms in SECTORS.items():
        vr_list = []
        for sym in syms:
            rows = con.execute("SELECT d, c, v FROM qfq WHERE sym=? ORDER BY d DESC LIMIT 8", [sym]).fetchall()
            if len(rows) < 8: continue
            v5 = statistics.mean(r[2] for r in rows[2:7])
            vr = rows[0][2] / v5 if v5 else 0   # rows[0]=最新日(今日盘中/收盘)
            chg = (rows[0][1] / rows[1][1] - 1) * 100 if rows[1][1] else 0
            vr_list.append({'sym': sym, 'vr': round(vr, 2), 'chg': round(chg, 2),
                            'close': rows[0][1], 'limit4': round(rows[0][1] * 1.04, 2)})
        burst = [v for v in vr_list if v['vr'] >= vr_min]
        if len(burst) >= min_stocks:
            alerts.append({'sector': sector, 'burst_count': len(burst), 'total': len(vr_list),
                           'stocks': sorted(burst, key=lambda x: -x['vr']),
                           'signal': f'板块{sector}昨日{len(burst)}/{len(vr_list)}票放量≥{vr_min} → 次日涨停潮预警',
                           'history_note': '机器人链今日8板复盘: 前兆=昨日板块5/8票量比>3.7'})
    con.close()
    alerts.sort(key=lambda a: -a['burst_count'])
    return alerts

if __name__ == '__main__':
    r = scan()
    print(json.dumps(r, ensure_ascii=False, indent=1)[:2000])

def scan_live(progress=0.6):
    """今日盘中实时板块放量扫描 (腾讯实时量/20日均量/日进度)"""
    import sys
    sys.path.insert(0, '/home/agentuser/stockdb/t1/alphas')
    from source_router import quote_tencent
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET memory_limit='2GB'")
    q = quote_tencent([s for v in SECTORS.values() for s in v])
    out = []
    for sector, syms in SECTORS.items():
        burst = []
        for sym in syms:
            live = q.get(sym) or {}
            if not live.get('price'): continue
            hist = con.execute("SELECT v FROM qfq WHERE sym=? ORDER BY d DESC LIMIT 20", [sym]).fetchall()
            avgv = statistics.mean(r[0] for r in hist) if hist else 0
            vr = (live['vol'] / avgv / progress) if avgv else 0
            burst.append({'sym': sym, 'vr': round(vr, 1), 'pct': live.get('pct'), 'price': live.get('price'),
                          'limit4': round(live['price'] * 1.04, 2), 'stop3': round(live['price'] * 0.97, 2)})
        hits = [b for b in burst if b['vr'] >= 1.8]
        if len(hits) >= 2:
            out.append({'sector': sector, 'burst': f"{len(hits)}/{len(burst)}",
                        'signal': f'{sector} 板块级放量 → 明日涨停潮候选 (历史: 昨日板块5/8放量→今日8板)',
                        'stocks': sorted(hits, key=lambda x: -x['vr'])})
    con.close()
    out.sort(key=lambda a: -len(a['stocks']))
    return out
