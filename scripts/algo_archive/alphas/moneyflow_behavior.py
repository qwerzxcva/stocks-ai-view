# moneyflow_behavior.py — 主力行为五态引擎 (2026-10-23)
# 模块: 微涨大流入/大涨大流入/微跌大流出/低位吸筹/低位洗盘/高位套人/高位洗盘 + 分析预测
# 数据: source_router.fundflow_sina (日线级净流入) + qfq_all 日K
# 权重: 本模块对「主力流入/流出分析预测」类模块为最高权重 (模块本职), 其他引擎辅助
import sys, json, statistics
sys.path.insert(0, '/home/agentuser/stockdb/t1/alphas')

def _daily(sym):
    """(closes, pcts, dates) 日序列, Router.kline 三源路由"""
    from source_router import Router
    try:
        rows, _src = Router().kline(sym, 60)
        if not rows or len(rows) < 30:
            return [], [], []
        closes = [r[2] for r in rows]
        pcts = []
        for i in range(1, len(closes)):
            pcts.append((closes[i] / closes[i-1] - 1) * 100 if closes[i-1] else 0.0)
        pcts.insert(0, 0.0)
        return closes, pcts, [r[0] for r in rows]
    except Exception:
        return [], [], []

def _flow(sym, days=20):
    from source_router import fundflow_sina
    try:
        return fundflow_sina(sym, days=days)
    except Exception:
        return []

def behavior_state(sym):
    """返回 {state, confidence, evidence, prediction} — 五态判定"""
    closes, pcts, dates = _daily(sym)
    fl = _flow(sym, 20)
    if len(closes) < 30 or len(fl) < 10:
        return {'sym': sym, 'state': 'insufficient', 'confidence': 0.0, 'evidence': [], 'prediction': None}
    # 对齐: 资金流与收盘按日期 merge
    fmap = {r['date'][:10].replace('-',''): r['net'] for r in fl}
    dmap = {d.replace('-',''): (c, p) for d, c, p in zip(dates, closes, pcts)}
    common = sorted(set(fmap) & set(dmap))[-20:]
    if len(common) < 8:
        return {'sym': sym, 'state': 'insufficient', 'confidence': 0.0, 'evidence': [], 'prediction': None}
    nets = [fmap[d] for d in common]
    pcts_al = [dmap[d][1] for d in common]
    closes_al = [dmap[d][0] for d in common]
    # 位置: 一年分位
    lo, hi = min(closes), max(closes)
    pos = (closes_al[-1] - lo) / (hi - lo) * 100 if hi > lo else 50.0
    net20 = sum(nets)
    net_pos_days = sum(1 for n in nets if n > 0)
    avg_pct = statistics.mean(pcts_al)
    flow_price_div = net_pos_days / len(nets) - (sum(1 for p in pcts_al if p > 0) / len(pcts_al))
    ev = []
    state, conf, pred = 'neutral', 0.3, None
    if net20 > 0 and avg_pct < 1.0 and pos < 40:
        state, conf = 'accumulation', min(0.9, 0.5 + flow_price_div)
        ev.append(f'低位(分位{pos:.0f})+累计净流入{net20/1e8:.2f}亿+价微动({avg_pct:.2f}%)=吸筹')
        pred = '吸筹末期大概率拉升, 20日看多'
    elif pos < 45 and net20 < 0 and avg_pct > -2.0 and flow_price_div > 0.1:
        state, conf = 'shakeout_low', 0.7
        ev.append(f'低位流出但跌幅温和({avg_pct:.2f}%)且流出日<跌日=洗盘特征')
        pred = '洗盘概率大, 回调即是买点观察'
    elif pos > 80 and net20 > 0 and avg_pct > 3.0:
        state, conf = 'trap_high', 0.65
        ev.append(f'高位(分位{pos:.0f})+大涨({avg_pct:.2f}%)+大流入=诱多/接力特征')
        pred = '高位套人风险, 追高慎入'
    elif pos > 75 and net20 < 0 and abs(avg_pct) < 1.5:
        state, conf = 'shakeout_high', 0.6
        ev.append(f'高位阴跌微跌({avg_pct:.2f}%)但主力持续流出{net20/1e8:.2f}亿=出货')
        pred = '高位洗盘/派发, 20日看空'
    elif net20 > 0 and avg_pct < 1.0:
        state, conf = 'quiet_in', 0.55
        ev.append(f'微涨+大流入{net20/1e8:.2f}亿(流入日{net_pos_days}/20)')
        pred = '主力潜伏, 跟随看多'
    elif net20 < 0 and avg_pct < -1.0:
        state, conf = 'dump', 0.6
        ev.append(f'大跌+大流出{net20/1e8:.2f}亿')
        pred = '主力出逃, 回避'
    else:
        ev.append(f'中性: 净{net20/1e8:.2f}亿, 均幅{avg_pct:.2f}%, 分位{pos:.0f}')
    return {'sym': sym, 'state': state, 'confidence': round(min(conf, 0.95), 2),
            'evidence': ev, 'prediction': pred,
            'metrics': {'pos': round(pos,1), 'net20': round(net20/1e8,2),
                        'net_pos_days': net_pos_days, 'avg_pct': round(avg_pct,2),
                        'flow_price_div': round(flow_price_div,3)}}

def scan(states=('accumulation','quiet_in'), top_n=15, universe=None):
    """全池扫描某类行为状态"""
    if universe is None:
        con = __import__('duckdb').connect('/home/agentuser/stockdb/factor_matrix.duckdb', read_only=True)
        con.execute("SET memory_limit='2GB'")
        universe = [r[0] for r in con.execute(
            "SELECT DISTINCT symbol FROM mf_factors WHERE as_of >= '2026-10-01' LIMIT 500").fetchall()]
    out = []
    for sym in universe:
        r = behavior_state(sym)
        if r['state'] in states:
            out.append(r)
        if len(out) >= top_n * 3:
            break
    out.sort(key=lambda x: -x['confidence'])
    return out[:top_n]

if __name__ == '__main__':
    for sym in ['sh600900', 'sh601328', 'sz002594']:
        print(json.dumps(behavior_state(sym), ensure_ascii=False))
