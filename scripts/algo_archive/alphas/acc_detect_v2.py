# acc_detect v2: 吸筹/派发检测 (融合 gitcode flow_divergence 的斜率法 + 波动压缩)
# 原理: 价格斜率 vs 资金流斜率 背离 + 持续性过滤 + TR波动压缩 (来源: dufuwuxin/stock-system scripts/flow_divergence.py)
def slope(series, i, span=5):
    """i 点前 span 期线性斜率"""
    n = min(span, i + 1)
    xs = list(range(n)); ys = [series[i - n + 1 + k] for k in range(n)]
    mx, my = sum(xs)/n, sum(ys)/n
    num = sum((xs[k]-mx)*(ys[k]-my) for k in range(n))
    den = sum((x-mx)**2 for x in xs) or 1
    return num/den

def classify(closes, flows, trs):
    """closes/flows: 最近20日序列; trs: true range 序列
    返回 (signal, score)"""
    if len(closes) < 20 or len(flows) < 20: return ("数据不足", 0)
    ap = slope(closes, len(closes)-1)
    af = slope(flows, len(flows)-1)
    close = closes[-1]
    ap_pct = ap/close*100 if close else 0
    scale = max(1e5, sum(abs(v) for v in flows)/len(flows))
    af_norm = af/scale
    persist = sum(v > 0 for v in flows[-10:])/10
    side = 1 if (ap_pct < -0.02 and af_norm > 0.05) else (-1 if (ap_pct > 0.02 and af_norm < -0.05) else 0)
    diverge = side if (side > 0 and persist >= 0.6) or (side < 0 and persist <= 0.4) else 0
    v20 = sum(trs[-20:])/max(1,len(trs[-20:])); v60 = sum(trs[-60:])/max(1,len(trs[-60:]))
    compress = v20/v60 if v60 else 1.0
    if diverge > 0: sig = "吸筹背离"
    elif diverge < 0: sig = "派发背离"
    elif compress < 0.8: sig = "波动收敛"
    elif persist >= 0.7: sig = "资金持续流入"
    elif persist <= 0.3: sig = "资金持续流出"
    else: sig = "无显著"
    score = 50 + 40*diverge*min(abs(af_norm)/0.3, 1.0) + 40*(persist-0.5) + 10*(1-min(compress,1.5)/1.5)
    return (sig, max(0, min(100, score)))
