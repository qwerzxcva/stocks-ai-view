#!/usr/bin/env python3
"""
tomorrow_candidate_engine.py — 明日候选引擎 v1 (盘后运行, 全市场打分 → 主板候选池 30)

【用途】盘后基于当日收盘全市场日线做精简多维打分, 输出明日候选池(默认 30 只)。
  打分覆盖全市场; 推荐输出仅主板 (前缀 600/601/603/605/000/001/002), 只裁输出层。

【与 engine_v5 的关系】思路兼容 (多维度方向票 × 强度 → 加权归一 × 共振系数),
  但为独立实现: 本文件自含全部指标计算, 不 import engine_v5 (它扫全市场过重),
  也不 import morning_pick_engine。维度权重写死为默认 + CLI 可调 (--weights JSON)。

【评分维度】(每维 vote ∈ {-1,0,+1}, strength ∈ [0,1])
  ma         均线结构: 收盘 vs MA20/MA60 乖离与排列
  rsi        RSI(14): 50~70 强而未热为多头, >80 过热反向
  macd       MACD(12,26,9): DIF/DEA 方向与金叉死叉
  volume     量能: 当日量 vs MA20 量, 放量上涨/缩量下跌加分
  sector     板块共振: industry_map_em.json 行业近 5 日均涨幅(由成员股日线自算)
  rel_strength 近 20 日相对强度: 个股涨幅 − 上证综指同期涨幅
  score = 50 + 50 × (Σw·vote·strength / Σw) × (0.6 + 0.4 × 同向率)   ∈ [0,100]

【数据来源】(只读)
  日线: ~/workspace/stocks-data-kline/data/bulk/day_qfq_{shard}.csv (至 2026-10-16)
  指数: ~/workspace/stocks-data-kline/data/index/history/sh000001.txt (至 2026-10-09)
  名单: ~/workspace/stocks-data-kline/data/bulk/universe.csv
  行业: ~/workspace/stocks-ai-view/data/industry_map_em.json (1050 只覆盖, 未覆盖个股该维缺席)

【已知限制/降级】板块共振的行业映射仅覆盖 1050 只, 未覆盖个股 sector 维度缺席
  (分母自动剔除); 资金流历史为已知缺口, 本引擎不使用资金流维度。
  --limit 控制扫描规模: 抽样时按 universe 顺序步进抽样(systematic sample),
  输出注明 sampling 口径。

【新增声明】新增文件 (2026-10-23), 不修改任何既有文件, 与 morning_pick_engine 零共享。
  只用 --asof(默认数据最新日) 及之前数据, 无前视。不打印任何 token/凭据。

CLI:
  python3 tomorrow_candidate_engine.py                    # 全市场扫描
  python3 tomorrow_candidate_engine.py --limit 500        # 抽样 500 只
  python3 tomorrow_candidate_engine.py --top 30 --out cand.json
  python3 tomorrow_candidate_engine.py --weights '{"rsi":1.5,"volume":0.8}'
"""
import argparse
import json
import math
import os
import random
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, "..", "..", "data", "engines", "tomorrow_candidate_engine.json"))
KLINE_BASE = os.environ.get(
    "STOCKS_DATA_KLINE",
    os.path.expanduser("~/workspace/stocks-data-kline/data"))
BULK_DIR = os.path.join(KLINE_BASE, "bulk")
INDEX_DIR = os.path.join(KLINE_BASE, "index", "history")
AI_VIEW_DATA = os.environ.get(
    "STOCKS_AI_VIEW_DATA",
    os.path.abspath(os.path.join(HERE, "..", "..", "data")))

ENGINE = "tomorrow_candidate_engine v1"
MAIN_BOARD_PREFIX = ("600", "601", "603", "605", "000", "001", "002")
SHARDS = ["00", "01", "02", "03", "04", "05", "06", "07", "08", "09",
          "20", "30", "60", "68"]

DEFAULT_WEIGHTS = {
    "ma": 1.0, "rsi": 1.0, "macd": 1.0, "volume": 1.0,
    "sector": 1.0, "rel_strength": 1.0,
}
TAIL = 120  # 每票打分只需尾部 120 根


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


