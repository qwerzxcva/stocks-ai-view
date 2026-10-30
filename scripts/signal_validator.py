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
             combo=True, max_tags_per_bar=4, min_n=30, oos_split=None):
    """逐标签（含两两组合）跑前向收益。

    tag_fn(bars) -> list[str]；bars 已截断到 t，只能看到历史。
    samples: [(code, bars)]，bars 为按日期升序的 dict 列表（含 close）。
    cut_step: 每隔几根取一个切点；1 = 全量（慢但样本多）。
    oos_split: None = 不分段，统计全样本（旧行为）。
        给 0~1 间的小数（如 0.7）时，按每只票切点在时间轴上的先后，把前
        oos_split 段当 in-sample、其余当 out-of-sample：主统计只吃 in-sample，
        另附 `oos_n` / `oos_avg_fwd` / `oos_t` 三个字段，供
        `calibrate(require_oos=True)` 判定信号能否外推。
        分段会让主统计的样本量降到 oos_split 倍，故默认关闭（None）。
    """
    buckets = {}
    oos_buckets = {}

    def _add(key, v, is_is=True):
        if oos_split is None:
            buckets.setdefault(key, []).append(v)
        else:
            (buckets if is_is else oos_buckets).setdefault(key, []).append(v)

    for code, bars in samples:
        n = len(bars)
        if n < min_history + horizon + 1:
            continue
        cuts = list(range(min_history, n - horizon, max(1, cut_step)))
        if not cuts:
            continue
        k = max(1, int(len(cuts) * oos_split)) if oos_split else 0
        for ci, cut in enumerate(cuts):
            is_is = ci < k
            view = bars[:cut]
            fwd = pct(bars[cut + horizon - 1]["close"], view[-1]["close"])
            try:
                tags = tag_fn(view) or []
            except Exception:
                continue  # 单点异常不能污染整张统计表
            for t in set(tags):
                _add(t, fwd, is_is)
            if combo and len(tags) >= 2:
                pri = list(dict.fromkeys(tags))[:max_tags_per_bar]
                for i in range(len(pri)):
                    for j in range(i + 1, len(pri)):
                        _add(f"{pri[i]} & {pri[j]}", fwd, is_is)

    # 先算出所有通过 n 门槛的键的统计量
    stat = {}
    for k, v in buckets.items():
        if len(v) < min_n:
            continue  # n=2 的 t=26 是噪声，不能上桌
        s = stats(v)
        if s:
            stat[k] = s

    # 组合标签若与某条单标签的 (n, avg, win_rate, t) 四项完全同值，说明这对组合
    # 没有筛出任何子集 —— 它和那条单标签是同一个估计量，写两行只是把证据数从 1
    # 说成 2（反转层实测：26 行里 12 行如此）。判据与依据见
    # docs/books/ENGINE-OPTIMIZATION.md 二.1 / 二.5。
    single_sig = {
        (s["n"], round(s["avg"], 3), round(s["win_rate"], 1), round(s["t"], 2))
        for k, s in stat.items() if " & " not in k
    }

    rows = []
    for k, s in stat.items():
        sig = (s["n"], round(s["avg"], 3), round(s["win_rate"], 1), round(s["t"], 2))
        if " & " in k and sig in single_sig:
            continue  # 组合没筛出子集 → 不进表
        row = {"signal": k, "n": s["n"], "avg_fwd": round(s["avg"], 3),
               "win_rate": round(s["win_rate"], 1), "t": round(s["t"], 2)}
        if oos_split is not None:
            so = stats(oos_buckets.get(k) or [])
            row["oos_n"] = so["n"] if so else 0
            row["oos_avg_fwd"] = round(so["avg"], 3) if so else None
            row["oos_t"] = round(so["t"], 2) if so else None
        rows.append(row)
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


def _net_fields(avg, t, cost_bps=(15, 30)):
    """净收益字段：扣掉交易成本后的期望与 t。**不覆盖** avg_fwd / t。

    为什么要单独开字段：表内 avg_fwd 全是毛收益，5 日双边成本约 0.15~0.3%，
    会把 mtr:second_lower_high(+0.28%)这类 edge 吃掉一半以上。用净字段而不是
    直接改 avg_fwd，是为了不动现有消费方（端上 CALIB、云端 confluence_score）。

    t 反推标准误（se = |avg / t|），再算 net_t = (avg - cost) / se；t 为 0 或缺失
    时不写 net_t（没法反推 SE，不编造数值）。
    """
    out = {}
    se = abs(avg / t) if t else None
    for bp in cost_bps:
        cost = bp / 100.0  # bp → 百分点：15bp = 0.15%
        out[f"net_avg_fwd_{bp}bp"] = round(avg - cost, 3)
        if se and se > 0:
            out[f"net_t_{bp}bp"] = round((avg - cost) / se, 2)
    return out

def calibrate(rows, bullish_tags, min_n=120, min_t=1.5,
              require_oos=False, oos_min_t=None, cost_bps=(15, 30)):
    """把检验结果冻成校准表，并标出 trend / contrarian。

    require_oos: True 时只收「样本外同向且仍过门槛」的标签。需要 rows 由
        `validate(..., oos_split=...)` 产出（每行带 oos_n / oos_avg_fwd / oos_t）。
        默认 False = 旧行为（只看全样本 t）。开它会把表砍掉一大截（实测反转层
        17 条显著里只有 7 条 OOS 稳健），故默认关、由调用方显式开。
    cost_bps: 净收益字段的成本档（bp），默认 15/30 = 0.15%/0.30%。
    """
    table = {}
    for r in rows:
        if r["n"] < min_n or abs(r["t"]) < min_t:
            continue
        if require_oos:
            oos_n = r.get("oos_n") or 0
            oos_t = r.get("oos_t")
            oos_avg = r.get("oos_avg_fwd")
            if oos_n < min_n or oos_t is None or oos_avg is None:
                continue  # 样本外样本不够，不得当“已验证”入表
            floor = oos_min_t if oos_min_t is not None else min_t
            if abs(oos_t) < floor:
                continue  # 样本外不够显著
            if (r["avg_fwd"] > 0) != (oos_avg > 0):
                continue  # 反号：表内与表外方向相反，不得入表
        bull = is_bullish_key(r["signal"], bullish_tags)
        same_dir = r["avg_fwd"] > 0 if bull else r["avg_fwd"] < 0
        entry = {
            "n": r["n"], "avg_fwd": r["avg_fwd"], "win_rate": r["win_rate"],
            "t": r["t"], "edge": "trend" if same_dir else "contrarian",
        }
        entry.update(_net_fields(r["avg_fwd"], r["t"], cost_bps))
        # OOS 体检字段：rows 里带就透传（不论 require_oos），方便表内自查
        # 「这条在样本外还成立吗」，也让诊断模式与严格模式共用同一张表的结构。
        if "oos_t" in r:
            entry["oos_n"] = r["oos_n"]
            entry["oos_t"] = r["oos_t"]
            entry["oos_avg_fwd"] = r["oos_avg_fwd"]
        table[r["signal"]] = entry
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
