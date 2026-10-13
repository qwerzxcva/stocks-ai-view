#!/usr/bin/env python3
"""
reflect_engine.py — 复盘与自我反思闭环 (增强版)

目的: 回答「我们的预测到底准不准」, 而不是只输出漂亮的分数。

增强点 (TSPv3):
  1. 支持多持有期 --horizons 1,3,5,10 (默认 5,10)
  2. 市场环境区分: 从 latest.json/market_now.json 读取市场温度,
     为每笔预测按 pred_date 标注市场环境 (牛/熊/震荡/未知), 分环境统计胜率
  3. Wilson 置信区间: 对每个胜率 (bull/bear/raw) 计算 95% Wilson CI,
     避免样本少时高估胜率

防自欺设计:
  - 只用「预测时点已知的信息」, 不用未来数据 (无前视偏差)
  - 明确标注样本量, 样本 <30 的结论标「证据不足」
  - 同时报告「多空双向胜率」, 只报看多胜率是选择性披露
  - 每个分环境/分指标的胜率都附 Wilson 95% 置信区间
"""
import argparse
import glob
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))

# ─── Wilson 置信区间 (95%, 可选 z 值) ──────────────────────────────
def wilson_ci(k, n, z=1.96):
    """返回 [lo, hi] (百分比)。k=命中数, n=样本数。
    使用 Wilson score interval, 对极端小样本比 Wald 区间更准确。
    """
    if n == 0:
        return None, None
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    lo = max(0.0, center - half)
    hi = min(1.0, center + half)
    return round(lo * 100, 2), round(hi * 100, 2)


# ─── 市场环境推断 ─────────────────────────────────────────────────
def _parse_int_date(s):
    """把 '20261013' 或 '2026-10-13' 转换为字符串键; 失败返回 None"""
    s = str(s).replace('-', '').strip()
    return s if len(s) == 8 and s.isdigit() else None


def load_phase_snapshots(data_dir):
    """从 data/latest.json / market_now.json 读市场快照 → {date_key: phase_dict}。
    只保存最新一条 (CI 只保留一份), 返回 (latest_phases, sources)。
    latest_phases: {date_key: {label, source, detail}}
    sources: 读取到的文件列表 (供元数据输出)
    """
    phases = {}
    sources = []
    for fname in ('latest.json', 'market_now.json'):
        p = os.path.join(data_dir, fname)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding='utf-8') as f:
                d = json.load(f)
        except Exception:
            continue
        dt = d.get('date') or ''
        key = _parse_int_date(dt)
        if not key:
            continue
        entry = _infer_phase(d, fname)
        if entry:
            phases[key] = entry
            sources.append(fname)
    return phases, sources


def _infer_phase(raw, source):
    """raw 是 JSON dict; source 是文件名 (latest.json/market_now.json)。
    返回 {label, source, detail} 或 None。
    label: 'bull' / 'bear' / 'range'
    """
    # 优先读 latest.json 的 phase 字段 (已有 score/stage)
    phase = raw.get('phase')
    if phase and isinstance(phase, dict):
        score = float(phase.get('score') or 0)
        stage = str(phase.get('stage') or '')
        if score >= 20:
            label = 'bull'
        elif score <= -20:
            label = 'bear'
        else:
            label = 'range'
        return {
            'label': label,
            'source': f'{source}:phase',
            'detail': f"score={score:.1f}, stage={stage}, zt_rate={phase.get('seal_rate')}",
        }
    # 回退: 用 market_now.json 的 market 字段算
    mk = raw.get('market') or {}
    adv = int(mk.get('advance') or 0)
    dec = int(mk.get('decline') or 0)
    total = adv + dec + int(mk.get('flat') or 0)
    zt = float(mk.get('zt_rate') or 0)
    up_pct = float(mk.get('up_pct') or 50)
    if total <= 0:
        return None
    net = (adv - dec) / total * 100
    if net >= 8 and zt >= 70 and up_pct >= 55:
        label = 'bull'
    elif net <= -8 and zt <= 50 and up_pct <= 45:
        label = 'bear'
    else:
        label = 'range'
    return {
        'label': label,
        'source': f'{source}:market',
        'detail': f"net_adv={net:.1f}%, zt_rate={zt}%",
    }


