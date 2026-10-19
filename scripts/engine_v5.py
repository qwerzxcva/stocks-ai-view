#!/usr/bin/env python3
"""
engine_v5.py — 算法引擎 v5 (多指标共振 + 自适应权重 + 可证伪)

相比旧版的问题:
  - 权重是手工常数 (位置40+金叉20+RSI15...), 没有依据
  - 只用 MA/RSI 两三个指标, 信息利用率低
  - 无法回答"为什么选它"和"这个权重对不对"

v5 设计:
  1. 多指标共振: 16 个指标 (indicators_ext) 各自给出方向票 (-1/0/+1) 与强度
  2. 共振度: 同向票数 / 总票数, 分歧大的直接降权 (避免"指标打架还高分")
  3. 自适应权重: 用 optimizer.walk_forward 在历史K线上验证权重组合,
     样本外不过关的权重不用 (而非拍脑袋)
  4. 可解释: 每个分数都能拆到具体指标的贡献
  5. 可证伪: 输出预测 + 依据, 由 reflect 模块事后核对

评分公式:
  score = 100 * (Σ w_i · vote_i · strength_i) / (Σ w_i)
          × 共振系数 (0.6 + 0.4 × 同向率)
  - vote_i ∈ {-1, 0, +1}   指标方向
  - strength_i ∈ [0, 1]    该指标本次信号强度
  - w_i                    权重 (默认等权, 可由 optimizer 校准)

输出:
  {code, name, score, resonance, votes:[{indicator, vote, strength, why}], ...}
"""
import argparse
import bisect
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import indicators_ext as I

# 指标 → (方向判断函数, 权重)
# 方向判断输入是指标序列与价格序列, 返回 (vote, strength, why)
def _last(seq):
    for v in reversed(seq):
        if v is not None:
            return v
    return None


def _prev(seq, n=1):
    vals = [v for v in seq if v is not None]
    return vals[-1 - n] if len(vals) > n else None


# ─── 外部数据增强 (collect_all_sources 合并去重后的 all_sources.json) ───
def _data_dir():
    """ai-view data 目录 (CI/本地双环境兼容, 与 paths.py 逻辑对齐)"""
    env = os.environ.get("STOCKS_AI_VIEW_DATA")
    if env:
        return env
    p = os.path.join(HERE, "..", "data")
    return os.path.abspath(p)


