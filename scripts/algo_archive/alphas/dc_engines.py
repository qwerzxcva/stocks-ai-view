"""
东财 datacenter 引擎群 (2026-10-22) — datacenter-web.eastmoney.com 不在 403 风控域
覆盖: 龙虎榜 / 限售解禁 / 大宗交易 / (可扩展: 股东增减持/融资融券)
"""
import json, urllib.request, time

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0',
      'Referer': 'https://data.eastmoney.com/'}

def dc_get(report, columns='ALL', filt=None, sort=None, pagesize=50, pageno=1):
    url = (f'https://datacenter-web.eastmoney.com/api/data/v1/get?reportName={report}'
           f'&columns={columns}&pageSize={pagesize}&pageNumber={pageno}')
    if filt: url += f'&filter={filt}'
    if sort: url += f'&sortColumns={sort}&sortTypes=-1'
    d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read())
    return ((d.get('result') or {}).get('data')) or []

def lhb(date_str):
    """龙虎榜: 当日上榜个股+买卖席位净额"""
    rows = dc_get('RPT_DAILYBILLBOARD_DETAILSNEW', filt=f'(TRADE_DATE=\'{date_str}\')', sort='BILLBOARD_NET_AMT')
    out = []
    for r in rows:
        out.append({'code': r.get('SECURITY_CODE'), 'name': r.get('SECURITY_NAME_ABBR'),
                    'reason': r.get('EXPLANATION'), 'net_buy': r.get('BILLBOARD_NET_AMT'),
                    'buy': r.get('BILLBOARD_BUY_AMT'), 'chg': r.get('CHANGE_RATE')})
    return out

def jiejie(days_ahead=14):
    """未来解禁: 解禁市值/占流通比 — 大解禁前规避"""
    rows = dc_get('RPT_LIFT_STAGE', sort='FREE_DATE', pagesize=100)
    out = []
    for r in rows:
        fd = (r.get('FREE_DATE') or '')[:10]
        if not fd: continue
        out.append({'code': r.get('SECURITY_CODE'), 'name': r.get('SECURITY_NAME_ABBR'),
                    'date': fd, 'shares_wan': r.get('LIFT_NUM'), 'mv': r.get('LIFT_MARKET_VALUE'),
                    'pct_of_float': r.get('TOTAL_RATIO')})
    return out

def dazong(date_str):
    """大宗交易: 折价率+买方营业部"""
    rows = dc_get('RPT_DATA_BLOCKTRADE', filt=f'(TRADE_DATE=\'{date_str}\')', sort='DEAL_AMT')
    return [{'code': r.get('SECURITY_CODE'), 'name': r.get('SECURITY_NAME_ABBR'),
             'price': r.get('DEAL_PRICE'), 'vol_wan': r.get('DEAL_VOL'),
             'amt': r.get('DEAL_AMT'), 'discount': r.get('DISCOUNT_RATE'),
             'buyer': r.get('BUYER_NAME'), 'seller': r.get('SELLER_NAME')} for r in rows]

def signals(date_str):
    """交易信号合成: 龙虎榜净买+大宗折价+解禁预警"""
    sig = {'lhb_hot': [], 'block_discount': [], 'jiejie_warn': []}
    try:
        l = lhb(date_str)
        sig['lhb_hot'] = [x for x in l if (x['net_buy'] or 0) > 5e7][:10]
    except Exception as e: sig['lhb_err'] = str(e)[:40]
    try:
        b = dazong(date_str)
        sig['block_discount'] = [x for x in b if (x['discount'] or 0) < -8][:10]
    except Exception as e: sig['block_err'] = str(e)[:40]
    try:
        j = jiejie()
        sig['jiejie_warn'] = [x for x in j if (x.get('pct_of_float') or 0) > 5][:10]
    except Exception as e: sig['jiejie_err'] = str(e)[:40]
    return sig

if __name__ == '__main__':
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else '2026-10-22'
    s = signals(d)
    print(json.dumps(s, ensure_ascii=False, indent=1, default=str)[:1500])