def match_phase_for(pred_date, phase_map):
    """给定 pred_date 字符串 (如 '2026-10-10'), 找到最近的相位快照并返回 label。
    如果 pred_date 在快照日期当天或之前, 直接取最近那个快照。
    找不到则返回 ('unknown', None)。"""
    pk = _parse_int_date(pred_date)
    if not pk:
        return 'unknown', None
    # 找最近的日期 <= pk 的快照
    closes = sorted(k for k in phase_map if k <= pk)
    if not closes:
        # 没有更早的快照, 尝试用当前最新快照, 但不作保证
        best = sorted(phase_map.keys())[-1]
        e = phase_map[best]
        return e['label'], {'note': f'pred_date={pk} 无历史快照, 使用最新 {best} 作为代理'}
    e = phase_map[closes[-1]]
    return e['label'], None


# ─── K线 / 验证核心逻辑 ──────────────────────────────────────────
def load_klines(bulk_dir):
    """加载本地K线 → {code: [[date,o,c,h,l,v], ...]}"""
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
    for c in data:
        data[c].sort(key=lambda b: b[0])
    return data


def kline_index(bars):
    """date → index 映射"""
    return {b[0]: i for i, b in enumerate(bars)}


def verify_prediction(bars, pred_date, horizon):
    """pred_date 之后 horizon 个交易日的实际涨跌 %。"""
    idx = kline_index(bars)
    if pred_date not in idx:
        # 找 pred_date 当天或之后最近的一根
        cands = [d for d in idx if d >= pred_date]
        if not cands:
            return None
        i = idx[min(cands)]
    else:
        i = idx[pred_date]
    j = i + horizon
    if j >= len(bars):
        return None
    entry = bars[i][2]
    exit_ = bars[j][2]
    if entry <= 0:
        return None
    return round((exit_ / entry - 1) * 100, 2)


def reflect_engine_v5(pred_file, klines, horizons=(5, 10)):
    """对 engine_v5 输出做事后验证。"""
    d = json.load(open(pred_file, encoding="utf-8"))
    ts = d.get("ts", "")
    pred_date = d.get("top", [{}])[0].get("date") if d.get("top") else ts[:10]
    if not pred_date:
        return None

    results = []
    for r in d.get("top", []):
        code = r["code"]
        bars = klines.get(code)
        if not bars:
            continue
        row = {"code": code, "pred_date": pred_date, "score": r["score"],
               "resonance": r["resonance"], "bull": r["bull"], "bear": r["bear"]}
        for h in horizons:
            row[f"actual_{h}d"] = verify_prediction(bars, pred_date, h)
        # 记录每个指标的票, 用于统计指标胜率
        # votes 兼容两种形式: slim 前 list[{indicator,vote}] / slim 后 dict{indicator:vote}
        _v = r.get("votes") or []
        if isinstance(_v, list):
            row["votes"] = {v["indicator"]: v["vote"] for v in _v
                           if isinstance(v, dict) and "indicator" in v}
        else:
            row["votes"] = dict(_v)
        results.append(row)
    return {"pred_file": os.path.basename(pred_file), "pred_date": pred_date,
            "n": len(results), "rows": results}


