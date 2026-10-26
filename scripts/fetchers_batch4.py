"""
fetchers_batch4.py — 新数据源批次 4 (2026-10-27)
全部本机实测通过 (2026-10-26 深夜探测), 补充 sources_registry 已有 67 域名之外的入口。
定位: 每类数据新增独立站点入口 — 即使底层行情与既有源同源 (搜狐/新浪/腾讯),
接口域名/字段结构不同, 视为独立容灾节点; 软件端 fetchers_extra.py 继续为主通道。

实测矩阵 (2026-10-26 22:00-00:25 CST):
  OK  sohu_hisHq        搜狐历史日K (指数/个股, GBK, [date,open,chg,pct,low,high,vol,amt,turnover])
  OK  sohu_zs_index     搜狐指数 (zs_000001 上证 / zs_399001 深成)
  OK  sina_5m_kline     新浪 5/15/30/60 分钟K (jsonp var 前缀需剥离)
  OK  sina_724_zhibo    新浪 7x24 直播 (zhibo_id=152, 财经频道)
  OK  em_np_fastnews    东财 7x24 快讯 (np-listapi)
  OK  em_np_colnews     东财栏目新闻 (column=350 宏观)
  OK  yicai_html        第一财经 (返回 HTML 页, 需解析; API 有 JS 门)
  OK  sina_global_idx   新浪全球指数 int_dji/nasdaq/sp500/hangseng/nikkei
  OK  sina_hk_rt        新浪港股实时 rt_hk00700 (腾讯 389.0 实测)
  OK  sina_hk_index     新浪港股指数 rt_hkHSI/HSCEI
  OK  sina_us_rt        新浪美股实时 gb_aapl
  OK  sina_fx           新浪外汇 fx_susdcny
  OK  sina_commodity    新浪商品期货连续 nf_RB0/M0/CU0 (主力 0 后缀)
  OK  sina_sector       新浪行业板块实时 (newSinaHy.php, GBK)
  OK  sina_60m_kline    新浪 60 分钟K (scale=60)
  OK  tencent_hk_kline  腾讯港股日K hkfqkline (hk00700)
  OK  tencent_us_kline  腾讯美股日K usfqkline (usAAPL.OQ)
  OK  tencent_week_k    腾讯周K fqkline week
  OK  tencent_minute    腾讯分时 minute/query
  OK  tencent_zxg       腾讯自选股 newfqkline (proxy.finance.qq.com)
  OK  ths_day_last      同花顺日K last.js (140 根, quotebridge_ 前缀)
  OK  szse_margin       深交所两融总量 (1837_xxpl)
  OK  em_margin_total   东财两融历史 RPTA_RZRQ_LSHJ
  OK  em_north          东财北向 RPT_MUTUAL_DEAL_HISTORY (7797 页)
  OK  em_ztpool         东财涨停池 push2ex (55 只 20261026)
  OK  jd_quote          京东金融实时 latestPrice
  FAIL xueqiu_hot(405)  kaipanla(DNS)  yahoo(429)  stooq(超时)  gelonghui(404)
  FAIL xuangubao(超时)  ifeng(DNS)  gushitong(403)  cnstock(403)  caixin(400)
"""
import json, re, urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0'}
SINA_H = {'Referer': 'https://finance.sina.com.cn'}


def _http(url, headers=None, timeout=10, enc='utf-8'):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout).read().decode(enc, 'ignore')


# ── 搜狐 (独立站点, hisHq 口径) ──
def sohu_daily(code, start, end, index=False):
    """code=600519 或 000001; index=True 时 code=000001/399001 加 zs_ 前缀"""
    p = f'zs_{code}' if index else f'cn_{code}'
    d = json.loads(_http(f'https://q.stock.sohu.com/hisHq?code={p}&start={start}&end={end}&stat=1&order=D&period=d', enc='gbk'))
    out = []
    def _f(x):
        x = str(x).strip().rstrip('%')
        try:
            return float(x)
        except ValueError:
            return 0.0
    for r in d[0]['hq']:
        # 实测字段序: [date, open, close, chg, pct, low, high, vol(手), amt(万), turnover, ?]
        out.append({'date': r[0], 'open': _f(r[1]), 'close': _f(r[2]),
                    'low': _f(r[5]), 'high': _f(r[6]), 'pct': _f(r[4]),
                    'vol': _f(r[7]), 'amt': _f(r[8]), 'turnover': _f(r[9])})
    return out


def sohu_daily_cn(code, start, end):
    return sohu_daily(code, start, end, index=False)


def sohu_daily_zs(code, start, end):
    return sohu_daily(code, start, end, index=True)


# ── 新浪分钟K (scale=5/15/30/60) ──
def sina_minute(sym, scale=5, n=48):
    """sym=sh600519; jsonp var 前缀剥离"""
    raw = _http(f'https://quotes.sina.cn/cn/api/jsonp_v2.php/var/CN_MarketDataService.getKLineData?symbol={sym}&scale={scale}&ma=no&datalen={n}')
    m = re.search(r'\[.*\]', raw, re.S)
    return json.loads(m.group(0)) if m else []