def load_industry_map():
    path = os.path.join(AI_VIEW_DATA, "industry_map_em.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("map") or {}
    except Exception:
        return {}


def load_index_tail(code="sh000001", tail=40):
    path = os.path.join(INDEX_DIR, f"{code}.txt")
    if not os.path.exists(path):
        return []
    closes = []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("|")
            if len(parts) == 7:
                try:
                    closes.append((parts[1], float(parts[3])))
                except ValueError:
                    pass
    return closes[-tail:]


def scan_shards(asof=None, limit=None, seed=42):
    """流式扫描 14 分片, 每票仅保留尾部 TAIL 根。返回 {full_code: bars_tail}。"""
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
                    continue  # 无前视: 跳过 asof 之后的数据
                code = parts[0]
                lst = data.setdefault(code, [])
                lst.append((parts[1], float(parts[2]), float(parts[3]),
                            float(parts[4]), float(parts[5]), float(parts[6])))
                if len(lst) > TAIL:
                    del lst[:len(lst) - TAIL]
    if limit and len(data) > limit:
        codes = sorted(data)
        step = len(codes) / limit
        keep = {codes[int(i * step)] for i in range(limit)}
        data = {c: v for c, v in data.items() if c in keep}
        data["__sampled__"] = True  # 哨兵, 输出前剔除
    return data


# ─── 指标 (独立实现, 不依赖 engine_v5) ─────────────────────────
def _sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = l = 0.0
    for i in range(len(closes) - n, len(closes)):
        d = closes[i] - closes[i - 1]
        g += max(d, 0.0)
        l += max(-d, 0.0)
    if l == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + (g / n) / (l / n))


def _ema_series(closes, n):
    k = 2 / (n + 1)
    e = closes[0]
    out = []
    for c in closes:
        e = c * k + e * (1 - k)
        out.append(e)
    return out


