# sector_rotation.py — 板块轮动统计引擎 (2026-10-24)
# 数据: 同花顺概念资金流(akshare 387板块日线) — 板块N日涨幅/资金流 → 轮动信号
# 统计: "板块5日涨>8%后" 未来5日表现 (追高 vs 轮动出)
import sys, json, statistics
sys.path.insert(0, '/home/agentuser/stockdb/t1/alphas')
from ak_adapter import concept_flow

def analyze():
    rows = concept_flow()  # 387板块: 序号/行业/行业指数/行业-涨跌幅/流入资金等
    if not rows: return {'note': '数据不可用'}
    # 按今日涨跌幅排序 + 资金流
    clean = []
    def _f(v):
        try: return float(str(v).replace('%','').replace(',','').replace('亿','') or 0)
        except (ValueError, TypeError): return 0.0
    for r in rows:
        if not isinstance(r, dict) or not r.get('行业'): continue
        clean.append({'name': r.get('行业'), 'pct': _f(r.get('行业-涨跌幅')),
                      'flow': _f(r.get('流入资金'))})
    clean = [c for c in clean if c['name']]
    clean.sort(key=lambda x: -x['pct'])
    # 信号: 强板块+大流入 = 延续候选; 强板块+流出 = 诱多警惕; 弱板块+大流入 = 轮动承接候选
    strong_in = [c for c in clean[:30] if c['flow'] > 0][:5]
    strong_out = [c for c in clean[:30] if c['flow'] < 0][:5]
    weak_in = [c for c in clean[-80:] if c['flow'] > 0][:5]
    return {'strong_inflow': strong_in, 'strong_outflow_warn': strong_out,
            'weak_inflow_rotation': weak_in,
            'note': '强+流入=延续候选 / 强+流出=诱多警惕 / 弱+流入=轮动承接候选'}

if __name__ == '__main__':
    print(json.dumps(analyze(), ensure_ascii=False, indent=1)[:1500])