# ── 新浪 7x24 直播 (财经频道) ──
def sina_724(page=1, size=20):
    d = json.loads(_http(f'https://zhibo.sina.com.cn/api/zhibo/feed?page={page}&page_size={size}&zhibo_id=152&tag_id=0'))
    items = d['result']['data']['feed']['list']
    return [{'time': i.get('create_time'), 'text': i.get('rich_text', '')} for i in items]


# ── 东财 7x24 快讯 / 栏目新闻 ──
def em_fastnews(page_size=10):
    d = json.loads(_http(f'https://np-listapi.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize={page_size}&req_trace=1'))
    return d.get('data', {}).get('fastNewsList', [])


def em_colnews(column=350, size=10):
    d = json.loads(_http(f'https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web_news_col&column={column}&order=1&needInteractData=0&page_index=1&page_size={size}&req_trace=1&fields=code,showTime,title,mediaName,summary,url'))
    return d.get('data', {}).get('list', [])


# ── 新浪全球/港/美/外汇/商品/板块 ──
def sina_global_idx():
    raw = _http('https://hq.sinajs.cn/list=int_dji,int_nasdaq,int_sp500,int_hangseng,int_nikkei', SINA_H, enc='gbk')
    out = {}
    for m in re.finditer(r'hq_str_(\w+)="([^"]*)"', raw):
        f = m.group(2).split(',')
        if len(f) >= 4:
            out[m.group(1).replace('int_', '')] = {'name': f[0], 'last': float(f[1]), 'chg': float(f[2]), 'pct': float(f[3])}
    return out


def sina_hk_rt(codes=('00700', '09988', '03690')):
    raw = _http('https://hq.sinajs.cn/list=' + ','.join(f'rt_hk{c}' for c in codes), SINA_H, enc='gbk')
    out = {}
    for m in re.finditer(r'hq_str_rt_hk(\w+)="([^"]*)"', raw):
        f = m.group(2).split(',')
        if len(f) > 10:
            out[m.group(1)] = {'name_en': f[0], 'name': f[1], 'last': float(f[3]), 'prev': float(f[4] or 0),
                               'high': float(f[5] or 0), 'low': float(f[6] or 0)}
    return out


def sina_hk_index():
    raw = _http('https://hq.sinajs.cn/list=rt_hkHSI,rt_hkHSCEI', SINA_H, enc='gbk')
    out = {}
    for m in re.finditer(r'hq_str_rt_hk(\w+)="([^"]*)"', raw):
        f = m.group(2).split(',')
        if len(f) > 9:
            out[m.group(1)] = {'name': f[0], 'last': float(f[6] or 0), 'chg': float(f[7] or 0), 'pct': float(f[8] or 0)}
    return out


def sina_us_rt(codes=('aapl', 'nvda', 'tsla')):
    raw = _http('https://hq.sinajs.cn/list=' + ','.join(f'gb_{c}' for c in codes), SINA_H, enc='gbk')
    out = {}
    for m in re.finditer(r'hq_str_gb_(\w+)="([^"]*)"', raw):
        f = m.group(2).split(',')
        if len(f) > 26:
            out[m.group(1).upper()] = {'name': f[0], 'last': float(f[1] or 0), 'pct': float(f[2] or 0)}
    return out


def sina_fx(pairs=('fx_susdcny', 'fx_seurusd', 'fx_susdjpy')):
    raw = _http('https://hq.sinajs.cn/list=' + ','.join(pairs), SINA_H, enc='gbk')
    out = {}
    for m in re.finditer(r'hq_str_(\w+)="([^"]*)"', raw):
        f = m.group(2).split(',')
        if len(f) > 4:
            out[m.group(1)] = {'last': float(f[1] or 0)}
    return out


def sina_commodity(syms=('nf_RB0', 'nf_M0', 'nf_CU0', 'nf_AU0', 'nf_SC0')):
    raw = _http('https://hq.sinajs.cn/list=' + ','.join(syms), SINA_H, enc='gbk')
    out = {}
    for m in re.finditer(r'hq_str_(\w+)="([^"]*)"', raw):
        f = m.group(2).split(',')
        if len(f) > 8 and f[0]:
            out[m.group(1)] = {'name': f[0], 'last': float(f[8] or 0), 'pct': float(f[9] or 0) if f[9] else 0}
    return out


def sina_sector():
    """新浪行业板块实时 (49 行业): {code,name,n,均价,涨跌额,涨跌幅,现量,总量,领涨sym,价,涨幅,领涨名}"""
    raw = _http('https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php', enc='gbk')
    body = raw.split('=', 1)[1].strip().rstrip(';')
    d = json.loads(body)
    out = {}
    for code, v in d.items():
        f = v.split(',')
        if len(f) >= 13:
            try:
                out[code] = {'name': f[1], 'n': int(f[2]), 'avg_price': float(f[3]),
                             'pct': float(f[5]), 'amt': float(f[7] or 0),
                             'leader_sym': f[8], 'leader_pct': float(f[10] or 0),
                             'leader_name': f[12]}
            except (ValueError, IndexError):
                continue
    return out