def load_all_sources(data_dir=None):
    """读 data/all_sources.json (collect_all_sources.py 合并去重输出)。
    不存在或损坏时返回 None, 不影响纯技术模式。"""
    if data_dir is None:
        data_dir = _data_dir()
    p = os.path.join(data_dir, "all_sources.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _code_match(bare, key):
    """裸码 (301030) 与带前缀码 (sh600000/sz000001) 双向归一化比较"""
    a = str(bare).lower().lstrip("shszbj").zfill(6)
    b = str(key).lower().lstrip("shszbj").zfill(6)
    return a == b


def sector_resonance(all_src, industry_hint):
    """板块联动共振指标: 该股票所属行业板块当日表现。

    判读: all_src.data.sector 里匹配行业名 (双向包含), 取板块当日涨幅:
      领涨 ≥2%  → 看多; 领跌 ≤-2% → 看空; 其余中性不投方向票。
    返回 (vote, strength, why) 或 None (无板块数据/无行业标签)。
    """
    secs = (all_src or {}).get("data", {}).get("sector") or []
    if not secs or not industry_hint:
        return None
    matches = [s for s in secs
               if industry_hint in str(s.get("sector", "")) or str(s.get("sector", "")) in industry_hint]
    if not matches:
        return None
    chgs = [float(s.get("chg_pct") or 0) for s in matches]
    avg_chg = sum(chgs) / len(chgs)
    if avg_chg >= 2.0:
        return (1, min(avg_chg / 6.0, 1.0),
                f"板块[{matches[0].get('sector', '')}] 当日 {avg_chg:+.2f}% 领涨, 板块联动")
    if avg_chg <= -2.0:
        return (-1, min(abs(avg_chg) / 6.0, 1.0),
                f"板块[{matches[0].get('sector', '')}] 当日 {avg_chg:+.2f}% 领跌")
    return (0, 0.1, f"板块[{matches[0].get('sector', '')}] 当日 {avg_chg:+.2f}% 中性")


def capital_flow_consistency(code, data_dir=None):
    """资金流向一致性: moneyflow.json 中该股主力净流入方向。

    net3_wan (3日累计主力) 与 net_main_wan (当日) 同向 → 投方向票;
    反方向 → 中性不投。返回 (vote, strength, why) 或 None。
    """
    if data_dir is None:
        data_dir = _data_dir()
    p = os.path.join(data_dir, "moneyflow.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            mf = json.load(f)
    except Exception:
        return None
    flows = mf.get("flows") or {}
    key = next((k for k in flows if _code_match(code, k)), None)
    if not key:
        return None
    rec = flows[key]
    net = float(rec.get("net_main_wan") or 0)
    net3 = float(rec.get("net3_wan") or 0)
    strength = min(abs(net3) / max(abs(net) * 5, 1e6) * 1.0, 1.0) if net else 0.0
    if net3 > 0 and net > 0:
        return (1, max(0.2, strength), f"主力净流入 {net3:+.0f}万 (3日), 当日 {net:+.0f}万 方向一致")
    if net3 < 0 and net < 0:
        return (-1, max(0.2, strength), f"主力净流出 {net3:+.0f}万 (3日), 当日 {net:+.0f}万 方向一致")
    return (0, 0.1, f"主力资金当日/3日方向不一致 (当日{net:+.0f}万, 3日{net3:+.0f}万)")


def build_votes(close, high, low, vol, source_info=None, code_bare=""):
    """指标投票列表: 1-17 技术指标 + 18-20 外部数据增强 (板块/资金/涨停池)"""
    votes = []

    def add(ind, vote, strength, why):
        votes.append({"indicator": ind, "vote": vote,
                      "strength": round(min(max(strength, 0), 1), 3), "why": why})

    # 1. SuperTrend (趋势)
    st, tr = I.supertrend(high, low, close)
    if _last(tr) is not None:
        t = _last(tr)
        add("SuperTrend", t, 0.9 if t != 0 else 0,
            f"趋势{'多头' if t>0 else '空头'}, 线值 {_last(st):.2f}" if _last(st) else "未形成")

    # 2. MA5 vs MA20 (金叉)
    ma5, ma20 = I.sma(close, 5), I.sma(close, 20)
    l5, l20 = _last(ma5), _last(ma20)
    if l5 and l20:
        dev = (l5 - l20) / l20 * 100
        v = 1 if dev > 0.3 else (-1 if dev < -0.3 else 0)
        add("MA5/MA20", v, min(abs(dev) / 3, 1), f"MA5={l5:.2f} MA20={l20:.2f} 偏离{dev:+.2f}%")

    # 3. RSI
    rsi = I._rsi(close)
    lr = _last(rsi)
    if lr:
        if lr < 30:
            add("RSI", 1, (30 - lr) / 30, f"RSI={lr} 超卖, 反弹概率增")
        elif lr > 70:
            add("RSI", -1, (lr - 70) / 30, f"RSI={lr} 超买, 回踩风险")
        elif 45 <= lr <= 60:
            add("RSI", 1, 0.4, f"RSI={lr} 强势区间")
        else:
            add("RSI", 0, 0.2, f"RSI={lr} 中性")

    # 4. MACD (EMA12-EMA26, 比较是否走强)
    e12, e26 = I.ema(close, 12), I.ema(close, 26)
    le, le26 = _last(e12), _last(e26)
    pe, pe26 = _prev(e12), _prev(e26)
    if le and le26 and pe and pe26:
        hist = le - le26
        ph = pe - pe26
        v = 1 if hist > 0 else -1
        s = min(abs(hist - ph) / max(abs(ph), 0.01), 1)
        add("MACD", v, max(s, 0.3), f"DIF={hist:+.3f} 前值{ph:+.3f} {'走强' if hist>ph else '走弱'}")

    # 5. KDJ (用 9日 RSV 近似 K 值)
    if len(close) >= 9:
        hn = max(high[-9:])
        ln = min(low[-9:])
        rsv = (close[-1] - ln) / (hn - ln) * 100 if hn > ln else 50.0
        v = 1 if rsv > 50 else -1
        add("KDJ(RSV)", v, abs(rsv - 50) / 50, f"RSV={rsv:.1f} (9日)")

    # 6. BOLL (布林带位置)
    mid, up, lo = I.keltner(high, low, close)   # Keltner 与 BOLL 同族
    lm, lu, ll = _last(mid), _last(up), _last(lo)
    if lm and lu and ll and lu > ll:
        pos = (close[-1] - ll) / (lu - ll)
        v = 1 if pos > 0.7 else (-1 if pos < 0.3 else 0)
        add("BOLL/Keltner", v, abs(pos - 0.5) * 2, f"带内位置 {pos*100:.0f}% (中轨{lm:.2f})")

    # 7. Donchian 位置
    # 2026-10-19 回测修正: 原逻辑「距 20 日高 -1% 内投正票」等于追突破,
    # 而 factor_lab 在 45,441/79,518 样本上实测 new_high_20 (win 40.76%,
    # avg -1.43%) 与 breakout_60 (39.86%, -1.71%) 均为全历史负期望。
    # 这里不再给突破正票, 改为中性; 真正的新高信号由 below_ma60 的反面约束。
    du, dm, dl = I.donchian(high, low, 20)
    ldu = _last(du)
    if ldu:
        dev = (close[-1] - ldu) / ldu * 100
        v = -1 if dev > -1 else 0
        add("Donchian20", v, min(abs(dev) / 8, 1) * 0.5,
            f"距20日高 {dev:+.2f}% (追突破无正edge, 不给正票)")

    # 8. CMO
    cmo = I.cmo(close, 14)
    lc = _last(cmo)
    if lc is not None:
        v = 1 if lc > 20 else (-1 if lc < -20 else 0)
        add("CMO", v, min(abs(lc) / 50, 1), f"CMO={lc}")

    # 9. Vortex
    vip, vim = I.vortex(high, low, close)
    lvip, lvim = _last(vip), _last(vim)
    if lvip and lvim:
        v = 1 if lvip > lvim else -1
        add("Vortex", v, min(abs(lvip - lvim) * 5, 1), f"VI+={lvip} VI-={lvim}")

    # 10. Ultimate Oscillator
    uo = I.ultimate_osc(high, low, close)
    lu_ = _last(uo)
    if lu_:
        v = 1 if 30 < lu_ < 70 and lu_ > 50 else (-1 if lu_ >= 70 else 0)
        add("UltimateOsc", v, 0.4 if v else 0.2, f"UO={lu_}")

    # 11. Choppiness (趋势/震荡判别)
    ch = I.choppiness(high, low, close)
    lch = _last(ch)
    if lch:
        # 低 CHOP = 强趋势, 高 CHOP = 震荡 (震荡时不投票)
        if lch < 40:
            v = 1 if close[-1] > _last(I.sma(close, 20)) else -1
            add("Choppiness", v, (50 - lch) / 50, f"CHOP={lch} 趋势市, 顺势")
        else:
            add("Choppiness", 0, 0.1, f"CHOP={lch} 震荡市, 不追趋势信号")

    # 12. HullMA
    hm = I.hull_ma(close, 9)
    lhm = _last(hm)
    if lhm:
        v = 1 if close[-1] > lhm else -1
        add("HullMA", v, min(abs(close[-1] - lhm) / lhm * 100 / 2, 1),
            f"HMA={lhm:.2f} 价格{'上方' if close[-1]>lhm else '下方'}")

    # 13. Elder Force
    ef = I.elder_force(close, vol)
    lef = _last(ef)
    if lef is not None:
        v = 1 if lef > 0 else -1
        add("ElderForce", v, min(abs(lef) / (abs(lef) + 1e5), 1), f"EF={lef:.0f}")

    # 14. Connors RSI
    crs = I.connors_rsi(close)
    lcrs = _last(crs)
    if lcrs is not None:
        if lcrs < 20:
            add("ConnorsRSI", 1, (20 - lcrs) / 20, f"CRSI={lcrs} 极度超卖")
        elif lcrs > 80:
            add("ConnorsRSI", -1, (lcrs - 80) / 20, f"CRSI={lcrs} 极度超买")
        else:
            add("ConnorsRSI", 0, 0.2, f"CRSI={lcrs} 中性")

    # 15. A/D Line (累积派发)
    ad = I.ad_line(high, low, close, vol)
    if len(ad) >= 11 and ad[-1] is not None and ad[-11] is not None:
        d = ad[-1] - ad[-11]
        v = 1 if d > 0 else -1
        add("A/D Line", v, min(abs(d) / (abs(ad[-1]) + 1), 1),
            f"10日{'累积' if d>0 else '派发'} {d:+.0f}")

    # 16. Pivot (枢轴支撑)
    pp = I.pivot_points(high, low, close)
    if pp:
        p = pp[-1]
        c = close[-1]
        if c > p["pp"]:
            v = 1
            why = f"价格在枢轴 {p['pp']} 上方, 支撑 {p['s1']}"
        else:
            v = -1
            why = f"价格在枢轴 {p['pp']} 下方, 压力 {p['r1']}"
        add("Pivot", v, 0.4, why)

    # 17. VWMA
    vw = I.vwma(close, vol, 20)
    lvw = _last(vw)
    if lvw:
        v = 1 if close[-1] > lvw else -1
        add("VWMA", v, min(abs(close[-1] - lvw) / lvw * 100 / 2, 1),
            f"VWMA={lvw:.2f} (量均价)")

    # 18. 板块联动 (行业板块当日涨幅共振)
    if source_info and source_info.get("all_sources"):
        idx = source_info.get("industry") or ""
        sr = sector_resonance(source_info["all_sources"], idx)
        data = source_info["all_sources"].get("data", {})
        c6 = str(code_bare)
        if sr is None and not idx:
            for e in (data.get("event") or []):
                if _code_match(c6, e.get("code", "")) and e.get("industry"):
                    idx = e["industry"]
                    sr = sector_resonance(source_info["all_sources"], idx)
                    break
        if sr is None and not idx:
            for q in (data.get("quote") or []):
                if q.get("kind") != "ZT":
                    continue
                for x in (q.get("detail") or []):
                    if _code_match(c6, x.get("code", "")) and x.get("industry"):
                        idx = x["industry"]
                        sr = sector_resonance(source_info["all_sources"], idx)
                        break
                if sr:
                    break
        if sr:
            add("SectorResonance", sr[0], sr[1], sr[2])

    # 19. 资金流向一致性
    if source_info:
        cf = capital_flow_consistency(code_bare)
        if cf:
            add("CapitalFlowConsistency", cf[0], cf[1], cf[2])

    # 20. 涨停池强度（当日该股是否仍在涨停池）
    if source_info and source_info.get("all_sources"):
        zts = source_info["all_sources"].get("data", {}).get("quote", []) or []
        zt_list = next((q.get("detail") for q in zts if q.get("kind") == "ZT"), None) or []
        dt_list = next((q.get("detail") for q in zts if q.get("kind") == "DT"), None) or []
        c6 = str(code_bare)
        found = [x for x in zt_list if _code_match(c6, x.get("code", ""))]
        if found:
            lb = found[0].get("lianban", 1)
            add("InZTPool", 1, min(lb / 6.0, 1.0),
                f"当日涨停池, 连板{lb}板")
        else:
            found2 = [x for x in dt_list if _code_match(c6, x.get("code", ""))]
            if found2:
                add("InDTPool", -1, 0.9, "当日跌停池")

    # 21. below_ma60 (2026-10-19 回测驱动的唯一正 edge 信号)
    # factor_lab 2,923,687 样本 5 日: win 51.39% avg +0.29% t=60.01
    # 对比 new_high_20 (win 40.76% avg -1.43%) / breakout_60 (39.86% / -1.71%)
    # 这两个追突破特征是全历史负期望, 所以这里不再给它们正向票。
    if len(close) >= 80:
        ma60 = sum(close[-60:]) / 60
        if ma60 > 0:
            dev60 = (close[-1] / ma60 - 1) * 100
            mx20 = max(high[-20:])
            ret20 = (close[-21] / close[-1] - 1) * -100 if close[-21] > 0 else 0
            near_high = mx20 > 0 and close[-1] >= mx20 * 0.98
            if -10 <= dev60 < -5 and not near_high and ret20 <= 15:
                # 主区间: 60 日线下方 5-10%, 未追高, 20 日涨幅有限
                add("below_ma60", 1, 0.9,
                    f"距60日线 {dev60:+.1f}% (回测正edge区), 20日{ret20:+.1f}%")
            elif close[-1] < ma60 * 0.95 and dev60 < -25:
                # 深跌: 可能是接刀, 反向提示
                add("below_ma60", -1, 0.5,
                    f"距60日线 {dev60:+.1f}% 跌深, 反弹不确定")
            elif dev60 > 20:
                # 远离60日线: by回测追高为负期望
                add("momentum_overheat", -1, min(dev60 / 100, 0.8),
                    f"距60日线 {dev60:+.1f}% 过热, 回测追突破为负edge")
    return votes


def score_stock(close, high, low, vol, weights=None, source_info=None, code_bare=""):
    """多指标共振评分（含外部数据增强）。"""
    votes = build_votes(close, high, low, vol, source_info, code_bare)
    if not votes:
        return None
    w = weights or {}
    num = den = 0.0
    bull = bear = 0
    for v in votes:
        wi = w.get(v["indicator"], 1.0)
        num += wi * v["vote"] * v["strength"]
        den += wi
        if v["vote"] > 0:
            bull += 1
        elif v["vote"] < 0:
            bear += 1
    directional = bull + bear
    resonance = (max(bull, bear) / directional) if directional else 0.0
    base = num / den if den else 0.0                     # [-1, 1]
    factor = 0.6 + 0.4 * resonance                       # 分歧降权
    score = round(50 + 50 * base * factor, 1)            # 映射到 0-100
    return {
        "score": score,
        "raw": round(base, 4),
        "resonance": round(resonance, 3),
        "bull": bull, "bear": bear, "neutral": len(votes) - directional,
        "votes": votes,
    }


# ─── 批量: 从本地K线扫全市场 ──────────────────────────────────────
def load_klines(bulk_dir, limit=None):
    """读 K 线 bulk。优先用 data/adjusted/bulk/{code}_qfq.json (真前复权,
    新浪 vip 源), 缺失时回退 day_qfq_*.csv。

    2026-10-17 基准校验发现: day_qfq_*.csv 虽名叫 qfq, 实为新浪非复权接口
    的原始价 (贵州茅台隐含复权因子恒为 1.0, 而真因子 8.88), 所有引擎
    在除权日都被引入虚假跳空。adjusted 副本 (collect_adjusted_bulk.py,
    新浪 vip getKLineData) 经 validate_ground_truth.py 对基准 CSV 校验通过。
    覆盖: 本地 334/5268, 随 nightly-backfill 扩充。
    """
    # 1) 真前复权, 三级来源按准确度:
    #    a. 因子表 (data/adjusted/factors/{code}.json, 基准CSV提取):
    #       qfq = raw × factor/factor_latest —— 与基准逐日一致, 最准
    #    b. hfq 副本锚点归一 (data/adjusted/bulk/{code}_hfq.json):
    #       跨厂商因子口径有约 5-7% 系统差, 仅近似
    #    c. 原始 bulk 兜底 (day_qfq_*.csv 实为新浪非复权原始价, 见下)
    #
    #    2026-10-17 基准校验发现: day_qfq_*.csv 虽名叫 qfq, 实为新浪
    #    非复权接口原始价 (茅台隐含因子恒 1.0, 真因子 8.88), 除权日
    #    全链引擎被引入虚假跳空。有因子表/hfq 的票优先用, 其余仍是
    #    原始价——accuracy gate (validate_ground_truth) 会红到全市场
    #    因子表建完为止。
    try:
        from paths import AI_VIEW_DATA as _AVD
        adj_root = os.path.join(_AVD, "adjusted")
    except Exception:
        adj_root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "data", "adjusted")
    factors_dir = os.path.join(adj_root, "factors")
    factor_tables = {}
    # 优先读合并文件 (1.1MB, 3985 只)。逐股一个文件在 CI 里是 326MB/3985 个
    # 文件, checkout 慢且易超时。云端一直报「因子表 2 只」就是因为只读了
    # factors/ 目录里残留的两个单股文件, 而合并文件没人读。
    merged = os.path.join(adj_root, "factors_all.json")
    if os.path.exists(merged):
        try:
            md = json.load(open(merged, encoding="utf-8"))
            for code, fac in (md.get("factors") or {}).items():
                if fac:
                    factor_tables[code] = fac
        except (OSError, ValueError):
            factor_tables = {}
    if not factor_tables and os.path.isdir(factors_dir):
        import glob as _g
        for fp in _g.glob(os.path.join(factors_dir, "*.json")):
            if os.path.basename(fp).startswith("_"):
                continue
            try:
                fd = json.load(open(fp, encoding="utf-8"))
                factor_tables[fd.get("code")] = fd.get("factors") or {}
            except (OSError, ValueError):
                continue
    hfq_dir = os.path.join(adj_root, "bulk")
    hfq_tables = {}
    if os.path.isdir(hfq_dir):
        import glob as _g
        for hq in _g.glob(os.path.join(hfq_dir, "*_hfq.json")):
            bare = os.path.basename(hq).replace("_hfq.json", "")
            try:
                hd = json.load(open(hq, encoding="utf-8"))
                hb = hd.get("bars") or []
                if len(hb) < 30:
                    continue
                code = hd.get("tk_code") or ("sh" + bare if bare[0] in "69"
                                             else "sz" + bare)
                hfq_tables[code] = [[b[0], float(b[1]), float(b[2]),
                                     float(b[3]), float(b[4]), float(b[5])]
                                    for b in hb]
            except (OSError, ValueError, KeyError):
                continue

    # 先扫一遍原始 bulk 建基础表 (全部 code 的原始价)
    import glob
    data = {}
    files = sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv")))
    for p in files:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                code, rest = line.split("|", 1)
                parts = rest.split("|") if "|" in rest else rest.split(",")
                if len(parts) < 6:
                    continue
                try:
                    data.setdefault(code, []).append(
                        [parts[0], float(parts[1]), float(parts[2]),
                         float(parts[3]), float(parts[4]), float(parts[5])])
                except ValueError:
                    continue
        if limit and len(data) >= limit:
            break
    for c in data:
        data[c].sort(key=lambda b: b[0])

    # 因子表覆盖的 code: 原始价 × 因子 = 真 qfq
    # 因子表有两种形式: 逐日展开 / 跳变点表 (factors_all.json 用后者)。
    # 必须用 bisect 取 <= 目标日的最近跳变点, 不能 exact get,
    # 否则跳变点表 99% 落空, 静默退化成未复权原始价。
    n_factor = 0
    for code, rows in data.items():
        ft = factor_tables.get(code)
        if not ft:
            continue
        kdates = sorted(ft)
        if not kdates:
            continue

        def _fac_at(d, _kd=kdates, _ft=ft):
            i = bisect.bisect_right(_kd, d) - 1
            return _ft[_kd[i]] if i >= 0 else _ft[_kd[0]]

        dates = [r[0] for r in rows]
        f_latest = _fac_at(dates[-1])
        if not f_latest or f_latest <= 0:
            continue
        adj_rows = []
        for r in rows:
            f = _fac_at(r[0])
            if not f or f <= 0:
                adj_rows.append(r)
                continue
            k = f / f_latest
            adj_rows.append([r[0], r[1] * k, r[2] * k, r[3] * k,
                             r[4] * k, r[5]])
        data[code] = adj_rows
        n_factor += 1

    # hfq 覆盖但无因子表的 code: 锚点归一近似
    n_hfq = 0
    for code, hrows in hfq_tables.items():
        if code in data and code in factor_tables:
            continue   # 已有因子表, 更准
        hrows = sorted(hrows, key=lambda r: r[0])
        anchor_f = hrows[-1][2]
        if anchor_f <= 0:
            continue
        data[code] = [[r[0], r[1] / anchor_f, r[2] / anchor_f,
                       r[3] / anchor_f, r[4] / anchor_f, r[5]]
                      for r in hrows]
        n_hfq += 1

    if n_factor or n_hfq:
        print(f"[load_klines] 复权覆盖: 因子表 {n_factor} 只 + hfq近似 {n_hfq} 只",
              flush=True)
    # 数据质量元信息: 调用方 (engine_v5/其他脚本) 靠它判定可信度,
    # 不能只看「加载了 N 只」就当全市场复权已完备。
    load_klines.stats = {
        "n_codes": len(data),
        "n_factor_table": n_factor,
        "n_hfq_approx": n_hfq,
        "n_raw_only": max(0, len(data) - n_factor - n_hfq),
        "adjust_coverage_pct": round(
            (n_factor + n_hfq) / max(1, len(data)) * 100, 2),
        "factor_trusted": n_factor,
        "hfq_approximate": n_hfq,
        "note": "raw_only = 未经复权, 除权日存在虚假跳空风险",
    }
    return data



MAIN_BOARD_PREFIX = ("600", "601", "603", "605", "000", "001", "002")


def _board_filter(rows):
    """只保留沪深主板 (用户无科创/创业板交易权限)。"""
    out = []
    for r in rows:
        bare = r.get("code", "")
        bare = bare[2:] if bare[:2] in ("sh", "sz", "bj") else bare
        if bare[:3] in MAIN_BOARD_PREFIX:
            out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bulk", default=None, help="K线 bulk 目录 (默认 paths.BULK)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--min-score", type=float, default=60)
    ap.add_argument("--no-ext", action="store_true", help="禁用外部数据源增强 (仅指标共振)")
    # 2026-10-19 用户约束: 无科创/创业板交易权限。打分仍覆盖全市场
    # (训练/测试需要), 但推荐输出只留主板。
    ap.add_argument("--main-board-only", action="store_true",
                    help="top 只输出沪深主板 (600/601/603/605/000/001/002), "
                         "688/300/301 仍参与打分但不进推荐")
    args = ap.parse_args()

    bulk = args.bulk
    if not bulk:
        from paths import BULK
        bulk = BULK
    klines = load_klines(bulk, args.limit)
    adj_stats = getattr(load_klines, "stats", {}) or {}
    print(f"[engine_v5] 加载 {len(klines)} 只K线", flush=True)

    all_sources = None if args.no_ext else load_all_sources()
    print(f"[engine_v5] all_sources {'已加载' if all_sources else '未找到 (仅用技术指标)'}", flush=True)

    results = []
    # 数据新鲜度门禁: 同一 bulk 目录里的分片可能不同步 (实测 2026-10-18:
    # shard 00/60 到 10-16, 其余 9 个停在 10-13)。拿旧K线打出的高分不能
    # 当作「今日信号」——按最新全市场交易日算 age, 超 3 个自然日的不进 top,
    # 但在统计里如实计数, 避免把陈旧数据静默当成新鲜。
    all_dates = [bars[-1][0] for bars in klines.values() if bars]
    market_asof = max(all_dates) if all_dates else ""
    # 只看是否与最新交易日同根K线: 不同根 = 该股当日未更新, 属陈旧。
    # 自然日会在周末/节假日误判 (周五->周日=2天), 所以不比天数。
    stale_days_limit = 0

    def _age_days(d):
        try:
            return (datetime.strptime(market_asof, "%Y-%m-%d")
                    - datetime.strptime(d, "%Y-%m-%d")).days
        except Exception:
            return 999
    # 预提取行业标签: 从事件/涨停池中收集 (code -> industry)
    industry_map = {}
    events = (all_sources or {}).get('data', {}).get('event', []) or []
    for e in events:
        industry_map.setdefault(str(e.get('code', '')).zfill(6), e.get('industry', ''))
    # 预提取涨停池中的行业
    zts = (all_sources or {}).get('data', {}).get('quote', []) or []
    for q in zts:
        if q.get('kind') == 'ZT':
            for x in (q.get('detail') or []):
                industry_map.setdefault(str(x.get('code', '')).zfill(6), x.get('industry', ''))

    for code, bars in klines.items():
        if len(bars) < 30:
            continue
        close = [b[2] for b in bars]
        high = [b[3] for b in bars]
        low = [b[4] for b in bars]
        vol = [b[5] for b in bars]
        bare = str(code).zfill(6) if isinstance(code, str) else code
        source_info = None
        if all_sources and not args.no_ext:
            source_info = {
                "all_sources": all_sources,
                "industry": industry_map.get(bare, ''),
            }
        r = score_stock(close, high, low, vol, source_info=source_info, code_bare=bare)
        if not r:
            continue
        r["code"] = bare
        r["date"] = bars[-1][0]
        r["price"] = close[-1]
        r["bars"] = len(bars)
        age = _age_days(r["date"])
        r["data_age_days"] = age
        r["data_fresh"] = age <= stale_days_limit
        r["source_ts"] = (all_sources or {}).get('summary', {}).get('ts')
        r["data_provenance"] = {
            "technical": "indicators_ext (17 指标, 等权)",
            "external": "collect_all_sources/all_sources.json" if all_sources and source_info else "none",
            "resonance_new": ["SectorResonance", "CapitalFlowConsistency", "InZTPool"],
            "industry_from": industry_map.get(bare, "unknown") or "N/A",
        }
        results.append(r)

    results.sort(key=lambda x: -x["score"])
    fresh = [r for r in results if r.get("data_fresh")]
    stale = [r for r in results if not r.get("data_fresh")]
    now = datetime.now(CST)
    out = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": "v5 (多指标共振 + 自适应权重 + 外部数据增强)",
        "n_scanned": len(klines),
        "n_scored": len(results),
        "market_asof": market_asof,
        # 数据质量门控: 复权 / 新鲜度 / 完整性三层可视化。
        # 任何一层不达标时, 下游 (前端/日报) 不应把本轮结果当成全市场可交易信号。
        "data_quality": {
            "adjustment": adj_stats,
            "adjust_trust_level": (
                "full" if adj_stats.get("adjust_coverage_pct", 0) >= 95 else
                "partial" if adj_stats.get("adjust_coverage_pct", 0) >= 30 else
                "minimal"),
            "fresh_completeness_pct": round(
                len(fresh) / max(1, len(results)) * 100, 2),
            "gates": {
                "adjust_coverage_ok": adj_stats.get("adjust_coverage_pct", 0) >= 95,
                "freshness_ok": len(stale) <= max(10, len(results) * 0.01),
                "min_universe_ok": len(results) >= 3000,
            },
        },
        "freshness": {
            "limit_days": stale_days_limit,
            "n_fresh": len(fresh),
            "n_stale": len(stale),
            "stale_codes": [r["code"] for r in stale[:50]],
            "note": "K线分片可能不同步; 旧数据不冒充今日信号",
        },
        "indicators": 20,  # 17 tech + 3 external
        "formula": "score = 50 + 50 * (Σ w·vote·strength / Σ w) * (0.6 + 0.4*共振率)",
        # top 只给新鲜数据; 陈旧分片的股票单列, 不用旧信号冒充今日推荐
        "top": _board_filter(fresh)[:50] if args.main_board_only else fresh[:50],
        "stale_top": stale[:20],
        "meta": {
            "resonance": "同向票数/方向票数, 分歧大时降权 (0.6~1.0 系数)",
            "weights": "默认等权; 可由 optimizer.py walk_forward 校准后写入",
            "external_enhancement": [
                "SectorResonance: 行业板块当日涨幅 + 板块内股票共振",
                "CapitalFlowConsistency: 主力资金近 3 日净流入方向",
                "InZTPool: 当日是否处于涨停池（连板数加权）",
            ],
            "provenance_note": "每只股票顶部含 source_ts / data_provenance 字段, 用于可追溯",
            "caveat": "技术指标共振不等于必然上涨, 只反映多维度信号一致性; "
                      "样本外未验证的权重不应用于实盘",
        },
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"\n=== TOP 10 (共振度优先, 仅新鲜K线) ===")
    for r in fresh[:10]:
        print(f"  {r['code']} score={r['score']:5.1f} 共振={r['resonance']:.2f} "
              f"多{r['bull']}/空{r['bear']}/中{r['neutral']} 价={r['price']} "
              f"K线={r['date']}")
    if stale:
        print(f"\n[陈旧] {len(stale)} 只 K线早于 {market_asof} 超 {stale_days_limit} 天, 不进 top")
    hi = [r for r in results if r["score"] >= args.min_score]
    print(f"\n≥{args.min_score}分: {len(hi)} 只")
    print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
