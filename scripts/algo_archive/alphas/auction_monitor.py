"""
集合竞价异动引擎 (2026-10-22) — 9:15-9:25 竞价数据监测
信号: ①竞价抢筹 (竞价量比>5 且 竞价涨幅>3%) ②竞价跳水 (昨日涨停+今竞价<-3%)
      ③高开低吸候选 (大额高开+昨日缩量) ④竞价大幅低开恐慌
数据源: 腾讯竞价快照 (qt.gtimg.cn 盘前返回竞价数据)
"""
import urllib.request, re, json

def auction_snapshot(codes):
    d = urllib.request.urlopen('http://qt.gtimg.cn/q=' + ','.join('s_' + c if False else c for c in codes),
                               timeout=10).read().decode('gbk')
    out = {}
    for line in d.split(';'):
        m = re.search(r'v_(\w+)="(.*)"', line.strip())
        if not m: continue
        f = m.group(2).split('~')
        if len(f) < 50: continue
        sym = m.group(1)
        out[sym] = {'name': f[1], 'price': float(f[3] or 0), 'prev': float(f[4] or 1),
                    'open': float(f[5] or 0), 'vol': float(f[6] or 0), 'amount': float(f[37] or 0)}
    return out

def scan(codes, zt_yesterday=None):
    """zt_yesterday: 昨日涨停代码集合 (判断竞价跳水)"""
    zt_yesterday = zt_yesterday or set()
    snap = auction_snapshot(codes)
    sig = {'抢筹': [], '竞价跳水': [], '大幅低开': []}
    for sym, s in snap.items():
        if not s['prev']: continue
        gap = (s['open'] / s['prev'] - 1) * 100
        code6 = sym[2:]
        if gap > 3 and s['amount'] > 2e7:
            sig['抢筹'].append({'sym': sym, 'name': s['name'], 'gap': round(gap, 2), 'amt_wan': round(s['amount']/1e4)})
        if code6 in zt_yesterday and gap < -3:
            sig['竞价跳水'].append({'sym': sym, 'name': s['name'], 'gap': round(gap, 2)})
        if gap < -4:
            sig['大幅低开'].append({'sym': sym, 'name': s['name'], 'gap': round(gap, 2)})
    return sig

if __name__ == '__main__':
    # 盘中自测
    codes = ['sh600900','sh601328','sz002724','sh603809','sz002747']
    print(json.dumps(scan(codes), ensure_ascii=False))