def analyze(results_list, horizons, phase_map):
    """统计: 方向命中率 + 分市场环境胜率 + 各指标胜率 (均含 Wilson CI)。"""
    # 0. 展平所有行并标注市场环境
    tagged = []
    for rl in results_list:
        for r in rl["rows"]:
            label, note = match_phase_for(r.get("pred_date", ""), phase_map)
            r["_phase_label"] = label
            if note:
                r["_phase_note"] = note
            tagged.append(r)

    # 1. 整体方向命中 (含 Wilson CI)
    overall = {}
    for h in horizons:
        key = f"actual_{h}d"
        valid = [(r, r[key]) for r in tagged if r.get(key) is not None]
        if not valid:
            overall[h] = {"n": 0}
            continue
        bull_hits = sum(1 for r, a in valid if r["bull"] > r["bear"] and a > 0)
        bull_n = sum(1 for r, a in valid if r["bull"] > r["bear"])
        bear_hits = sum(1 for r, a in valid if r["bear"] > r["bull"] and a < 0)
        bear_n = sum(1 for r, a in valid if r["bear"] > r["bull"])
        avg_ret = sum(a for _, a in valid) / len(valid)
        win = sum(1 for _, a in valid if a > 0)
        overall[h] = {
            "n": len(valid),
            "bull_win_rate": round(bull_hits / bull_n * 100, 1) if bull_n else None,
            "bull_win_rate_ci": wilson_ci(bull_hits, bull_n),
            "bull_n": bull_n,
            "bear_win_rate": round(bear_hits / bear_n * 100, 1) if bear_n else None,
            "bear_win_rate_ci": wilson_ci(bear_hits, bear_n),
            "bear_n": bear_n,
            "avg_ret_pct": round(avg_ret, 2),
            "raw_win_rate": round(win / len(valid) * 100, 1),
            "raw_win_rate_ci": wilson_ci(win, len(valid)),
        }

    # 2. 分市场环境 (牛/熊/震荡/未知) 的胜率
    by_phase = {}
    for label in ("bull", "bear", "range", "unknown"):
        subset = [r for r in tagged if r.get("_phase_label") == label]
        if not subset:
            continue
        ph = {"n_records": len(subset)}
        for h in horizons:
            key = f"actual_{h}d"
            valid = [(r, r[key]) for r in subset if r.get(key) is not None]
            if not valid:
                continue
            bull_hits = sum(1 for r, a in valid if r["bull"] > r["bear"] and a > 0)
            bull_n = sum(1 for r, a in valid if r["bull"] > r["bear"])
            bear_hits = sum(1 for r, a in valid if r["bear"] > r["bull"] and a < 0)
            bear_n = sum(1 for r, a in valid if r["bear"] > r["bull"])
            win = sum(1 for _, a in valid if a > 0)
            ph[f"{h}d"] = {
                "n": len(valid),
                "bull_win_rate": round(bull_hits / bull_n * 100, 1) if bull_n else None,
                "bull_win_rate_ci": wilson_ci(bull_hits, bull_n),
                "bear_win_rate": round(bear_hits / bear_n * 100, 1) if bear_n else None,
                "bear_win_rate_ci": wilson_ci(bear_hits, bear_n),
                "raw_win_rate": round(win / len(valid) * 100, 1),
                "raw_win_rate_ci": wilson_ci(win, len(valid)),
            }
        by_phase[label] = ph

    # 3. 各指标胜率 (含 Wilson CI)
    #    标签取「该记录可用的最大持有期」, 避免最新预测因 10d 未来数据缺失而整体丢失
    ind_stats = defaultdict(lambda: {"bull_n": 0, "bull_hit": 0, "bear_n": 0, "bear_hit": 0})
    for r in tagged:
        usable = [h for h in sorted(horizons, reverse=True) if r.get(f"actual_{h}d") is not None]
        if not usable:
            continue
        act = r[f"actual_{usable[0]}d"]
        for ind, vote in r.get("votes", {}).items():
            s = ind_stats[ind]
            if vote > 0:
                s["bull_n"] += 1
                if act > 0:
                    s["bull_hit"] += 1
            elif vote < 0:
                s["bear_n"] += 1
                if act < 0:
                    s["bear_hit"] += 1

    indicators = {}
    for ind, s in ind_stats.items():
        bn, bh = s["bull_n"], s["bull_hit"]
        sn, sh = s["bear_n"], s["bear_hit"]
        indicators[ind] = {
            "bull_n": bn,
            "bull_win_rate": round(bh / bn * 100, 1) if bn else None,
            "bull_win_rate_ci": wilson_ci(bh, bn),
            "bear_n": sn,
            "bear_win_rate": round(sh / sn * 100, 1) if sn else None,
            "bear_win_rate_ci": wilson_ci(sh, sn),
            "evidence": "insufficient" if (bn + sn) < 30 else "ok",
        }

    # 4. 按多空胜率均值排序, 找出有效/噪声指标
    def quality(v):
        rates = [x for x in (v["bull_win_rate"], v["bear_win_rate"]) if x is not None]
        return sum(rates) / len(rates) if rates else 50.0

    ranked = sorted(indicators.items(), key=lambda kv: -quality(kv[1]))
    return {
        "overall": overall,
        "by_phase": by_phase,
        "indicators": indicators,
        "ranked": [{"indicator": k, "quality": round(quality(v), 1), **v}
                   for k, v in ranked],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pred-dir', default='/tmp', help='engine_v5 输出所在目录')
    ap.add_argument('--bulk', default=None, help='K线 bulk 目录 (默认 paths.BULK)')
    ap.add_argument('--data-dir', default=None, help='ai-view data 目录 (默认 paths.AI_VIEW_DATA)')
    ap.add_argument('--horizons', default='5,10')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    horizons = tuple(int(x) for x in args.horizons.split(','))
    pred_files = sorted(glob.glob(os.path.join(args.pred_dir, 'engine_v5*.json')))
    if not pred_files:
        print(f'未找到预测文件: {args.pred_dir}/engine_v5*.json', file=sys.stderr)
        return 1

    bulk = args.bulk
    if not bulk:
        from paths import BULK
        bulk = BULK
    klines = load_klines(bulk)
    print(f'[reflect] K线 {len(klines)} 只, 预测文件 {len(pred_files)} 个', flush=True)

    results_list = []
    for pf in pred_files:
        rl = reflect_engine_v5(pf, klines, horizons)
        if rl and rl['rows']:
            print(f'  {os.path.basename(pf)}: {rl["n"]} 条 (pred_date={rl["pred_date"]})', flush=True)
            results_list.append(rl)

    if not results_list:
        print('无可验证记录 (可能预测日期太近, 未来数据还不存在)', file=sys.stderr)
        return 1

    # 加载市场相位快照 (latest.json / market_now.json)
    data_dir = args.data_dir or os.path.join(HERE, '..', 'data')
    phase_map, phase_sources = load_phase_snapshots(data_dir)
    latest_phases = [phase_map[k] for k in sorted(phase_map.keys())]
    current_phase = latest_phases[-1] if latest_phases else None
    print(f'[reflect] 市场相位快照: {len(phase_map)} 条 (source={phase_sources})', flush=True)
    print(f'[reflect] 最新环境: {current_phase["label"]} ({current_phase["detail"] if current_phase else "N/A"})', flush=True)

    analysis = analyze(results_list, horizons, phase_map)
    now = datetime.now(CST)
    out = {
        'ts': now.isoformat(timespec='seconds'),
        'horizons': list(horizons),
        'n_pred_files': len(results_list),
        'n_records': sum(rl['n'] for rl in results_list),
        'market_phase': {
            'current': current_phase,
            'sources': phase_sources,
            'snapshots': {
                k: {'label': v['label'], 'detail': v['detail']} for k, v in sorted(phase_map.items())
            },
        },
        'analysis': analysis,
        'meta': {
            'method': '预测时点的多空票 vs 预测后 N 日实际涨跌, 无前视偏差; Wilson 95% CI 标注胜率可信度',
            'bull_win_rate': '指标看多且实际上涨的比例',
            'bear_win_rate': '指标看空且实际下跌的比例',
            'evidence': '样本<30 标 insufficient, 结论不可信',
            'phase_method': 'pred_date 附近的 latest.json/market_now.json 市场相位快照匹配; 找不到则用最近快照代理并标注 note',
            'ci': 'Wilson score interval, z=1.96 (95%), 对极端小样本比 Wald 区间更准确',
            'caveat': '单一指标历史胜率接近50%即噪声; 只报看多胜率属选择性披露, 故同时报多空双向; '
                     '分环境样本量小或无历史快照时结果仅供参考',
        },
    }
    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print()
    print(f"=== 整体方向命中 (n={out['n_records']}) ===")
    for h, v in analysis["overall"].items():
        if v.get("n"):
            ci = v["bull_win_rate_ci"]
            print(f"  {h}日后: 看多胜率={v['bull_win_rate']}% CI[{ci[0]}, {ci[1]}] (n={v['bull_n']}) | "
                  f"看空胜率={v['bear_win_rate']}% (n={v['bear_n']}) | "
                  f"平均收益={v['avg_ret_pct']}%")
    print()
    print("=== 分市场环境 (bull=牛 / bear=熊 / range=震荡 / unknown=无历史快照) ===")
    for label, v in analysis["by_phase"].items():
        segs = []
        for hk in [f"{h}d" for h in horizons if f"{h}d" in v]:
            hv = v[hk]
            segs.append(f"{hk}={hv.get('raw_win_rate')}%(n={hv.get('n')})")
        print(f"  {label}: n={v['n_records']}" + (f"  {' '.join(segs)}" if segs else " (无可验证记录)"))
    print()
    print("=== 指标质量排名 (多空胜率均值, 含 Wilson 95% CI) ===")
    for r in analysis["ranked"][:12]:
        ev = "" if r["evidence"] == "ok" else " [样本不足]"
        cbi = r.get("bull_win_rate_ci") or [None, None]
        print(f"  {r['indicator']:20s} 质量={r['quality']:5.1f} "
              f"多{r['bull_win_rate']}% CI[{cbi[0]}, {cbi[1]}] (n={r['bull_n']}) "
              f"空{r['bear_win_rate']}%(n={r['bear_n']}){ev}")
    print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
