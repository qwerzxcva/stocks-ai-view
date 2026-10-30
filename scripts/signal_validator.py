#!/usr/bin/env python3
"""signal_validator.py — 所有引擎共用的「前向收益检验 + 校准表」框架。

为什么要独立成模块：结构层、威科夫层、交易管理层、题材层都要回答同一个问题——
「我这条标签到底有没有用、方向对不对」。以前每个引擎各写一遍，阈值和口径迟早分叉，
最后就会出现 A 引擎说 t=2.3、B 引擎说 t=-1.1 而没人知道谁算错了。

三条纪律（写在代码里，别只写文档）：
1. **只用 t 及之前的数据**：tag_fn 收到的 bars 已截断到 t，函数内部不允许看后面。
2. **没有校准就不给方向**：calibrate() 只放行 n ≥ min_n 且 |t| ≥ min_t 的标签。
3. **方向来自数据，不来自书**：edge 标签（trend / contrarian）由实测符号决定。

用法：
    import signal_validator as SV
    rows = SV.validate(tag_fn, samples, horizon=5)          # samples: [(code, bars)]
    table = SV.calibrate(rows, bullish_tags=BULLISH, min_n=120, min_t=1.5)
    SV.write_calibration(path, table, meta={...})
"""
import json
import math
import os


# ─────────────────────────── 统计基元 ───────────────────────────
def pct(now, prev):
    if not prev:
        return 0.0
    return (now / prev - 1.0) * 100.0


def stats(xs):
    """返回 n / mean / 胜率 / t 值。空样本返回 None，不返回 0 假装有数据。"""
    n = len(xs)
    if n < 2:
        return None
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    sd = math.sqrt(var)
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    win = 100.0 * sum(1 for x in xs if x > 0) / n
    return {"n": n, "avg": mean, "win_rate": win, "t": t}


# ─────────────────────────── 通用检验 ───────────────────────────
def validate(tag_fn, samples, horizon=5, min_history=120, cut_step=1,
             combo=True, max_tags_per_bar=4, min_n=30):
    """逐标签（含两两组合）跑前向收益。

    tag_fn(bars) -> list[str]；bars 已截断到 t，只能看到历史。
    samples: [(code, bars)]，bars 为按日期升序的 dict 列表（含 close）。
    cut_step: 每隔几根取一个切点；1 = 全量（慢但样本多）。
    """
    buckets = {}

    def _add(key, v):
        buckets.setdefault(key, []).append(v)

    for code, bars in samples:
        n = len(bars)
        if n < min_history + horizon + 1:
            continue
        for cut in range(min_history, n - horizon, max(1, cut_step)):
            view = bars[:cut]
            fwd = pct(bars[cut + horizon - 1]["close"], view[-1]["close"])
            try:
                tags = tag_fn(view) or []
            except Exception:
                continue  # 单点异常不能污染整张统计表
            for t in set(tags):
                _add(t, fwd)
            if combo and len(tags) >= 2:
                pri = list(dict.fromkeys(tags))[:max_tags_per_bar]
                for i in range(len(pri)):
                    for j in range(i + 1, len(pri)):
                        _add(f"{pri[i]} & {pri[j]}", fwd)

    rows = []
    for k, v in buckets.items():
        if len(v) < min_n:
            continue  # n=2 的 t=26 是噪声，不能上桌
        s = stats(v)
        if not s:
            continue
        rows.append({"signal": k, "n": s["n"], "avg_fwd": round(s["avg"], 3),
                     "win_rate": round(s["win_rate"], 1), "t": round(s["t"], 2)})
    rows.sort(key=lambda r: -abs(r["t"]))
    return rows


# ─────────────────────────── 校准表 ───────────────────────────
def is_bullish_key(key, bullish_tags):
    """组合键按 ` & ` 切 token 精确比对。

    必须精确：`chan:third_sell` 含子串 `chan:third`，子串判断会把三卖当多头，
    方向直接反掉——这个坑在结构层踩过一次，别再踩。
    """
    toks = [t.strip() for t in key.split("&")]
    return any(t in bullish_tags for t in toks)


def calibrate(rows, bullish_tags, min_n=120, min_t=1.5):
    """把检验结果冻成校准表，并标出 trend / contrarian。"""
    table = {}
    for r in rows:
        if r["n"] < min_n or abs(r["t"]) < min_t:
            continue
        bull = is_bullish_key(r["signal"], bullish_tags)
        same_dir = r["avg_fwd"] > 0 if bull else r["avg_fwd"] < 0
        table[r["signal"]] = {
            "n": r["n"], "avg_fwd": r["avg_fwd"], "win_rate": r["win_rate"],
            "t": r["t"], "edge": "trend" if same_dir else "contrarian",
        }
    return table


def write_calibration(path, table, meta=None, horizon=None):
    """写校准表。

    horizon 会写进 meta.horizon_days：表里只存 avg_fwd，不注明是几日口径的话，
    下游只能靠猜（structure 旧表用的是 avg_fwd_5d 键，就是口径分叉的后果）。
    """
    meta = dict(meta or {})
    if horizon is not None:
        meta.setdefault("horizon_days", horizon)
    doc = {"engine": "signal_validator/calibration", "meta": meta, "table": table}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)  # 原子替换：半截文件会让下游读到空表
    return doc


def load_calibration(path, min_n=120, min_t=1.5):
    """读校准表并再做一次阈值过滤（防止有人手改文件放宽门槛）。"""
    if not os.path.exists(path):
        return {}
    try:
        d = json.load(open(path))
    except Exception:
        return {}
    return {k: v for k, v in (d.get("table") or {}).items()
            if v.get("n", 0) >= min_n and abs(v.get("t", 0)) >= min_t}


def pick_best(table, tags, min_n=120, min_t=1.5):
    """在一组标签里挑实测最强的那个（组合优先，其次单标签）。

    返回 None = 这些标签没有实测支持。**宁可不给结论，也不给编的分数。**
    """
    if not table or not tags:
        return None
    cands = []
    uniq = list(dict.fromkeys(tags))
    for i, a in enumerate(uniq):
        for b in uniq[i + 1:]:
            for key in (f"{a} & {b}", f"{b} & {a}"):
                if key in table:
                    cands.append((key, table[key]))
    for a in uniq:
        if a in table:
            cands.append((a, table[a]))
    cands = [(k, v) for k, v in cands
             if v.get("n", 0) >= min_n and abs(v.get("t", 0)) >= min_t]
    if not cands:
        return None
    key, best = max(cands, key=lambda kv: abs(kv[1]["t"]))
    return {"basis": key, **{k: best[k] for k in ("n", "t", "avg_fwd", "win_rate", "edge")}}


def print_rows(rows, limit=40):
    print(f"{'signal':46} {'n':>6} {'fwd%':>7} {'win%':>6} {'t':>6}")
    for r in rows[:limit]:
        print(f"{r['signal'][:46]:46} {r['n']:>6} {r['avg_fwd']:>7.2f} {r['win_rate']:>6.1f} {r['t']:>6.2f}")
