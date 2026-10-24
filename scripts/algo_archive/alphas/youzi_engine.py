# youzi_engine.py — 龙虎榜游资含金量引擎 (2026-10-24, 吸收 oficcejo/aiagents-stock longhubang_scoring 设计)
# 五维: 资金含金量30/净买25/卖压20/机构15/加项10
# 数据: 东财 datacenter-web 席位明细; TOP游资名单映射营业部
import json, urllib.request

DC = 'https://datacenter-web.eastmoney.com/api/data/v1/get'
TOP_YOUZI = ['章盟主', '赵老哥', '方新侠', '炒股养家', '量化基金', '宁波桑田路', '拉萨天团', '孙哥', '佛山无影脚']
FAMOUS = ['华鑫', '银泰', '国泰君安南京太平南路', '中信上海', '东方财富拉萨', '财通杭州上塘', '银河绍兴']
INST = ['机构专用', '基金', '社保', 'QFII']

def _dc(report, filt, sort, limit=100):
    url = (f'{DC}?reportName={report}&columns=ALL&filter={filt}'
           f'&pageNumber=1&pageSize={limit}&sortColumns={sort}&sortTypes=-1')
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://data.eastmoney.com/'})
    try:
        return (json.loads(urllib.request.urlopen(req, timeout=12).read()).get('result') or {}).get('data') or []
    except Exception:
        return []

_SEAT_CACHE = {}
def seat_detail(date_str, code=None):
    """席位明细: 全量拉当日(东财带 RISE_PROBABILITY_3DAY 3日上涨概率字段), 本地按票筛"""
    if date_str not in _SEAT_CACHE:
        rows = _dc('RPT_BILLBOARD_DAILYDETAILSBUY', filt=f"(TRADE_DATE='{date_str}')", sort='BUY', limit=500)
        by = {}
        for r in rows:
            by.setdefault(r.get('SECURITY_CODE'), []).append(r)
        _SEAT_CACHE[date_str] = by
    return _SEAT_CACHE[date_str].get(code, []) if code else _SEAT_CACHE[date_str]

def score_stock(date_str, code, name='', net_buy=0):
    """单票游资含金量总分"""
    rows = seat_detail(date_str, code)
    if not rows: return None
    q, f, inst, bonus = 0.0, min(25.0, max(0.0, (net_buy or 0)/1e7*2.5)), 0.0, 0.0
    buy_amts = []
    for r in rows:
        op = r.get('OPERATEDEPT_NAME') or ''
        buy = float(r.get('BUY') or 0); sell = float(r.get('SELL') or 0)
        buy_amts.append(buy)
        if any(t in op for t in TOP_YOUZI): q += 10
        elif any(t in op for t in FAMOUS): q += 5
        else: q += 1.5
        if any(t in op for t in INST): inst += 5
        bonus += 1.0  # 席位数量分
    sell_pressure = min(20.0, max(0.0, (sum(float(r.get('SELL') or 0) for r in rows) / max(sum(buy_amts), 1)) * 10))
    total = min(q, 30) + f + (20 - sell_pressure) * 0.5 + min(inst, 15) + min(bonus, 10)
    return {'code': code, 'name': name, 'youzi_q': round(min(q, 30), 1), 'netbuy_s': round(f, 1),
            'sell_p': round(sell_pressure, 1), 'inst_s': round(min(inst, 15), 1), 'total': round(total, 1),
            'seats': [{'op': (r.get('OPERATEDEPT_NAME') or '')[:20], 'buy': float(r.get('BUY') or 0)/1e8} for r in rows[:3]]}

def day_board(date_str):
    """当日龙虎榜全扫描 → 游资含金量 TOP"""
    sys_path_none = None
    import sys
    sys.path.insert(0, '/home/agentuser/stockdb/t1/alphas')
    from dc_engines import lhb
    out = []
    for r in lhb(date_str):
        if not r.get('code'): continue
        s = score_stock(date_str, r['code'], r.get('name') or '', r.get('net_buy') or 0)
        if s: out.append(s)
    out.sort(key=lambda x: -x['total'])
    return out[:15]

if __name__ == '__main__':
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else '2026-10-23'
    for s in day_board(d):
        print(f"{s['code']} {s['name']} 总分{s['total']} 游资{s['youzi_q']} 净买{s['netbuy_s']} 卖压{s['sell_p']} 机构{s['inst_s']}")
