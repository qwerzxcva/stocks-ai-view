#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R3 评估: 读 r3_intermediate/orch/*.json 榜单, 流式建 close 序列并计算 T+1/T+5。

口径(与 phase3_baseline_final.py 修正版一致):
  - 主分片(00-09/20/30/60/68)优先, 辅助片(expand/extra/recent/stockdb)仅补主片
    缺失的 (code,date); 辅助片支持 | 与 , 两种分隔(与基线脚本一致)。
  - 收益: T(asof 当日)收盘 → T+N 收盘; asof 为真实交易日, 个股当日无 bar 丢弃计数。
  - 基准: 同日全体主分片股票等权(均值/上涨占比)。
输出: reports/r3_intermediate/eval.json
"""
import glob
import json
import math
import os
from collections import defaultdict

AIV = os.path.expanduser("~/workspace/stocks-ai-view")
BULK = os.path.expanduser("~/workspace/stocks-data-kline/data/bulk")
ORCH_DIR = os.path.join(AIV, "reports", "r3_intermediate", "orch")
OUT = os.path.join(AIV, "reports", "r3_intermediate", "eval.json")
MAIN = tuple(f"day_qfq_{s}.csv" for s in
             ("00", "01", "02", "03", "04", "05", "06", "07", "08", "09",
              "20", "30", "60", "68"))
AUX = ("day_qfq_expand.csv", "day_qfq_extra.csv",
       "day_qfq_recent.csv", "day_qfq_stockdb.csv")
HORIZONS = (1, 5)


def parse_line(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    sep = "|" if "|" in line else ("," if "," in line else None)
    if not sep:
        return None
    parts = line.split(sep)
    if len(parts) < 4:
        return None
    try:
        return parts[0], parts[1], float(parts[3])
    except ValueError:
        return None


def wilson(hits, n, z=1.96):
    if n == 0:
        return [None, None]
    p = hits / n
    den = 1 + z * z / n
    ctr = p + z * z / (2 * n)
    rad = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [round((ctr - rad) / den * 100, 2), round((ctr + rad) / den * 100, 2)]


def main():
    # ── 读榜单 ──
    picks = []   # {asof, fold, code, S, regime, llm}
    orch_files = sorted(glob.glob(os.path.join(ORCH_DIR, "run_*.json")))
    for fp in orch_files:
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        asof = d.get("asof")
        fold = os.path.basename(fp)[4:8]  # run_2024_2024-06-14.json → '2024'
        regime = (d.get("regime") or {}).get("regime")
        llm = d.get("llm")
        for r in d.get("top") or []:
            picks.append({"asof": asof, "fold": fold, "code": r["code"],
                          "S": r.get("S"), "S_raw": r.get("S_raw"),
                          "llm_adjust": r.get("llm_adjust"),
                          "regime": regime, "llm": llm})
    pick_dates = sorted({p["asof"] for p in picks})
    pick_codes = {p["code"] for p in picks}
    print(f"[eval] orch files={len(orch_files)} picks={len(picks)} "
          f"asof days={len(pick_dates)} codes={len(pick_codes)}", flush=True)
    if not picks:
        raise SystemExit("no picks")

    # ── Pass A: 主分片 → closes(全市场, 基准用) ──
    kl = {}      # code -> {date: close}  (主分片)
    for fn in MAIN:
        fp = os.path.join(BULK, fn)
        if not os.path.exists(fp):
            continue
        with open(fp, encoding="utf-8", errors="replace") as f:
            for line in f:
                pr = parse_line(line)
                if not pr:
                    continue
                code, d, c = pr
                dd = kl.setdefault(code, {})
                if d not in dd:          # 主片内同 (code,date) first-wins 去重
                    dd[d] = c
    print(f"[eval] 主分片 codes={len(kl)}", flush=True)

    # ── Pass B: 辅助片补榜单代码缺失 (code,date) ──
    aux_add, aux_skip = 0, 0
    for fn in AUX:
        fp = os.path.join(BULK, fn)
        if not os.path.exists(fp):
            continue
        touched = defaultdict(dict)
        with open(fp, encoding="utf-8", errors="replace") as f:
            for line in f:
                pr = parse_line(line)
                if not pr:
                    continue
                code, d, c = pr
                if code in pick_codes:
                    touched[code][d] = c
        for code, dd in touched.items():
            have = kl.setdefault(code, {})
            for d, c in dd.items():
                if d in have:
                    aux_skip += 1
                else:
                    have[d] = c
                    aux_add += 1
    print(f"[eval] 辅助片补 {aux_add} 行(跳过冲突 {aux_skip})", flush=True)

    ser = {c: sorted(v.items()) for c, v in kl.items()}
    kidx = {c: {d: i for i, (d, _) in enumerate(v)} for c, v in ser.items()}
    del kl

    # ── 基准: 对每个 pick 日, 全市场等权 T+1/T+5 ──
    bench = {}
    for d in pick_dates:
        for h in HORIZONS:
            rets = []
            for c, s in ser.items():
                i = kidx[c].get(d)
                if i is None or i + h >= len(s):
                    continue
                e = s[i][1]
                if e > 0:
                    rets.append(s[i + h][1] / e - 1)
            if rets:
                bench[f"{d}|{h}"] = {
                    "n": len(rets),
                    "mean_ret": round(sum(rets) / len(rets) * 100, 4),
                    "win_rate": round(
                        sum(1 for r in rets if r > 0) / len(rets) * 100, 2)}
    print(f"[eval] 基准完成 {len(bench)} 个 (date,h)", flush=True)

    # ── 逐 pick 结算 ──
    dropped = defaultdict(int)
    for p in picks:
        s = ser.get(p["code"])
        if not s:
            dropped["no_kline"] += 1
            continue
        i = kidx[p["code"]].get(p["asof"])
        if i is None:
            dropped["no_bar_at_asof"] += 1
            continue
        e = s[i][1]
        if e <= 0:
            dropped["bad_price"] += 1
            continue
        for h in HORIZONS:
            if i + h >= len(s):
                dropped[f"no_forward_{h}d"] += 1
                p[f"ret_t{h}"] = None
            else:
                p[f"ret_t{h}"] = round((s[i + h][1] / e - 1) * 100, 4)

    result = {
        "convention": {
            "return": "T(asof)收盘→T+N收盘, %",
            "benchmark": "同日全体主分片股票等权(均值/上涨占比); "
                         "主分片优先, 辅助片仅补主片缺失(code,date)",
            "dedup": "主片内 (code,date) first-wins; 跨片冲突弃辅助片",
            "ci": "Wilson 95%",
        },
        "data_quality": {"aux_rows_merged": aux_add,
                         "aux_conflict_skipped": aux_skip,
                         "main_shard_codes": len(ser)},
        "dropped": dict(dropped),
        "bench": bench,
        "picks": picks,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    print(f"[eval] -> {OUT} picks={len(picks)} dropped={dict(dropped)}",
          flush=True)


if __name__ == "__main__":
    main()