def _macd(closes):
    if len(closes) < 35:
        return None, None
    e12 = _ema_series(closes, 12)
    e26 = _ema_series(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = _ema_series(dif, 9)
    return dif, dea


def vote_ma(closes):
    c = closes[-1]
    m20, m60 = _sma(closes, 20), _sma(closes, 60)
    if not m20 or not m60:
        return None
    dev20 = (c - m20) / m20 * 100
    if c > m20 > m60 and dev20 < 15:
        return (1, min(max(dev20, 1) / 10, 1.0),
                f"多头排列(收>{m20:.2f}>{m60:.2f}), 乖离 {dev20:+.1f}%")
    if c < m20 < m60:
        return (-1, min(abs(dev20) / 10, 1.0), f"空头排列, 乖离 {dev20:+.1f}%")
    return (0, 0.2, f"均线缠绕, 乖离MA20 {dev20:+.1f}%")


def vote_rsi(closes):
    r = _rsi(closes)
    if r is None:
        return None
    if 50 <= r <= 70:
        return (1, (r - 50) / 20 * 0.8 + 0.2, f"RSI {r:.1f} 强而未热")
    if r > 80:
        return (-1, min((r - 80) / 20 + 0.4, 1.0), f"RSI {r:.1f} 过热")
    if r < 30:
        return (-1, min((30 - r) / 30 + 0.3, 1.0), f"RSI {r:.1f} 超卖弱势")
    if r < 45:
        return (-1, 0.3, f"RSI {r:.1f} 偏弱")
    return (0, 0.2, f"RSI {r:.1f} 中性")


def vote_macd(closes):
    dif, dea = _macd(closes)
    if not dif:
        return None
    d, e, pd_, pe = dif[-1], dea[-1], dif[-2], dea[-2]
    if pd_ <= pe and d > e:
        return (1, 0.9, "MACD 金叉")
    if d > e and d > 0:
        return (1, 0.5, "MACD 零上多头")
    if pd_ >= pe and d < e:
        return (-1, 0.9, "MACD 死叉")
    if d < e and d < 0:
        return (-1, 0.5, "MACD 零下空头")
    return (0, 0.2, "MACD 收敛")


def vote_volume(bars):
    if len(bars) < 21:
        return None
    vols = [b[5] for b in bars]
    v20 = sum(vols[-21:-1]) / 20 or 1.0
    vr = vols[-1] / v20
    chg = (bars[-1][2] - bars[-2][2]) / bars[-2][2] * 100 if bars[-2][2] else 0.0
    if vr >= 1.8 and chg > 1.5:
        return (1, min(vr / 4 + 0.3, 1.0), f"放量上涨(量比 {vr:.1f}, 涨 {chg:+.1f}%)")
    if vr >= 1.8 and chg < -1.5:
        return (-1, min(vr / 4 + 0.3, 1.0), f"放量下跌(量比 {vr:.1f}, 跌 {chg:+.1f}%)")
    if vr <= 0.6 and chg < 0:
        return (1, 0.3, f"缩量回调(量比 {vr:.2f}), 抛压衰减")
    return (0, 0.2, f"量比 {vr:.2f} 平量")


def vote_sector(ind_chg5):
    """ind_chg5: 所属行业近 5 日均涨幅(%), None 则该维缺席。"""
    if ind_chg5 is None:
        return None
    if ind_chg5 >= 3.0:
        return (1, min(ind_chg5 / 10, 1.0), f"行业近5日 {ind_chg5:+.1f}% 领涨共振")
    if ind_chg5 <= -3.0:
        return (-1, min(abs(ind_chg5) / 10, 1.0), f"行业近5日 {ind_chg5:+.1f}% 领跌")
    return (0, 0.2, f"行业近5日 {ind_chg5:+.1f}% 中性")


def vote_rel_strength(bars, index_closes):
    """近 20 日个股涨幅 − 指数同期涨幅。

    指数数据常滞后个股若干交易日: 以指数最后交易日为对齐锚点,
    个股与指数均取该锚点向前 20 日, 避免日期错位导致该维缺席。"""
    if len(bars) < 21 or len(index_closes) < 21:
        return None
    anchor = index_closes[-1][0]  # 指数最后交易日
    win = [b for b in bars if b[0] <= anchor]
    if len(win) < 21:
        return None
    idx20 = [c for d, c in index_closes if d <= anchor]
    if len(idx20) < 21:
        return None
    if not win[-21][2] or not idx20[-21]:
        return None
    stock_chg = (win[-1][2] - win[-21][2]) / win[-21][2] * 100
    idx_chg = (idx20[-1] - idx20[-21]) / idx20[-21] * 100
    rel = stock_chg - idx_chg
    if rel >= 5:
        return (1, min(rel / 15, 1.0), f"20日相对强度 {rel:+.1f}% 显著强于大盘")
    if rel <= -5:
        return (-1, min(abs(rel) / 15, 1.0), f"20日相对强度 {rel:+.1f}% 弱于大盘")
    return (0, 0.2, f"20日相对强度 {rel:+.1f}%")


def score_stock(bars, weights, ind_chg5, index_closes):
    """score = 50 + 50 × (Σw·vote·strength / Σw) × (0.6 + 0.4 × 同向率)"""
    closes = [b[2] for b in bars]
    votes = []
    for fn, arg in ((vote_ma, None), (vote_rsi, None), (vote_macd, None)):
        v = fn(closes)
        if v:
            votes.append({"indicator": fn.__name__[5:], "vote": v[0],
                          "strength": round(v[1], 3), "why": v[2]})
    for name, v in (("volume", vote_volume(bars)),
                    ("sector", vote_sector(ind_chg5)),
                    ("rel_strength", vote_rel_strength(bars, index_closes))):
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
    base = num / den if den else 0.0
    same = max(bull, bear) / len(votes)
    score = 50 + 50 * base * (0.6 + 0.4 * same)
    return round(max(0.0, min(100.0, score)), 2), votes


# ─── 主流程 ─────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=ENGINE + " — 盘后全市场打分, 主板候选池")
    ap.add_argument("--asof", help="截止日期 YYYY-MM-DD (默认数据最新日)")
    ap.add_argument("--limit", type=int, help="扫描规模上限(超出按 universe 步进抽样)")
    ap.add_argument("--top", type=int, default=30, help="输出候选数(默认 30)")
    ap.add_argument("--weights", help='维度权重 JSON, 如 \'{"rsi":1.5}\'')
    ap.add_argument("--all-boards", action="store_true",
                    help="不裁主板, 输出全市场 top(默认只输出主板)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="输出 JSON 路径(默认 DEFAULT_OUT) ('--out -' 输出 stdout)")
    args = ap.parse_args()

    weights = dict(DEFAULT_WEIGHTS)
    if args.weights:
        try:
            weights.update({k: float(v) for k, v in
                            json.loads(args.weights).items()})
        except Exception as e:
            print(f"weights JSON 解析失败: {e}", file=sys.stderr)
            sys.exit(2)

    names = load_universe()
    ind_map = load_industry_map()
    index_closes = load_index_tail("sh000001", 40)
    data = scan_shards(asof=args.asof, limit=args.limit)
    sampled = bool(data.pop("__sampled__", False))

    # 数据最新交易日(全市场口径)
    all_dates = sorted({b[0] for bars in data.values() for b in bars[-1:]})
    data_asof = all_dates[-1] if all_dates else None

    # 行业近 5 日涨幅(由成员股日线自算): 先收集每只票 5 日涨幅
    chg5 = {}
    for code, bars in data.items():
        if len(bars) >= 6 and bars[-6][2]:
            chg5[code] = (bars[-1][2] - bars[-6][2]) / bars[-6][2] * 100
    # 行业近 5 日涨幅: 反向索引 bare→code 一次建表, 避免 O(n_ind × n_stocks)
    code_by_bare = {}
    for code in chg5:
        code_by_bare[code[2:]] = code
    ind_groups = {}
    for bare, ind in ind_map.items():
        code = code_by_bare.get(bare)
        if code:
            ind_groups.setdefault(ind, []).append(chg5[code])
    ind_chg5 = {k: sum(v) / len(v) for k, v in ind_groups.items() if v}

    rows = []
    for code, bars in data.items():
        if len(bars) < 60:
            continue
        bare = code[2:]
        res = score_stock(bars, weights, ind_chg5.get(ind_map.get(bare)),
                          index_closes)
        if not res:
            continue
        score, votes = res
        rows.append({
            "code": code, "name": names.get(code),
            "score": score,
            "close": bars[-1][2], "date": bars[-1][0],
            "chg_pct": round((bars[-1][2] - bars[-2][2]) / bars[-2][2] * 100, 2)
            if bars[-2][2] else None,
            "votes": votes,
            "main_board": bare.startswith(MAIN_BOARD_PREFIX),
        })
    rows.sort(key=lambda r: -r["score"])
    if not args.all_boards:
        rows = [r for r in rows if r["main_board"]]
    picks = rows[:args.top]
    for r in picks:
        r.pop("main_board", None)

    result = {
        "engine": ENGINE,
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "data_asof": data_asof,
        "index_asof": index_closes[-1][0] if index_closes else None,
        "asof_param": args.asof,
        "weights": weights,
        "scanned": len(data),
        "scored": len(rows) if args.all_boards else None,
        "sampling": ("systematic step-sample limit=%d" % args.limit)
                    if sampled else "full-market",
        "output_filter": "all_boards" if args.all_boards
                         else "main_board_only(600/601/603/605/000/001/002)",
        "candidates": picks,
    }
    text = json.dumps(result, ensure_ascii=False, indent=1)
    # 复盘快照落盘: data/predictions/tomorrow_candidate_<date>.json (带 ts 只增,
    # 当日重跑不覆盖历史锚点), 供 feedback_updater 回查 T+N 收益
    if data_asof and picks:
        snap_dir = os.path.join(AI_VIEW_DATA, "predictions")
        snap = os.path.join(snap_dir, f"tomorrow_candidate_{data_asof}.json")
        if not os.path.exists(snap):
            try:
                os.makedirs(snap_dir, exist_ok=True)
                with open(snap, "w", encoding="utf-8") as f:
                    json.dump({"engine": "tomorrow_candidate",
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
