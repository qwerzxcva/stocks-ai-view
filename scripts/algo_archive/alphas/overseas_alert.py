# 缺口引擎包 4: 外围关联 + 玄学降权计分 + 行业映射 — 2026-10-22 深夜
import urllib.request

UA = {'User-Agent': 'Mozilla/5.0'}

# ── 1. 外围关联引擎 (overseas_linkage) ──
OVERSEAS = [('usDJI', '道琼斯'), ('usIXIC', '纳斯达克'), ('hkHSI', '恒生'), ('hkHSTECH', '恒生科技')]

def overseas_snapshot():
    """外围实时快照 → A 股开盘影响预测
    经验规则 (历史统计): 恒生科技与 A 股科创相关最高; 隔夜美股→A股开盘缺口"""
    out = {}
    for code, name in OVERSEAS:
        try:
            d = urllib.request.urlopen(f"http://qt.gtimg.cn/q={code}", timeout=8).read().decode('gbk', 'ignore')
            f = d.split('~')
            if len(f) > 32 and f[1]:
                out[name] = {"px": float(f[3]), "chg_pct": float(f[32])}
        except Exception:
            continue
    # 影响 A 股开盘的综合判断
    chgs = [v["chg_pct"] for v in out.values()]
    if chgs:
        avg = sum(chgs) / len(chgs)
        if avg < -2:
            verdict, impact = "外围重挫", -1.0
        elif avg < -0.8:
            verdict, impact = "外围偏空", -0.5
        elif avg > 2:
            verdict, impact = "外围大涨", 1.0
        elif avg > 0.8:
            verdict, impact = "外围偏多", 0.5
        else:
            verdict, impact = "外围中性", 0.0
        out["_verdict"] = {"avg_chg": round(avg, 2), "verdict": verdict, "ah_open_impact": impact}
    return out

# ── 2. 异动监测 (用户清单: 外围大跌/板块突涨/龙头大跌) ──
def anomaly_check(zt_pool_size, max_lb, prev_zt, prev_lb, overseas):
    """异动三查: 情绪骤变/龙头异常/外围冲击
    返回告警列表"""
    alerts = []
    if overseas.get("_verdict", {}).get("avg_chg", 0) < -2:
        alerts.append(f"外围大跌: {overseas['_verdict']['avg_chg']}% — A股开盘缺口风险")
    if prev_zt and zt_pool_size < prev_zt * 0.7:
        alerts.append(f"涨停骤降: {prev_zt}→{zt_pool_size} — 情绪冰点加速")
    if prev_zt and zt_pool_size > prev_zt * 1.5:
        alerts.append(f"涨停激增: {prev_zt}→{zt_pool_size} — 情绪亢奋")
    if prev_lb and max_lb > prev_lb:
        alerts.append(f"连板高度抬升: {prev_lb}→{max_lb} 板 — 主线强化")
    return alerts

# ── 3. 玄学参与评分 (降权但计分, 用户指示) ──
# 修改: 玄学引擎 weight 0 → 0.2 (计分不投票: 不单独立推票, 但融合总分里计入)
MYSTIC_WEIGHT = 0.2

# ── 4. 行业映射 v1 (代码段+已知代表票粗分 28 行业, 后续接 ths_universe) ──
INDUSTRY_HINT = {
    "银行": ("sh601", "sh6019", "sz0012"),  # 前缀提示 (粗)
    # 完整映射依赖 ths_universe.json (上游有, 明日接入)
}

def industry_of(sym):
    """粗分行业 — 精确映射待 ths_universe 接入"""
    if sym.startswith('sh688'): return "科创板"
    if sym.startswith('sz30'): return "创业板"
    if sym in ("sh601328", "sh601166", "sh601988", "sh600036", "sh601939"): return "银行"
    if sym in ("sh600900", "sh600886", "sh600025"): return "电力"
    if sym in ("sh601088", "sh601898", "sh600188"): return "煤炭"
    return "其他"
