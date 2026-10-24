# theme_lane.py — 题材异动泳道 (2026-10-24, 复刻图1/图2的三件套)
# ①题材事件流: 每轮记录板块涨幅/量能突变(板块, 时刻, 幅度, 额) ②量能簇: 全市场5分钟量vs前20均 ③涨停泳道: 按题材分组涨停时刻序列
# 输出: /tmp/theme_lane_today.json (盘中滚动) — 收盘即当日泳道文字版, LLM+次日预测消费
import urllib.request, json, re, time, datetime, statistics, duckdb

DB = '/home/agentuser/stockdb/t1/qfq_all.duckdb'
LANE = '/tmp/theme_lane_today.json'

def sina_boards():
    d = urllib.request.urlopen(urllib.request.Request(
        'https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php',
        headers={'Referer': 'https://finance.sina.com.cn', 'User-Agent': 'Mozilla/5.0'}), timeout=10).read().decode('gbk', 'ignore')
    m = re.search(r'\{".*\}', d, re.S)
    j = json.loads(m.group(0))
    out = []
    for k, v in j.items():
        p = v.split(',')
        out.append({'code': k, 'name': p[1], 'pct': float(p[5] or 0), 'amount': float(p[6] or 0)/1e8})
    return out

def detect_events(state):
    """与上一轮对比: 板块涨幅跳变>0.8pt 或 成交额跳增>30% → 事件"""
    events = []
    now = datetime.datetime.now().strftime('%H:%M')
    prev = state.get('last', {})
    for b in state['boards']:
        p = prev.get(b['code'])
        if p:
            dpct = b['pct'] - p['pct']
            damt = b['amount'] - p['amount']
            if dpct >= 0.8:
                events.append({'t': now, 'type': '板块急拉', 'sector': b['name'], 'delta': round(dpct, 2), 'pct': b['pct']})
            elif dpct <= -0.8:
                events.append({'t': now, 'type': '板块急落', 'sector': b['name'], 'delta': round(dpct, 2), 'pct': b['pct']})
            if damt >= max(3.0, p['amount'] * 0.3):
                events.append({'t': now, 'type': '板块放量', 'sector': b['name'], 'amt_add': round(damt, 1)})
    return events

def market_volume_pulse():
    """全市场量能脉冲: 上证5分钟量 vs 前12根均值 (腾讯分时)"""
    try:
        d = json.loads(urllib.request.urlopen(urllib.request.Request(
            'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001',
            headers={'User-Agent': 'Mozilla/5.0'}), timeout=10).read())
        data = d['data']['sh000001']['data']['data']
        vols = [float(l.split()[2]) for l in data if len(l.split()) >= 3]
        if len(vols) < 15: return None
        v, avg = vols[-1], statistics.mean(vols[-13:-1])
        mult = v/avg if avg else 0
        if mult > 1.6:
            return {'t': datetime.datetime.now().strftime('%H:%M'), 'type': '大盘加速放量', 'mult': round(mult, 2)}
        return None
    except Exception:
        return None

def tick():
    """每轮调用(cron 20min): 更新泳道JSON"""
    state = json.load(open(LANE)) if __import__('os').path.exists(LANE) else {'events': [], 'last': {}}
    boards = sina_boards()
    state['boards'] = boards
    state['last'] = {b['code']: {'pct': b['pct'], 'amount': b['amount']} for b in boards}
    state['events'] += detect_events(state)
    pulse = market_volume_pulse()
    if pulse: state['events'].append(pulse)
    json.dump(state, open(LANE, 'w'), ensure_ascii=False)
    return {'new_events': len(state['events']), 'latest': state['events'][-3:]}

if __name__ == '__main__':
    print(json.dumps(tick(), ensure_ascii=False))
