# module_router.py — 模块×引擎路由表 (2026-10-23)
# 用户架构定调: 每个模块调用全部引擎评分, 但本职引擎权重最高, 其他引擎辅助;
# 「推股」模块 = 中枢 + LLM 决策, 其他引擎只供数据与评分
# 用法: from module_router import run_module; run_module('主力流入分析', {'sh600900': ...})
import json, importlib

# 模块 → (本职引擎函数, 全局辅助引擎集, 辅助权重系数)
MODULES = {
    '大盘当日预测':   {'primary': ('market_forecast', 'forecast_today'),     'aux': ['overseas', 'sentiment', 'calendar'], 'aux_w': 0.4},
    '大盘复盘':       {'primary': ('market_forecast', 'review_today'),       'aux': ['sentiment', 'dual_theme'], 'aux_w': 0.4},
    '大盘明日预测':   {'primary': ('market_forecast', 'forecast_tomorrow'),  'aux': ['calendar', 'overseas'], 'aux_w': 0.4},
    '外围关联预测':   {'primary': ('overseas_alert', 'overseas_snapshot'),   'aux': ['market_forecast'], 'aux_w': 0.5},
    '异动监测':       {'primary': ('dc_engines', 'signals'),               'aux': ['overseas', 'anomaly_predict'], 'aux_w': 0.4},
    '异动分析':       {'primary': ('dc_engines', 'lhb_recent'),              'aux': ['dual_theme', 'auction'], 'aux_w': 0.4},
    '双主线':         {'primary': ('dual_theme', 'analyze'),                   'aux': ['anomaly_predict'], 'aux_w': 0.3},
    '主力流入分析':   {'primary': ('moneyflow_behavior', 'scan'),          'aux': ['moneyflow', 'acc_detect'], 'aux_w': 0.4},
    '低位吸筹':       {'primary': ('moneyflow_behavior', 'scan'),          'aux': ['pattern_low', 'moneyflow'], 'aux_w': 0.4},
    '高位出货':       {'primary': ('moneyflow_behavior', 'scan'),          'aux': ['ban_prob'], 'aux_w': 0.4},
    '超跌反弹':       {'primary': ('pattern_engines', 'oversold_rebound'),   'aux': ['moneyflow_behavior'], 'aux_w': 0.4},
    '低位股分析':     {'primary': ('pattern_engines', 'low_high_scan'),      'aux': ['lowpos_cyq', 'pead'], 'aux_w': 0.5},
    '高低位关键位':   {'primary': ('key_levels', 'key_levels'),              'aux': ['moneyflow'], 'aux_w': 0.3},
    '周期轮动':       {'primary': ('closed_loop', 'sector_rotation'),        'aux': ['market_forecast'], 'aux_w': 0.4},
    '牛熊判定':       {'primary': ('closed_loop', 'bull_bear_phase'),        'aux': ['overseas', 'sentiment'], 'aux_w': 0.3},
    '玄学辅助':       {'primary': ('news_mystic', 'mystic_daily'),           'aux': [], 'aux_w': 0.0},
    '推股':           {'primary': ('llm_council', 'council'),                'aux': ['ALL'], 'aux_w': 0.3,
                       'note': '中枢投票+LLM 陈述决策; 其他引擎仅提供数据与评分'},
}

# 模块默认参数 (免签名不匹配)
DEFAULT_ARGS = {
    '双主线': ({}, {'db_path': '/home/agentuser/stockdb/factor_matrix.duckdb'}),
    '异动监测': ({}, {}),
    '主力流入分析': ({}, {'top_n': 8}),
    '低位吸筹': ({}, {'states': ('accumulation', 'quiet_in'), 'top_n': 8}),
    '高位出货': ({}, {'states': ('trap_high', 'shakeout_high', 'dump'), 'top_n': 8}),
    '异动分析': ((None,), {}),
    '异动监测': ((None,), {}),
}
def run_module(name, *args, **kwargs):
    cfg = MODULES.get(name)
    if not cfg:
        return {'error': f'unknown module {name}', 'available': list(MODULES)}
    mod = importlib.import_module(cfg['primary'][0])
    fn = getattr(mod, cfg['primary'][1])
    if not args and not kwargs and name in DEFAULT_ARGS:
        dargs, dkw = DEFAULT_ARGS[name]
        args, kwargs = dargs, dict(dkw)
    try:
        primary = fn(*args, **kwargs)
    except TypeError:
        # 参数不匹配时尝试无参
        primary = fn()
    return {'module': name, 'primary_weight': 'max(本职)', 'result': primary,
            'aux_engines': cfg['aux'], 'aux_weight': cfg['aux_w']}

def chain_run(phase='震荡市'):
    """闭环链路: 牛熊判定 → 按市况选重点模块并行 → 汇总"""
    from closed_loop import bull_bear_phase
    pr = bull_bear_phase()
    phase_r = pr if isinstance(pr, dict) else {'phase': pr[0] if pr else '?', 'detail': pr[1:] if len(pr) > 1 else []}
    modules = ['大盘复盘', '大盘明日预测', '双主线', '异动监测', '主力流入分析', '超跌反弹']
    if 'bear' in str(phase_r.get('phase', '')):
        modules += ['高位出货', '低位股分析']
    else:
        modules += ['低位吸筹', '周期轮动']
    results = {}
    for m in modules:
        try:
            results[m] = run_module(m)
        except Exception as e:
            results[m] = {'error': str(e)[:80]}
    return {'phase': phase_r, 'chain': results,
            'note': f'{phase_r.get("phase","?")}市链路: 本职引擎最高权重+辅助{0.3-0.5}权重'}

if __name__ == '__main__':
    print(json.dumps(list(MODULES.keys()), ensure_ascii=False))
    r = run_module('大盘复盘')
    print(json.dumps(r, ensure_ascii=False, default=str)[:400])
