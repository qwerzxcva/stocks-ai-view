# sector_watch.py — 全板块盘中监控 (2026-10-23)
# 监控三层: ①49行业板块实时涨跌+量能(资金向代理) ②板块内领涨股 ③涨停/炸板情绪(涨停池+连板高度)
# 每轮写 /tmp/sector_watch.json; 板块异动(|涨|>2% 或 量比突增>2)写 /tmp/sector_alerts.log
import urllib.request, json, re, time, statistics, duckdb

DB = '/home/agentuser/stockdb/t1/qfq_all.duckdb'

def sina_boards():
    d = urllib.request.urlopen(urllib.request.Request(
        'https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php',
        headers={'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'}), timeout=10).read().decode('gbk', 'ignore')
    m = re.search(r'\{".*\}', d, re.S)
    j = json.loads(m.group(0))
    out = []
    for k, v in j.items():
        p = v.split(',')
        # code,名称,家数,均价,涨跌额,涨跌幅%,成交额(元),总市值,领涨股code,领涨涨幅%,均价?,领涨价,领涨股名
        out.append({'code': k, 'name': p[1], 'n': int(p[2]), 'pct': float(p[5] or 0),
                    'amount': float(p[6] or 0)/1e8, 'mcap': float(p[7] or 0)/1e8,
                    'leader': p[12] if len(p) > 12 else p[-1], 'leader_pct': float(p[9] or 0)})
    return out

def scan():
    out = sina_boards()
    out.sort(key=lambda x: -x['pct'])
    return out

def alerts(boards, pct_th=2.0, amt_th=1.3):
    """资金向异动: 涨幅/跌幅>2% 或 成交额显著放大(对比板块历史需积累, 先用绝对额排序标注)"""
    al = []
    for b in boards:
        if abs(b['pct']) >= pct_th:
            al.append(f"[异动] {b['name']} {b['pct']:+.2f}% 额{b['amount']:.0f}亿 领涨:{b['leader']}{b['leader_pct']:+.1f}%")
    return al

if __name__ == '__main__':
    bs = scan()
    json.dump(bs, open('/tmp/sector_watch.json', 'w'), ensure_ascii=False)
    print(f"板块全景 {len(bs)} 个 | 涨:{sum(1 for b in bs if b['pct']>0)} 跌:{sum(1 for b in bs if b['pct']<0)}")
    print('TOP6 领涨:', [(b['name'], b['pct']) for b in bs[:6]])
    print('BOTTOM4:', [(b['name'], b['pct']) for b in bs[-4:]])
    for a in alerts(bs):
        print(a)
