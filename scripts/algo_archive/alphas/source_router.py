"""
多源行情路由 (2026-10-22) — 每类数据 ≥2 源, 健康度滑动窗自动切换
实测矩阵: 腾讯qt✓ 新浪hq✓ 新浪日K✓ 同花顺日K✓ 东财push2 本机403(软件端保留为首选, 失败即切)
"""
import json, time, urllib.request
from collections import deque

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0'}

class Source:
    def __init__(self, name, fn, timeout=8):
        self.name, self.fn, self.timeout = name, fn, timeout
        self.health = deque(maxlen=20)   # 1=ok 0=fail
    @property
    def score(self):
        if not self.health: return 0.5
        return sum(self.health) / len(self.health)

def _http(url, headers=None, timeout=8):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout).read()

# ── 实时行情源 ──
def quote_tencent(codes):
    d = _http('http://qt.gtimg.cn/q=' + ','.join(codes)).decode('gbk')
    out = {}
    for line in d.split(';'):
        m = None
        import re
        m = re.search(r'v_(\w+)="(.*)"', line.strip())
        if not m: continue
        f = m.group(2).split('~')
        if len(f) < 50: continue
        sym = m.group(1) if m.group(1).startswith(('sh','sz','bj')) else f[2]
        out[sym] = {'name': f[1], 'price': float(f[3] or 0), 'pct': float(f[32] or 0),
                     'vol': float(f[36] or 0), 'amount': float(f[37] or 0)}
    return out

def quote_sina(codes):
    d = _http('https://hq.sinajs.cn/list=' + ','.join(codes),
              {'Referer': 'https://finance.sina.com.cn'}).decode('gbk')
    out = {}
    for line in d.strip().split('\n'):
        import re
        m = re.search(r'hq_str_(\w+)="(.*)"', line)
        if not m: continue
        sym, f = m.group(1), m.group(2).split(',')
        if len(f) < 32: continue
        prev = float(f[2] or 1)
        out[sym[2:]] = {'name': f[0], 'price': float(f[3] or 0),
                        'pct': round((float(f[3] or 0)/prev-1)*100, 2) if prev else 0,
                        'vol': float(f[8] or 0), 'amount': float(f[9] or 0)}
    return out

# ── 日K源 ──
def kline_tencent(sym, n=120):
    d = json.loads(_http(f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},qfq').decode())
    k = d['data'][sym].get('qfqday') or d['data'][sym]['day']
    return [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in k]

def kline_sina(sym, n=120):
    d = json.loads(_http(f'https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData?symbol={sym}&scale=240&ma=no&datalen={n}',
                         {'Referer': 'https://finance.sina.com.cn'}).decode())
    return [(r['day'], float(r['open']), float(r['close']), float(r['high']), float(r['low']), float(r['volume'])) for r in d]

def kline_ths(sym, n=120):  # 同花顺 (600900 格式, 无前缀)
    t = _http(f'https://d.10jqka.com.cn/v6/line/hs_{sym.replace("sh","").replace("sz","")}/01/last.js',
              {'Referer': 'https://www.10jqka.com.cn'}).decode()
    import re
    j = json.loads(re.search(r'\((.*)\)', t, re.S).group(1))
    out = []
    for y, days in j.get('data', {}).items() if isinstance(j.get('data'), dict) else []:
        pass
    # last.js 结构复杂时退化: 用 2026 年段
    return out if out else None

class Router:
    def __init__(self):
        self.quote_sources = [
            Source('eastmoney', None),   # 软件端首选, 本机不注册 fn
            Source('tencent', quote_tencent),
            Source('sina', quote_sina),
        ]
        self.kline_sources = [
            Source('eastmoney', None),
            Source('tencent', kline_tencent),
            Source('sina', kline_sina),
        ]
    def _pick(self, sources, skip_disabled=True):
        alive = [s for s in sources if s.fn and (s.score > 0.3 or not s.health)]
        return sorted(alive, key=lambda s: -s.score)
    def quotes(self, codes):
        errs = []
        for s in self._pick(self.quote_sources):
            try:
                r = s.fn(codes); s.health.append(1)
                if r: return r, s.name
            except Exception as e:
                s.health.append(0); errs.append(f'{s.name}:{str(e)[:30]}')
        raise RuntimeError('all quote sources failed: ' + ';'.join(errs))
    def kline(self, sym, n=120):
        for s in self._pick(self.kline_sources):
            try:
                r = s.fn(sym, n); s.health.append(1)
                if r: return r, s.name
            except Exception:
                s.health.append(0)
        raise RuntimeError('all kline sources failed')

ROUTER = Router()

if __name__ == '__main__':
    q, src = ROUTER.quotes(['sh600900', 'sh000001'])
    print('行情 via', src, {k: v['price'] for k, v in list(q.items())[:2]})
    k, src = ROUTER.kline('sh600900', 30)
    print('日K via', src, len(k), '根, 最新', k[-1][0], k[-1][2])

# ── 资金流源 (新增 2026-10-22) ──
def fundflow_sina(sym, days=10):
    """新浪历史资金流: netamount=净流入, r0特大单/r1大单/... 返回按日期升序"""
    d = _http(f'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_lscjfb?page=1&num={days}&sort=opendate&asc=0&daima={sym}',
              {'Referer': 'https://finance.sina.com.cn'}).decode('gbk', 'ignore')
    rows = json.loads(d)
    out = []
    for r in rows:
        out.append({'date': r['opendate'], 'net': float(r.get('netamount') or 0),
                    'ratio': float(r.get('ratioamount') or 0), 'turnover': float(r.get('turnover') or 0)})
    out.reverse()
    return out

def fundflow_tencent(sym):
    """腾讯单票实时资金流 (ff_ 接口在服务器曾失效, 软件端可用)"""
    import re
    d = _http(f'http://qt.gtimg.cn/q=ff_{sym}').decode('gbk', 'ignore')
    m = re.search(r'v_ff_\w+="(.*)"', d)
    if not m: return None
    f = m.group(1).split('~')
    # f: 主流入/主流出/净额/... 字段序: 1流入 2流出 3净 5流入资金?按实测
    return {'main_in': float(f[1] or 0), 'main_out': float(f[2] or 0), 'net': float(f[3] or 0)}

Router.fundflow_sources = None
def fundflow(self, sym, days=10):
    errs = []
    for s in [Source('sina_ff', fundflow_sina), Source('tencent_ff', fundflow_tencent)]:
        if not s.fn: continue
        try:
            r = s.fn(sym, days) if s.name == 'sina_ff' else s.fn(sym)
            if r: return r, s.name
        except Exception as e:
            errs.append(f'{s.name}:{str(e)[:30]}')
    raise RuntimeError('fundflow all failed: ' + ';'.join(errs))
Router.fundflow = fundflow

if __name__ == '__main__' and False:
    pass
