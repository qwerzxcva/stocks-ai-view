#!/usr/bin/env python3
"""
market_anomaly_engine.py — 行情异动引擎 (新增引擎, 不改任何既有文件)

与既有 scripts/anomaly_detector.py 的区别: 那是数据质量告警; 本引擎检测行情层面异动。

检测类型 (全部基于日线, 无分钟线 — 已知缺口, 此处声明降级):
  1. index_gap        指数单日涨跌幅超阈 (默认 ±2%, --index-th 可调)
  2. sector_surge     板块突涨: 行业成分股当日等权涨幅超阈 (industry_map_em.json + 日线聚合)
  3. leader_crash     龙头大跌: 行业内区间成交额头部股 (--topn, 默认 3) 当日跌超阈
  4. zt_flood/dt_flood 涨停潮/跌停潮: 优先 emotion/daily.jsonl (仅 6 行, 覆盖不足则降级为
                      日线分片自算涨跌幅≥±9.8% 计数; 主板 10% 口径近似, ST/20cm 未区分)
  5. dual_mainline    双主线: 当日涨幅前五行业中 ≥2 个同时满足涨幅与涨停数双阈共振

异动预测 (walkforward 思想, 无前视):
  对每类异动, 用历史同类异动事件日之后的实际大盘走向统计条件胜率:
  T+1 / T+5 上证综指收阳比例, 含样本数 n 与 Wilson 95% CI。
  预测基准面只使用 asof 之前的事件; asof 当日异动的「预测」即该历史条件频率, 不做点预测。

数据来源 (全部本地真实文件):
  - 指数: stocks-data-kline/data/index/history/sh000001.txt (asof 2026-10-09)
  - 全 A: stocks-data-kline/data/bulk/day_qfq_*.csv (asof 2026-10-16, 滞后 2026-10-23 已知)
  - 行业映射: stocks-ai-view/data/industry_map_em.json (1050 股覆盖, 多对一取首命中, 降级标注)
  - 情绪: stocks-data-live/data/emotion/daily.jsonl (仅 6 行, 缺口径则降级自算)

已知降级:
  - 无分钟线 → 无法识别盘中冲高回落/炸板, 全部以收盘口径;
  - 行业映射仅覆盖 1050/5343 股 → 板块口径为「已映射成分股等权」, 非全口径;
  - 涨跌停统计统一 ±9.8% 阈值, 未按板块区分 10%/20%/ST5%。
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, "..", "..", "data", "engines", "market_anomaly_engine.json"))


KLINE = os.path.expanduser(os.environ.get("KLINE_DIR", "~/workspace/stocks-data-kline/data"))
LIVE = os.path.expanduser(os.environ.get("LIVE_DIR", "~/workspace/stocks-data-live/data"))
AIV = os.path.expanduser(os.environ.get("AIV_DIR", "~/workspace/stocks-ai-view/data"))
INDEX_TXT = os.path.join(KLINE, "index", "history", "sh000001.txt")
EMOTION_JSONL = os.path.join(LIVE, "emotion", "daily.jsonl")
INDUSTRY_JSON = os.path.join(AIV, "industry_map_em.json")

ZT_PCT = 9.8       # 涨停近似阈值 %
DT_PCT = -9.8      # 跌停近似阈值 %
ZT_FLOOD_N = 80    # 涨停潮阈值 (情绪口径常见尺度)
DT_FLOOD_N = 30    # 跌停潮阈值
SURGE_SECTOR_PCT = 2.5   # 板块突涨: 成分等权涨幅阈 %
LEADER_DROP_PCT = -5.0   # 龙头大跌阈 %
DUAL_TOP_N = 5           # 双主线: 涨幅前五行业
DUAL_MIN_RESONANCE = 2   # 至少几个行业共振
DUAL_ZT_MIN = 2          # 共振行业最少涨停数


def read_index(path):
    rows = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p = line.split("|")
            if len(p) < 7:
                continue
            try:
                rows.append({"date": p[1], "open": float(p[2]), "close": float(p[3]),
                             "high": float(p[4]), "low": float(p[5]), "vol": float(p[6])})
            except ValueError:
                continue
    rows.sort(key=lambda r: r["date"])
    return rows


def load_bulk(asof, limit=None):
    """返回 hist: code -> {date:(close,vol)}, amt: code->累计成交额代理, 日期集合。
    limit>0 时先扫代码清单再截取前 limit 只 (确定性抽样), 第二遍只载入选中股票。"""
    files = sorted(glob.glob(os.path.join(KLINE, "bulk", "day_qfq_*.csv")))
    codes = []
    seen = set()
    for fp in files:
        with open(fp, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                c = line.split("|", 1)[0]
                if c not in seen:
                    seen.add(c)
                    codes.append(c)
    keep = set(codes[:limit]) if limit else set(codes)
    sampled = limit is not None and limit < len(codes)
    hist, amt, dates = {}, {}, set()
    for fp in files:
        with open(fp, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                p = line.rstrip("\n").split("|")
                if len(p) < 7 or p[1] > asof or p[0] not in keep:
                    continue
                c, v = float(p[3]), float(p[6])
                hist.setdefault(p[0], {})[p[1]] = (c, v)
                amt[p[0]] = amt.get(p[0], 0.0) + c * v  # 成交额代理, 仅排序用
                dates.add(p[1])
    return hist, amt, sorted(dates), sampled, len(keep), len(codes)


def load_industry_map():
    """返回 code(带市场前缀) -> industry。源文件键为 6 位数字代码。"""
    if not os.path.exists(INDUSTRY_JSON):
        return {}
    d = json.load(open(INDUSTRY_JSON, encoding="utf-8"))
    m = d.get("map", {})
    out = {}
    for code, ind in m.items():
        prefix = "sh" if code.startswith(("6", "9")) else "sz"
        out[prefix + code] = ind
    return out


def load_emotion(asof):
    out = {}
    if not os.path.exists(EMOTION_JSONL):
        return out
    with open(EMOTION_JSONL, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("date", "") <= asof:
                out[d["date"]] = d
    return out


def wilson_ci(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [round(max(0.0, center - half), 4), round(min(1.0, center + half), 4)]


def cond_winrate(event_dates, index_rows, horizon):
    """事件日后 horizon 日指数收阳(收盘>事件日收盘)的历史频率。事件日须严格早于统计终点。"""
    dates = [r["date"] for r in index_rows]
    pos = {d: i for i, d in enumerate(dates)}
    k = n = 0
    for d in event_dates:
        i = pos.get(d)
        if i is None or i + horizon >= len(index_rows):
            continue  # 无前视: 终点未知的事件不计
        n += 1
        if index_rows[i + horizon]["close"] > index_rows[i]["close"]:
            k += 1
    if n == 0:
        return {"n": 0, "p": None, "ci95": None}
    return {"n": n, "p": round(k / n, 4), "ci95": wilson_ci(k, n)}


def daily_stats(hist, dates, code2ind):
    """逐日聚合: {date: {zt, dt, n, ind_ret{ind:[rets]}, ind_zt{ind:n}, code_ret{code:ret}, code_amt}}"""
    per_date = {d: {"zt": 0, "dt": 0, "n": 0, "ind_ret": {}, "ind_zt": {},
                    "code_ret": {}, "code_amt": {}} for d in dates}
    for code, series in hist.items():
        sd = sorted(series)
        ind = code2ind.get(code)
        for j in range(1, len(sd)):
            d, dprev = sd[j], sd[j - 1]
            if d not in per_date:
                continue
            c, v = series[d]
            pc = series[dprev][0]
            if not pc:
                continue
            r = (c / pc - 1) * 100
            st = per_date[d]
            st["n"] += 1
            st["code_ret"][code] = r
            st["code_amt"][code] = c * v  # 成交额代理 (前复权口径, 仅排序用)
            if r >= ZT_PCT:
                st["zt"] += 1
                if ind:
                    st["ind_zt"][ind] = st["ind_zt"].get(ind, 0) + 1
            elif r <= DT_PCT:
                st["dt"] += 1
            if ind:
                st["ind_ret"].setdefault(ind, []).append(r)
    return per_date


def detect_events(per_date, dates, index_rows, emotion, args):
    """对 dates 内每一日判定各类型异动, 返回 {type: [event_date,...]} 与 asof 当日明细。"""
    idx_ret = {}
    for j in range(1, len(index_rows)):
        idx_ret[index_rows[j]["date"]] = (index_rows[j]["close"] / index_rows[j - 1]["close"] - 1) * 100

    events = {"index_gap_up": [], "index_gap_down": [], "sector_surge": [],
              "leader_crash": [], "zt_flood": [], "dt_flood": [], "dual_mainline": []}
    detail = {}  # date -> list of anomaly records (asof 用)

    # 龙头名单: 全窗口成交额 topN per 行业 (只用 <= 当日数据会在循环内重算太贵;
    # 采用 walkforward 折衷: 用截至 asof 全历史成交额排序 — 对历史事件判定有轻微前视,
    # 故龙头大跌的历史胜率仅作参考并在输出标注 leader_lookahead_note)
    code2ind = detect_events.code2ind
    hist_amt = detect_events.hist_amt
    leaders = {}  # ind -> [codes]
    for code, ind in code2ind.items():
        leaders.setdefault(ind, []).append((hist_amt.get(code, 0.0), code))
    leader_set = {}
    for ind, lst in leaders.items():
        lst.sort(reverse=True)
        leader_set[ind] = [c for _, c in lst[:args.topn]]

    for d in dates:
        st = per_date[d]
        day_anoms = []
        # 1. 指数异动
        r = idx_ret.get(d)
        if r is not None:
            if r >= args.index_th:
                events["index_gap_up"].append(d)
                day_anoms.append(("index_gap_up", round(r, 2)))
            elif r <= -args.index_th:
                events["index_gap_down"].append(d)
                day_anoms.append(("index_gap_down", round(r, 2)))
        # 4. 涨停潮/跌停潮: emotion 优先
        em = emotion.get(d)
        if em is not None:
            zt_n, dt_n, src = em.get("zt", 0), em.get("dt", 0), "emotion"
        else:
            zt_n, dt_n, src = st["zt"], st["dt"], "daily_shards>=9.8%"
        if zt_n >= ZT_FLOOD_N:
            events["zt_flood"].append(d)
            day_anoms.append(("zt_flood", f"{zt_n}({src})"))
        if dt_n >= DT_FLOOD_N:
            events["dt_flood"].append(d)
            day_anoms.append(("dt_flood", f"{dt_n}({src})"))
        # 2/5. 行业涨幅与双主线
        ind_avg = {ind: (sum(rs) / len(rs), len(rs)) for ind, rs in st["ind_ret"].items() if len(rs) >= 5}
        top5 = sorted(ind_avg.items(), key=lambda kv: -kv[1][0])[:DUAL_TOP_N]
        surge = [(ind, avg) for ind, (avg, _) in ind_avg.items() if avg >= SURGE_SECTOR_PCT]
        if surge:
            events["sector_surge"].append(d)
            day_anoms.append(("sector_surge",
                              [(i, round(a, 2)) for i, a in sorted(surge, key=lambda x: -x[1])[:5]]))
        resonance = [ind for ind, (avg, _) in top5
                     if avg >= SURGE_SECTOR_PCT and st["ind_zt"].get(ind, 0) >= DUAL_ZT_MIN]
        if len(resonance) >= DUAL_MIN_RESONANCE:
            events["dual_mainline"].append(d)
            day_anoms.append(("dual_mainline", resonance))
        # 3. 龙头大跌
        crashed = []
        for ind, codes in leader_set.items():
            for c in codes:
                cr = st["code_ret"].get(c)
                if cr is not None and cr <= LEADER_DROP_PCT:
                    crashed.append({"code": c, "industry": ind, "ret": round(cr, 2)})
        if crashed:
            events["leader_crash"].append(d)
            day_anoms.append(("leader_crash", crashed[:10]))
        if day_anoms:
            detail[d] = day_anoms
    return events, detail


def main():
    ap = argparse.ArgumentParser(description="行情异动引擎 (日线口径, 含历史条件胜率)")
    ap.add_argument("--asof", default=None, help="判定截止日, 默认指数数据最新日")
    ap.add_argument("--lookback", type=int, default=250, help="历史统计窗口(交易日)")
    ap.add_argument("--index-th", type=float, default=2.0, help="指数异动阈值 ±%%")
    ap.add_argument("--topn", type=int, default=3, help="行业龙头取成交额前 N")
    ap.add_argument("--limit", type=int, default=None,
                    help="抽样股票数上限 (全市场算不起时使用, 输出注明口径)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="输出 JSON, 默认 DEFAULT_OUT ('--out -' 输出 stdout)")
    args = ap.parse_args()

    index_rows = read_index(INDEX_TXT)
    if not index_rows:
        print(json.dumps({"error": f"no index data: {INDEX_TXT}"}),
              file=sys.stderr)
        raise SystemExit(2)
    asof = args.asof or index_rows[-1]["date"]
    index_rows = [r for r in index_rows if r["date"] <= asof]
    idx_asof = index_rows[-1]["date"]

    hist, amt, dates_all, sampled, n_codes, n_universe = load_bulk(asof, args.limit)
    bulk_asof = dates_all[-1] if dates_all else None
    dates = [d for d in dates_all if d <= asof][-args.lookback:]
    if not dates:
        print(json.dumps({"error": "no bulk dates in window"}),
              file=sys.stderr)
        raise SystemExit(2)

    code2ind = load_industry_map()
    emotion = load_emotion(asof)

    # 供 detect_events 使用的全期累计成交额代理 (龙头名单, 见 leader_lookahead_note)
    detect_events.code2ind = code2ind
    detect_events.hist_amt = amt

    per_date = daily_stats(hist, dates, code2ind)
    events, detail = detect_events(per_date, dates, index_rows, emotion, args)

    degraded = ["无分钟线, 全部收盘口径, 盘中异动(炸板/冲高回落)不可见"]
    if sampled:
        degraded.append(f"抽样口径: 按代码序取 {n_codes}/{n_universe} 只 (--limit), 非全市场")
    degraded.append("行业映射仅覆盖 %d/5343 股, 板块为已映射成分等权口径" % len(code2ind))
    if len(emotion) < 10:
        degraded.append(f"emotion/daily.jsonl 仅 {len(emotion)} 行, 涨跌停多数日期降级为日线 >=9.8% 自算")

    asof_day = dates[-1]
    anomalies = []
    for rec in detail.get(asof_day, []):
        atype, ev = rec[0], rec[1]
        sev = "high" if atype in ("index_gap_down", "dt_flood") else "medium"
        anomalies.append({
            "type": atype,
            "severity": sev,
            "evidence": ev,
            "hist_winrate": {
                "T+1": cond_winrate(events[atype], index_rows, 1),
                "T+5": cond_winrate(events[atype], index_rows, 5),
            },
            "direction": ("历史同类异动后 T+1/T+5 大盘收阳条件频率见 hist_winrate; "
                          "为频率描述非点预测, 仅用 T 日前数据"),
        })

    result = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "engine": "market_anomaly_engine v1 (行情异动, 区别于数据质量 anomaly_detector; 新增引擎不改既有文件)",
        "asof": asof,
        "data_asof": {"index": idx_asof, "bulk": bulk_asof, "emotion_rows": len(emotion),
                      "note": "本地数据滞后于当前日期 2026-10-23, 为已知限制"},
        "params": {"lookback": args.lookback, "index_th": args.index_th, "topn": args.topn,
                   "limit": args.limit, "zt_th": ZT_PCT, "zt_flood_n": ZT_FLOOD_N,
                   "dt_flood_n": DT_FLOOD_N, "surge_sector_pct": SURGE_SECTOR_PCT,
                   "leader_drop_pct": LEADER_DROP_PCT},
        "n_stocks": n_codes,
        "n_universe": n_universe,
        "window": {"from": dates[0], "to": asof_day, "n_days": len(dates)},
        "anomalies": anomalies,
        "event_counts_in_window": {k: len(v) for k, v in events.items()},
        "hist_winrate_all_types": {
            k: {"T+1": cond_winrate(v, index_rows, 1), "T+5": cond_winrate(v, index_rows, 5)}
            for k, v in events.items()},
        "leader_lookahead_note": ("龙头名单按截至 asof 全期成交额排序, 对窗口内历史事件的归属判定"
                                  "存在轻微前视; asof 当日检测无此前视。历史胜率仅供参考。"),
        "degraded": degraded,
    }

    txt = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out and args.out != "-":
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(txt + "\n")
        print("->", args.out, file=sys.stderr)
    else:
        print(txt)


if __name__ == "__main__":
    main()
