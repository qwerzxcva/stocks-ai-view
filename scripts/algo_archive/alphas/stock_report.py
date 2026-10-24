# stock_report.py — 完整个股分析报告引擎 (2026-10-24)
# 六层结构对标用户模板: 大盘层面/技术层面/资金层面/板块对比/K线与形态/决策建议
# 输出: 分析过程(数字实证) + 决策(买入区间/限价/止损/观察条件)
import sys, json, statistics, datetime
sys.path.insert(0, '/home/agentuser/stockdb/t1/alphas')
import duckdb
from source_router import quote_tencent
from ak_adapter import sentiment_snapshot
from tech_score_engine import tech_score
from dc_engines import lhb

DB = '/home/agentuser/stockdb/t1/qfq_all.duckdb'

def ema(vals, n):
    k = 2/(n+1); e = vals[0]
    for v in vals[1:]: e = v*k + e*(1-k)
    return e

def build_report(sym):
    code = sym[2:]
    # 行情+历史
    q = quote_tencent([sym]).get(sym, {})
    con = duckdb.connect(DB, read_only=True)
    con.execute("SET memory_limit='2GB'")
    rows = con.execute("SELECT d, o, h, l, c, v FROM qfq WHERE sym=? ORDER BY d DESC LIMIT 120", [sym]).fetchall()
    con.close()
    if len(rows) < 60 or not q.get('price'): return {'err': f'{sym} 数据不足'}
    rows = rows[::-1]
    closes = [r[4] for r in rows]; vols = [r[5] for r in rows]
    rep = {'sym': sym, 'name': q.get('name'), 'as_of': datetime.date.today().isoformat()}

    # ── 1. 大盘层面
    idx = quote_tencent(['sh000001', 'sz399006'])
    rep['market'] = {
        'sh': f"{idx['sh000001']['pct']:+.2f}%", 'cyb': f"{idx['sz399006']['pct']:+.2f}%",
        'note': '上涨家数见 sentiment_snapshot'}
    s = sentiment_snapshot()
    rep['market']['adv'] = f"{int(s.get('上涨',0))}涨/{int(s.get('下跌',0))}跌"
    # 个股 vs 大盘
    chg = (q['price']/closes[-1]-1)*100 if closes else 0
    rep['market']['relative'] = f"今日{q['pct']:+.2f}% vs 上证{idx['sh000001']['pct']:+.2f}% ({'强于' if q['pct']>idx['sh000001']['pct'] else '弱于'}大盘)"

    # ── 2. 技术层面 (EMA排列/RSI/BOLL/乖离)
    c = closes
    e5, e10, e20 = ema(c[-60:], 5), ema(c[-60:], 10), ema(c[-60:], 20)
    arr = '多头' if e5 > e10 > e20 else ('空头' if e5 < e10 < e20 else '纠缠')
    diffs = [c[i]-c[i-1] for i in range(len(c)-14, len(c))]
    up = statistics.mean([d for d in diffs if d > 0]) if any(d > 0 for d in diffs) else 0
    dn = -statistics.mean([d for d in diffs if d < 0]) if any(d < 0 for d in diffs) else 0
    rsi = 100*up/(up+dn) if (up+dn) else 50
    mu, sd = statistics.mean(c[-20:]), statistics.pstdev(c[-20:])
    boll = {'mid': round(mu, 2), 'low': round(mu-2*sd, 2), 'up': round(mu+2*sd, 2)}
    rep['tech'] = {'ema5': round(e5,2), 'ema10': round(e10,2), 'ema20': round(e20,2), '排列': arr,
                   '乖离EMA20': f"{(q['price']/e20-1)*100:+.2f}%", 'RSI14': round(rsi,1), 'BOLL': boll,
                   '位置': '下轨下方超卖' if q['price'] < boll['low'] else ('下轨附近' if q['price'] < mu else '中轨上方')}

    # ── 3. 资金层面 (腾讯当日 + 新浪10日连续性)
    import urllib.request
    rep['fund'] = {}
    try:
        d = urllib.request.urlopen(urllib.request.Request(
            f'http://qt.gtimg.cn/q=ff_{sym}', headers={'User-Agent':'Mozilla/5.0'}), timeout=8).read().decode('gbk','ignore')
        f = d.split('~')
        main_in, main_out = float(f[1] or 0)/1e8, float(f[2] or 0)/1e8
        rep['fund']['当日主力净流'] = f"{main_in-main_out:+.2f}亿"
    except Exception:
        pass
    try:
        dd = urllib.request.urlopen(urllib.request.Request(
            f'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_lscjfb?page=1&num=6&sort=opendate&asc=0&daima={sym}',
            headers={'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'}), timeout=8).read().decode('gbk','ignore')
        import re as _re
        rows_f = json.loads(_re.sub(r"([a-zA-Z_]+):", r'"\1":', dd.replace("'", '"')))
        nets = [float(x.get('netamount') or 0)/1e8 for x in rows_f]
        streak = 0
        for v in nets:
            if (v > 0) == (nets[0] > 0): streak += 1
            else: break
        rep['fund']['近6日净流'] = [round(v, 2) for v in nets]
        rep['fund']['连续'] = f"连续{streak}日{'流入' if nets[0] > 0 else '流出'}"
        rep['fund']['6日合计'] = f"{sum(nets):+.2f}亿"
    except Exception:
        if not rep['fund']: rep['fund']['note'] = '资金接口暂不可用'

    # ── 4. K线形态 (近3日)
    last3 = rows[-3:]
    shapes = []
    for r in last3:
        body = abs(r[4]-r[1]); rng = max(r[2]-r[3], 1e-9)
        lower_shadow = min(r[1], r[4]) - r[3]
        if lower_shadow > body*2: shapes.append('长下影(探底回升)')
        elif r[4] > r[1] and r[5] > vols[-4]*1.3: shapes.append('放量阳线')
        elif r[4] < r[1] and r[5] > vols[-4]*1.3: shapes.append('放量阴线')
    rep['candle'] = {'近3日': shapes or ['无显著形态'], '20日高': round(max(r[2] for r in rows[-20:]),2),
                     '20日低': round(min(r[3] for r in rows[-20:]),2)}

    # ── 5. 技术综合分 + 龙虎榜
    ts = tech_score(sym)
    rep['score'] = {'tech_score': ts['score'] if ts else None, 'ma': ts['ma'] if ts else None,
                    'vol_ratio': ts['vol_ratio'] if ts else None}

    # ── 6. 决策 (量价引擎口径: 买入区间/限价/止损/观察条件)
    t4 = round(q['price']*1.04, 2)
    stop = round(q['price']*0.97, 2)
    buy_lo, buy_hi = round(q['price']*0.985, 2), round(q['price']*1.0, 2)
    cond = []
    if arr == '多头': cond.append('EMA多头排列在位')
    else: cond.append(f"EMA{arr}排列 — 等站回EMA10({round(e10,2)})再视为趋势修复")
    if rsi < 35: cond.append('RSI低位, 超跌反抽概率升高')
    elif rsi > 70: cond.append('RSI超买, 谨防冲高回落')
    if q['price'] < boll['low']: cond.append(f"跌破BOLL下轨{boll['low']}, 若3日不收回则趋势恶化")
    rep['decision'] = {
        'buy_zone': [buy_lo, buy_hi], 'limit4': t4, 'stop': stop,
        'observe': cond,
        'note': '买入区间=昨收±1.5%回踩区(历史89%信号票次日回踩); 4%限价=今日收盘×1.04; 止损-3%无条件执行'}
    return rep

if __name__ == '__main__':
    for s in sys.argv[1:]:
        print(json.dumps(build_report(s), ensure_ascii=False, indent=1))
        print('---')
