#!/usr/bin/env python3
"""
morning_pick_engine.py — 早盘推股引擎 v1 (盘前/早盘运行, 终出 3~5 只)

⚠ 数据缺口声明: 分钟线/竞价数据为已知缺口, 当前为【日线级降级实现】。
  数据源适配层已预留: detect_intraday_source() 探测分钟/竞价数据源,
  当前一律返回 None 并标注 TODO —— 接入后仅需在该函数返回数据源,
  打分栈的 intraday 维度即自动启用, 其余逻辑不变。

【用途】早盘运行: 隔夜外围快照 + 前一交易日日线形态, 独立打分栈终出 3~5 只。
  打分覆盖全市场, 推荐输出仅主板 (前缀 600/601/603/605/000/001/002), 只裁输出层。

【独立性声明】本引擎与 tomorrow_candidate_engine.py 零共享代码:
  不 import 对方、不共享指标函数, 维度权重自含于本文件 DEFAULT_WEIGHTS;
  仅共享数据层只读读取 (K 线 / 指数 / universe / 外围快照 / 资金流快照)。

【打分栈】(vote ∈ {-1,0,+1}, strength ∈ [0,1],
  score = 50 + 50 × Σw·vote·strength/Σw × (0.5 + 0.5 × 同向率))
  gap_env      隔夜外围环境: global_markets.json 欧美/亚太隔夜涨跌幅均值
               (文件缺失或过旧 → 该维缺席并标注 skipped)
  breakout     放量突破: 昨收创近 60 日新高 且 量比≥1.5
  pullback     缩量回踩: 上升趋势中(收>MA20>MA60) 昨日缩量(量比<0.7)小阴/十字
  momentum     动量延续: 近 5 日涨幅排名靠前但未过热(<20%)
  vol_energy   量能结构: 近 5 日量能温和放大(量斜率>0)且昨日非巨量滞涨
  moneyflow    资金快照: moneyflow.json 当日主力净流入(快照, 无历史, 仅加分项)

【数据来源】(只读)
  日线: ~/workspace/stocks-data-kline/data/bulk/day_qfq_{shard}.csv (至 2026-10-16)
  外围: ~/workspace/stocks-ai-view/data/global_markets.json (快照, 缺则跳过)
  资金: ~/workspace/stocks-ai-view/data/moneyflow.json (快照, 无历史序列)
  名单: ~/workspace/stocks-data-kline/data/bulk/universe.csv

【新增声明】新增文件 (2026-10-23), 不修改任何既有文件。
  只用 --date(默认数据最新交易日) 及之前数据, 无前视。不打印任何 token/凭据。

CLI:
  python3 morning_pick_engine.py                  # 数据最新日, top 5
  python3 morning_pick_engine.py --date 2026-10-16 --top 3
  python3 morning_pick_engine.py --limit 800 --out picks.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, "..", "..", "data", "engines", "morning_pick_engine.json"))
KLINE_BASE = os.environ.get(
    "STOCKS_DATA_KLINE",
    os.path.expanduser("~/workspace/stocks-data-kline/data"))
BULK_DIR = os.path.join(KLINE_BASE, "bulk")
AI_VIEW_DATA = os.environ.get(
    "STOCKS_AI_VIEW_DATA",
    os.path.abspath(os.path.join(HERE, "..", "..", "data")))

ENGINE = "morning_pick_engine v1 (daily-degraded)"
MAIN_BOARD_PREFIX = ("600", "601", "603", "605", "000", "001", "002")
SHARDS = ["00", "01", "02", "03", "04", "05", "06", "07", "08", "09",
          "20", "30", "60", "68"]
TAIL = 80

# 独立打分栈权重 (与 tomorrow_candidate_engine 零共享)
DEFAULT_WEIGHTS = {
    "gap_env": 0.8, "breakout": 1.2, "pullback": 1.2,
    "momentum": 1.0, "vol_energy": 1.0, "moneyflow": 0.6,
}


# ─── 数据源适配层 (预留) ────────────────────────────────────────
def detect_intraday_source():
    """探测分钟线/竞价数据源。当前为已知缺口, 一律返回 None。

    TODO: 接入分钟线仓库或竞价快照后, 在此返回 {"path": ..., "granularity": ...},
    打分栈将自动启用 intraday 维度 (竞价量比、开盘位置、首 30 分钟资金流)。
    """
    return None


# ─── 数据层 (只读) ──────────────────────────────────────────────
def load_universe():
    path = os.path.join(BULK_DIR, "universe.csv")
    names = {}
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("|")
            if len(parts) >= 2:
                names[parts[0]] = parts[1]
    return names


def load_global_markets():
    """隔夜外围快照。缺失/损坏返回 (None, 'missing')。"""
    path = os.path.join(AI_VIEW_DATA, "global_markets.json")
    if not os.path.exists(path):
        return None, "missing"
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d, d.get("ts")
    except Exception:
        return None, "corrupt"


def load_moneyflow_snapshot():
    path = os.path.join(AI_VIEW_DATA, "moneyflow.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def scan_shards(asof=None, limit=None):
    """流式扫描, 每票保留尾部 TAIL 根, 只取 ≤asof 数据(无前视)。"""
    data = {}
    for shard in SHARDS:
        path = os.path.join(BULK_DIR, f"day_qfq_{shard}.csv")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("|")
                if len(parts) != 7:
                    continue
                if asof and parts[1] > asof:
                    continue
                lst = data.setdefault(parts[0], [])
                lst.append((parts[1], float(parts[2]), float(parts[3]),
                            float(parts[4]), float(parts[5]), float(parts[6])))
                if len(lst) > TAIL:
                    del lst[:len(lst) - TAIL]
    sampled = False
    if limit and len(data) > limit:
        codes = sorted(data)
        step = len(codes) / limit
        keep = {codes[int(i * step)] for i in range(limit)}
        data = {c: v for c, v in data.items() if c in keep}
        sampled = True
    return data, sampled


# ─── 独立打分栈 ─────────────────────────────────────────────────
def _sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def vote_gap_env(gm):
    """隔夜外围: 非 A 股指数涨跌幅均值。"""
    if not gm:
        return None
    idx = gm.get("indices") or []
    chgs = [float(i["chg_pct"]) for i in idx
            if i.get("market") not in ("A股",) and i.get("chg_pct") is not None]
    if not chgs:
        return None
    avg = sum(chgs) / len(chgs)
    if avg >= 0.8:
        return (1, min(avg / 2, 1.0), f"隔夜外围均涨 {avg:+.2f}% 偏暖")
    if avg <= -0.8:
        return (-1, min(abs(avg) / 2, 1.0), f"隔夜外围均跌 {avg:+.2f}% 承压")
    return (0, 0.2, f"隔夜外围 {avg:+.2f}% 平稳")


def vote_breakout(bars):
    """放量突破: 昨收创近 60 日新高且量比≥1.5。"""
    if len(bars) < 61:
        return None
    c = bars[-1][2]
    hi60 = max(b[3] for b in bars[-61:-1])
    vols = [b[5] for b in bars]
    v20 = sum(vols[-21:-1]) / 20 or 1.0
    vr = vols[-1] / v20
    chg = (bars[-1][2] - bars[-2][2]) / bars[-2][2] * 100 if bars[-2][2] else 0
    if c >= hi60 and vr >= 1.5:
        return (1, min(vr / 3 + 0.4, 1.0),
                f"放量突破60日新高 {hi60:.2f} (量比 {vr:.1f})")
    if c >= hi60 and vr < 0.8:
        return (0, 0.3, f"创新高但缩量(量比 {vr:.2f}), 动能存疑")
    if chg < -3 and vr >= 2:
        return (-1, 0.6, f"放量重挫 {chg:+.1f}%, 早盘回避")
    return (0, 0.2, "未突破")


def vote_pullback(bars):
    """缩量回踩: 收>MA20>MA60 上升趋势, 昨日缩量小阴/十字。"""
    closes = [b[2] for b in bars]
    m20, m60 = _sma(closes, 20), _sma(closes, 60)
    if not m20 or not m60 or len(bars) < 21:
        return None
    vols = [b[5] for b in bars]
    v20 = sum(vols[-21:-1]) / 20 or 1.0
    vr = vols[-1] / v20
    chg = (bars[-1][2] - bars[-2][2]) / bars[-2][2] * 100 if bars[-2][2] else 0
    uptrend = closes[-1] > m20 > m60
    if uptrend and vr <= 0.7 and -2.5 <= chg <= 0.5:
        return (1, min((0.7 - vr) + 0.4, 1.0),
                f"上升趋势缩量回踩(量比 {vr:.2f}, {chg:+.1f}%), 低吸窗口")
    if not uptrend and closes[-1] < m60:
        return (-1, 0.4, "收于 MA60 下方, 趋势走弱")
    return (0, 0.2, "无回踩结构")


def vote_momentum(bars):
    """近 5 日动量: 温和上涨加分, 过热(>20%)反向。"""
    if len(bars) < 6 or not bars[-6][2]:
        return None
    chg5 = (bars[-1][2] - bars[-6][2]) / bars[-6][2] * 100
    if 3 <= chg5 <= 15:
        return (1, min(chg5 / 15, 1.0), f"5日动量 {chg5:+.1f}% 温和走强")
    if chg5 > 20:
        return (-1, 0.6, f"5日 {chg5:+.1f}% 过热, 早盘追高风险")
    if chg5 < -8:
        return (-1, 0.5, f"5日 {chg5:+.1f}% 急跌, 不接飞刀")
    return (0, 0.2, f"5日动量 {chg5:+.1f}%")


def vote_vol_energy(bars):
    """量能结构: 近 5 日量斜率温和放大 且 昨日非巨量滞涨。"""
    if len(bars) < 22:
        return None
    vols = [b[5] for b in bars]
    v5 = sum(vols[-5:]) / 5
    v20 = sum(vols[-20:]) / 20 or 1.0
    chg = (bars[-1][2] - bars[-2][2]) / bars[-2][2] * 100 if bars[-2][2] else 0
    ratio = v5 / v20
    if 1.05 <= ratio <= 2.0:
        return (1, min((ratio - 1) / 1, 1.0) * 0.7 + 0.2,
                f"量能温和放大(5日/20日 {ratio:.2f})")
    if ratio > 3 and abs(chg) < 1:
        return (-1, 0.6, f"巨量滞涨(5日/20日 {ratio:.2f}), 分歧出货嫌疑")
    if ratio < 0.6:
        return (0, 0.3, f"量能萎缩({ratio:.2f}), 观望")
    return (0, 0.2, f"量能 {ratio:.2f}")


def vote_moneyflow(mf, bare):
    """资金流快照(无历史): 当日主力净流入方向, 仅加分项。"""
    if not mf:
        return None
    stocks = mf.get("stocks") or {}
    rec = stocks.get(bare)
    if not rec:
        return None
    net = rec.get("main_net_today")
    if net is None:
        return None
    if net > 0:
        return (1, 0.4, f"快照主力净流入 {net / 1e4:+.0f} 万")
    if net < 0:
        return (-1, 0.4, f"快照主力净流出 {net / 1e4:+.0f} 万")
    return (0, 0.1, "快照资金流中性")


def score_stock(bars, weights, gm, mf, bare):
    votes = []
    for name, v in (("gap_env", vote_gap_env(gm)),
                    ("breakout", vote_breakout(bars)),
                    ("pullback", vote_pullback(bars)),
                    ("momentum", vote_momentum(bars)),
                    ("vol_energy", vote_vol_energy(bars)),
                    ("moneyflow", vote_moneyflow(mf, bare))):
        if v:
            votes.append({"indicator": name, "vote": v[0],
                          "strength": round(v[1], 3), "why": v[2]})
    if not votes:
        return None
    num = den = 0.0
    bull = bear = 0
    for v in votes:
        w = weights.get(v["indicator"], 1.0)
        num += w * v["vote"] * v["strength"]
        den += w
        bull += v["vote"] > 0
        bear += v["vote"] < 0
    if not den:
        return None
    base = num / den
    same = max(bull, bear) / len(votes)
    score = 50 + 50 * base * (0.5 + 0.5 * same)
    return round(max(0.0, min(100.0, score)), 2), votes


def main():
    ap = argparse.ArgumentParser(description=ENGINE + " — 早盘推股 (日线级降级实现)")
    ap.add_argument("--date", help="交易日 YYYY-MM-DD (默认数据最新日)")
    ap.add_argument("--top", type=int, default=5, help="输出只数, 3~5 (默认 5)")
    ap.add_argument("--limit", type=int, help="扫描规模上限(超出步进抽样)")
    ap.add_argument("--weights", help='维度权重 JSON, 如 \'{"breakout":1.5}\'')
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="输出 JSON 路径(默认 DEFAULT_OUT) ('--out -' 输出 stdout)")
    args = ap.parse_args()
    args.top = max(3, min(5, args.top))

    weights = dict(DEFAULT_WEIGHTS)
    if args.weights:
        try:
            weights.update({k: float(v) for k, v in
                            json.loads(args.weights).items()})
        except Exception as e:
            print(f"weights JSON 解析失败: {e}", file=sys.stderr)
            sys.exit(2)

    intraday = detect_intraday_source()
    names = load_universe()
    gm, gm_ts = load_global_markets()
    mf = load_moneyflow_snapshot()
    data, sampled = scan_shards(asof=args.date, limit=args.limit)

    dates = sorted({bars[-1][0] for bars in data.values() if bars})
    data_asof = dates[-1] if dates else None

    rows = []
    for code, bars in data.items():
        if len(bars) < 61:
            continue
        bare = code[2:]
        res = score_stock(bars, weights, gm, mf, bare)
        if not res:
            continue
        score, votes = res
        rows.append({"code": code, "name": names.get(code), "score": score,
                     "close": bars[-1][2], "date": bars[-1][0], "votes": votes,
                     "main_board": bare.startswith(MAIN_BOARD_PREFIX)})
    rows.sort(key=lambda r: -r["score"])
    main_rows = [r for r in rows if r["main_board"]]
    picks = main_rows[:args.top]
    for r in picks:
        r.pop("main_board", None)

    result = {
        "engine": ENGINE,
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "data_asof": data_asof,
        "trade_date": args.date or data_asof,
        "weights": weights,
        "scanned": len(data),
        "sampling": ("systematic step-sample limit=%d" % args.limit)
                    if sampled else "full-market",
        "output_filter": "main_board_only(600/601/603/605/000/001/002)",
        "degraded": {
            "intraday_source": intraday,
            "note": "分钟线/竞价数据为已知缺口, 当前为日线级降级实现; "
                    "detect_intraday_source() 返回 None, 数据源适配层已预留(TODO)",
            "gap_env": ("ok ts=%s" % gm_ts) if gm else
                       "skipped: global_markets.json %s" % gm_ts,
            "moneyflow": "ok (snapshot, no history)" if mf else
                         "skipped: moneyflow.json missing",
        },
        "picks": picks,
    }
    text = json.dumps(result, ensure_ascii=False, indent=1)
    # 复盘快照落盘: data/predictions/morning_pick_<date>.json (带 ts 只增,
    # 当日重跑不覆盖历史锚点), 供 feedback_updater 回查 T+N 收益
    if data_asof and picks:
        snap_dir = os.path.join(AI_VIEW_DATA, "predictions")
        snap = os.path.join(snap_dir, f"morning_pick_{data_asof}.json")
        if not os.path.exists(snap):
            try:
                os.makedirs(snap_dir, exist_ok=True)
                with open(snap, "w", encoding="utf-8") as f:
                    json.dump({"engine": "morning_pick",
                               "ts": result["ts"], "data_asof": data_asof,
                               "top": picks},
                              f, ensure_ascii=False, indent=1)
                    f.write("\n")
            except OSError as e:
                print(f"snapshot write failed: {e}", file=sys.stderr)
    if args.out and args.out != "-":
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"written: {args.out} ({len(picks)} picks, scanned {len(data)})",
              file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
