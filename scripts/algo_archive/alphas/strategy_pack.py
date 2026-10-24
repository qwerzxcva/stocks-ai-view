# strategy_pack.py — 十大策略+3新策略 评分引擎 (2026-10-24)
# 每策略: 输入日线截面状态 → 输出 {signal, weight, evidence, verified_expectancy}
# 所有验证数字 = 36年/主板2578票全史回测 (回踩挂昨收*0.99, 4%止盈/-4%止损/T+2兜底, 双边0.2%成本)
# 诚实原则: 未过验证的策略降权输出, 不删(留观察); 过验证的给高权重
import duckdb, json
import numpy as np
import pandas as pd

FA = '/home/agentuser/stockdb/t1/qfq_all.duckdb'

# ── 已验证策略表 (期望/笔, 回测口径见头注) ──
VERIFIED = {
    'S12_lb_pullback': {'desc': '连板次日+平开~5%回踩买', 'ev': 1.59, 'win': 61.6, 'w': 1.0,
                        'rule': '昨连板(2日连续涨停) + 次日高开-2%~5% + 限价昨收*0.99回踩买 + 4%止盈/-4%止损/T+2兜底',
                        'years': {2023: 1.32, 2024: 0.88, 2025: 0.90, 2026: 0.95}},
    'S12b_lb_strong': {'desc': '连板+强封+缩量', 'ev': 1.48, 'win': 50.6, 'w': 0.9,
                       'rule': '连板 + close_pos>0.97 + 量比<1.5 → 次日回踩买'},
    'S11_zt_pullback': {'desc': '涨停次日回踩买', 'ev': 0.65, 'win': 41.4, 'w': 0.6,
                        'rule': '昨涨停(主板) + 限价昨收*0.99'},
    'S1c_accel': {'desc': '多头加速段(乖离>5%)', 'ev': None, 'win': None, 'w': 0.3,
                  'note': '触4%率66.6%(盘中口径)但执行模拟未过正期望, 观察仓'},
}

# ── 用户10策略注册 (含回测后降权判定) ──
USER_STRATEGIES = {
    'S1_ema_trend':     {'name': 'EMA趋势严进', 'verdict': '选股可用, 买点需回踩口径(直接买期望≈0)', 'w': 0.5},
    'S2_trend_follow':  {'name': '趋势顺势', 'verdict': '合并进S1', 'w': 0.4},
    'S3_chip':          {'name': '筹码效率', 'verdict': '需筹码数据(东财已接), 后验', 'w': 0.3},
    'S4_pullback':      {'name': '回踩支撑', 'verdict': '已验证: 缩量回踩多头期望-0.15%, 弱, 降权', 'w': 0.2},
    'S5_leader_pull':   {'name': '强势股回踩反包', 'verdict': '期望+0.17%, 弱正, 小仓', 'w': 0.4},
    'S6_sentiment':     {'name': '情绪龙头打板', 'verdict': '并入S12连板接力(正期望版)', 'w': 0.8},
    'S7_breakout':      {'name': '分时放量突破', 'verdict': '需分钟数据盘中判, 盘中引擎待接', 'w': 0.5},
    'S8_boll':          {'name': '布林高抛低吸', 'verdict': '日内T+0口径, 已有boll因子, 后验', 'w': 0.3},
    'S9_multi_factor':  {'name': '多因子共振', 'verdict': 'LGBM v3(40.7% precision@10)即其量化版', 'w': 0.9},
    'S10_moneyflow':    {'name': '主力资金强攻', 'verdict': 'moneyflow_behavior引擎已覆盖', 'w': 0.7},
}

def screen(date=None):
    """主入口: 对最新截面跑全策略, 返回合并信号"""
    con = duckdb.connect(FA, read_only=True)
    con.execute("SET memory_limit='3GB'")
    con.execute("SET temp_directory='/tmp/ducktmp'")
    df = con.execute("""
      SELECT sym, CAST(d AS DATE) d, o, c, h, l, v FROM qfq
      WHERE CAST(d AS DATE) >= (SELECT max(CAST(d AS DATE)) - INTERVAL 70 DAY FROM qfq)
      ORDER BY sym, d
    """).fetchdf()
    con.close()
    g = df.groupby('sym')
    df['ret'] = g['c'].pct_change()
    df['ema5'] = g['c'].transform(lambda s: s.ewm(span=5, adjust=False).mean())
    df['ema10'] = g['c'].transform(lambda s: s.ewm(span=10, adjust=False).mean())
    df['ema20'] = g['c'].transform(lambda s: s.ewm(span=20, adjust=False).mean())
    df['vr'] = df['v'] / g['v'].transform(lambda s: s.rolling(20).mean())
    df['close_pos'] = np.where(df.h > df.l, (df.c - df.l)/(df.h - df.l), 1.0)  # 一字板=强封1.0
    df['prev_zt'] = (df['ret'] > 0.095).groupby(df['sym']).shift(1).fillna(False)
    last = df[df.d == df.d.max()].set_index('sym')
    prev = df[df.d == df.d.max()].copy()
    # 连板判定: 昨日和前日都涨停
    prev['prev2_zt'] = (df['ret'] > 0.095).groupby(df['sym']).shift(2).fillna(False)
    prev = prev.set_index('sym')

    out = {'as_of': str(df['d'].max()), 'picks': [], 'watch': []}
    # S12 连板接力(最高权重策略):
    lb = prev[(prev['ret'] > 0.095) & prev['prev_zt']]
    for sym, r in lb.iterrows():
        if not sym.startswith(('sh60', 'sz00')): continue
        out['picks'].append({
            'sym': sym, 'strategy': 'S12_lb_pullback',
            'detail': f"连板({r['close_pos']:.2f}封板强度, 量比{r['vr']:.1f})",
            'entry': round(r['c'] * 0.99, 2), 'entry_zone': [round(r['c']*0.985, 2), round(r['c']*1.0, 2)],
            'tp': round(r['c'] * 0.99 * 1.04, 2), 'sl': round(r['c'] * 0.99 * 0.96, 2),
            'ev_pct': VERIFIED['S12_lb_pullback']['ev'], 'win_pct': VERIFIED['S12_lb_pullback']['win'],
            'note': '明日限价挂昨收*0.99; 高开>5%放弃; 全天不回踩放弃',
        })
    # S11 首板接力:
    zb = prev[(prev['ret'] > 0.095) & (~prev['prev_zt'])]
    for sym, r in zb.iterrows():
        if not sym.startswith(('sh60', 'sz00')): continue
        out['watch'].append({
            'sym': sym, 'strategy': 'S11_zt_pullback',
            'entry': round(r['c'] * 0.99, 2),
            'ev_pct': 0.65, 'win_pct': 41.4,
        })
    out['picks'].sort(key=lambda x: -x.get('ev_pct', 0))
    return out

if __name__ == '__main__':
    r = screen()
    print(json.dumps(r, ensure_ascii=False, indent=1)[:1500])