# ── 腾讯补充周期/市场 ──
def tencent_kline_market(market_sym, n=120, period='day'):
    """market_sym: sh600519 / hk00700 / usAAPL.OQ; period: day/week/month"""
    if market_sym.startswith('hk'):
        ep = 'hkfqkline'
    elif market_sym.startswith('us'):
        ep = 'usfqkline'
    else:
        ep = 'fqkline'
    d = json.loads(_http(f'https://web.ifzq.gtimg.cn/appstock/app/{ep}/get?param={market_sym},{period},,,{n},qfq'))
    k = d['data'][market_sym].get(f'qfq{period}') or d['data'][market_sym].get(period) or []
    return [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in k]


def tencent_zxg(sym, n=3):
    """腾讯自选股 proxy 域 K线"""
    d = json.loads(_http(f'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={sym},day,,,{n},qfq'))
    k = d['data'][sym].get('qfqday') or []
    return [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in k]


def tencent_minute(sym):
    d = json.loads(_http(f'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}'))
    data = d['data'][sym]['data']['data']
    return [(r.split()[0], float(r.split()[1])) for r in data]


# ── 同花顺 ──
def ths_day_last(code='600519'):
    """quotebridge_ 前缀 JSONP, 140 根日K"""
    raw = _http(f'https://d.10jqka.com.cn/v6/line/hs_{code}/01/last.js')
    m = re.search(r'\((\{.*\})\)', raw, re.S)
    return json.loads(m.group(1)) if m else {}


# ── 交易所 ──
def szse_margin():
    d = json.loads(_http('https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON&CATALOGID=1837_xxpl&TABKEY=tab1&random=0.1'))
    return d[0].get('data', []) if isinstance(d, list) and d else []


def em_margin_total(pagesize=5):
    d = json.loads(_http(f'https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPTA_RZRQ_LSHJ&columns=ALL&pageSize={pagesize}&pageNumber=1&sortColumns=DIM_DATE&sortTypes=-1'))
    return (d.get('result') or {}).get('data', [])


def em_north(pagesize=5, mutual_type='002'):
    """002=北向沪股通 003=北向深股通 001=南向港股通"""
    d = json.loads(_http(f'https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_MUTUAL_DEAL_HISTORY&columns=ALL&pageSize={pagesize}&pageNumber=1&MUTUAL_TYPE={mutual_type}&sortColumns=TRADE_DATE&sortTypes=-1'))
    return (d.get('result') or {}).get('data', [])


def em_ztpool(date='20261026'):
    d = json.loads(_http(f'https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt&Pageindex=0&pagesize=200&sort=fbt%3Aasc&date={date}'))
    return (d.get('data') or {}).get('pool', [])


# ── 京东金融 ──
def jd_quote(code='600519', market='0'):
    d = json.loads(_http(f'https://ms.jr.jd.com/gw/generic/hj/h5/m/latestPrice?param={market}_{code}'))
    return d


# ── 自检 ──
if __name__ == '__main__':
    checks = [
        ('sohu_daily_cn', lambda: len(sohu_daily_cn('600519', '20261020', '20261026'))),
        ('sohu_daily_zs', lambda: len(sohu_daily_zs('000001', '20261023', '20261026'))),
        ('sina_minute', lambda: len(sina_minute('sh600519', 5, 10))),
        ('sina_724', lambda: len(sina_724(1, 5))),
        ('em_fastnews', lambda: len(em_fastnews(5))),
        ('em_colnews', lambda: len(em_colnews(350, 5))),
        ('sina_global_idx', lambda: len(sina_global_idx())),
        ('sina_hk_rt', lambda: len(sina_hk_rt())),
        ('sina_hk_index', lambda: len(sina_hk_index())),
        ('sina_us_rt', lambda: len(sina_us_rt())),
        ('sina_fx', lambda: len(sina_fx())),
        ('sina_commodity', lambda: len(sina_commodity())),
        ('sina_sector', lambda: len(sina_sector())),
        ('tencent_hk', lambda: len(tencent_kline_market('hk00700', 3))),
        ('tencent_us', lambda: len(tencent_kline_market('usAAPL.OQ', 3))),
        ('tencent_week', lambda: len(tencent_kline_market('sh600519', 3, 'week'))),
        ('tencent_zxg', lambda: len(tencent_zxg('sh600519', 3))),
        ('tencent_minute', lambda: len(tencent_minute('sh600519'))),
        ('ths_day_last', lambda: len(ths_day_last().get('data', ''))),
        ('szse_margin', lambda: len(szse_margin())),
        ('em_margin_total', lambda: len(em_margin_total(3))),
        ('em_north', lambda: len(em_north(3))),
        ('em_ztpool', lambda: len(em_ztpool('20261026'))),
        ('jd_quote', lambda: len(json.dumps(jd_quote()))),
    ]
    ok = 0
    for name, fn in checks:
        try:
            r = fn()
            print(f'OK   {name}: {r}')
            ok += 1
        except Exception as e:
            print(f'FAIL {name}: {str(e)[:60]}')
    print(f'\n{ok}/{len(checks)} sources functional')
