#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R3 驱动: 交易日表 → fold 划分 → 每 fold train 期 feedback_updater(快) →
test 期每周 1 个 asof 跑 central_orchestrator(B,E 组, top10 主板, 冻结 fold 权重)。

只写 reports/ 与 reports/r3_intermediate/; 引擎一律子进程调用不修改。
orchestrator 运行次数 ~100 次 (5 fold × ~20 asof), 单次实测 ~130s, 串行 ~3.6h。
fold 内 asof 串行 (避免 16GB 容器并发 OOM); 每 fold 结束落 manifest 断点续跑。
"""
import bisect
import json
import os
import subprocess
import sys
import time

AIV = os.path.expanduser("~/workspace/stocks-ai-view")
KLINE = os.path.expanduser("~/workspace/stocks-data-kline/data")
ENG = os.path.join(AIV, "scripts", "engines")
INT = os.path.join(AIV, "reports", "r3_intermediate")
ORCH_DIR = os.path.join(INT, "orch")
W_DIR = os.path.join(INT, "weights")
IDX = os.path.join(KLINE, "index", "history", "sh000001.txt")

FOLD_YEARS = ["2023", "2024", "2025", "2026"]
SNAP_START = "2023-11-20"   # engine_v5 快照最早日; fold2023 test 需晚于它才有榜
TEST_START = {"2023": "2023-11-20"}   # fold 年度 test 窗下界覆写
MIN_SNAP_DAYS = 15          # train 期快照交易日下限, 不足则标记降级


def sh(cmd, timeout, log):
    t0 = time.time()
    log.write(f"$ {' '.join(cmd)}\n")
    log.flush()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    el = time.time() - t0
    log.write(p.stdout[-2000:] + "\n" + p.stderr[-1000:]
              + f"\n[rc={p.returncode} {el:.0f}s]\n")
    log.flush()
    return p.returncode, el


def main():
    os.makedirs(ORCH_DIR, exist_ok=True)
    os.makedirs(W_DIR, exist_ok=True)
    log = open(os.path.join(INT, "driver.log"), "a", encoding="utf-8")

    # ── 交易日表 (指数) ──
    dates = []
    with open(IDX, encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.strip().split("|")
            if len(p) >= 2 and p[1][:4].isdigit():
                dates.append(p[1])
    dates = sorted(set(dates))
    log.write(f"[driver] index trading days {dates[0]}..{dates[-1]} "
              f"n={len(dates)}\n")

    # ── 快照日集合 ──
    import glob as _g
    snap_days = sorted(os.path.basename(fp)[len("engine_v5_"):-5]
                       for fp in _g.glob(os.path.join(
                           AIV, "data", "predictions", "engine_v5_*.json")))
    log.write(f"[driver] v5 snapshots {snap_days[0]}..{snap_days[-1]} "
              f"n={len(snap_days)}\n")

    manifest_p = os.path.join(INT, "manifest.json")
    manifest = {"folds": {}}
    if os.path.exists(manifest_p):
        manifest = json.load(open(manifest_p, encoding="utf-8"))

    for year in FOLD_YEARS:
        yd = [d for d in dates if d.startswith(year)]
        if not yd:
            log.write(f"[driver] fold {year}: 指数无交易日, 跳过\n")
            continue
        n = len(yd)
        cut = yd[int(n * 0.7) - 1]
        train_end = cut
        test_start = max(yd[int(n * 0.7)], TEST_START.get(year, "0000"))
        test_days = [d for d in yd if d >= test_start]
        # T+5 结算边界: test asof ≤ 最后交易日 - 5
        lim = dates[-6] if len(dates) > 5 else dates[-1]
        test_days = [d for d in test_days if d <= lim]
        asofs = test_days[::5]          # 每周 1 个
        fold = manifest["folds"].setdefault(year, {})
        fold.update({"year_days": n, "train_end": train_end,
                     "test_start": test_days[0] if test_days else None,
                     "test_end": test_days[-1] if test_days else None,
                     "asofs_planned": asofs})
        # train 窗快照数
        tr_start = f"{int(year) - 1}-01-01"
        n_snap = sum(1 for s in snap_days if tr_start <= s <= train_end)
        fold["train_snap_days"] = n_snap
        fold["train_window"] = [tr_start, train_end]
        wf = os.path.join(W_DIR, f"fold_{year}.json")
        fold["weights_file"] = wf
        fold["weights_note"] = None

        # ── train: feedback_updater (fold 级一次; 幂等重跑) ──
        fb_out = os.path.join(INT, f"fb_fold_{year}.json")
        if not fold.get("fb_done"):
            if n_snap < MIN_SNAP_DAYS:
                fold["weights_note"] = (
                    f"train 期 v5 快照仅 {n_snap} 个交易日 "
                    f"(<{MIN_SNAP_DAYS}), feedback 复盘样本不足; "
                    "test 期用主仓 weights/latest.json (单引擎 cold 链)")
                fold["weights_file"] = os.path.join(ENG, "weights",
                                                    "latest.json")
                fold["fb_done"] = True
            else:
                rc, el = sh(["python3", os.path.join(ENG,
                                                     "feedback_updater.py"),
                             "--asof", train_end, "--train-end", train_end,
                             "--lookback", "250",
                             "--weights-file", os.path.join(ENG, "weights",
                                                            "latest.json"),
                             "--dry-run", "--out", fb_out], 300, log)
                fold["fb_done"] = rc == 0
                # 快照: fold 权重冻结 = 主仓 latest.json 内容拷入 r3 目录,
                # 语义等价 (单引擎 r̄=r_i 因子=1), 且不受后续批次更新 latest 影响
                latest = os.path.join(ENG, "weights", "latest.json")
                try:
                    fb = json.load(open(fb_out, encoding="utf-8"))
                    fold["fb"] = {"updated_would": not fb.get("dry_run"),
                                  "settled": fb.get("settled_records"),
                                  "engine_r": fb.get("engine_r"),
                                  "single_engine": fb.get(
                                      "single_engine_no_relative_update"),
                                  "degraded": fb.get("degraded")}
                except Exception as e:
                    fold["fb"] = {"parse_fail": str(e)}
                try:
                    if not os.path.exists(wf):
                        with open(latest, encoding="utf-8") as fi, \
                                open(wf, "w", encoding="utf-8") as fo:
                            fo.write(fi.read())
                    fold["weights_file"] = wf
                    fold["weights_note"] = (
                        "feedback 仅 engine_v5 单信号源, r̄=r_i 乘性因子=1, "
                        "fold 权重=latest.json 快照冻结于 r3_intermediate/weights/")
                except OSError as e:
                    fold["weights_file"] = latest
                    fold["weights_note"] = f"snapshot fail({e}), 引用主仓 latest"

        # ── test: orchestrator × asofs ──
        done = fold.setdefault("asofs_done", [])
        for asof in asofs:
            if asof in done:
                continue
            out = os.path.join(ORCH_DIR, f"run_{year}_{asof}.json")
            rc, el = sh(["python3", os.path.join(ENG,
                                                 "central_orchestrator.py"),
                         "--asof", asof, "--groups", "B,E",
                         "--limit", "300", "--no-v5", "--no-feedback",
                         "--price-levels-top", "0", "--top", "10",
                         "--weights-file", fold["weights_file"],
                         "--out", out], 600, log)
            ok = rc == 0 and os.path.exists(out)
            if ok:
                try:
                    d = json.load(open(out, encoding="utf-8"))
                    ok = bool(d.get("top"))
                    if not (d.get("top")):
                        log.write(f"[driver] {asof} 空榜 "
                                  f"(degraded={d.get('degraded')})\n")
                except Exception:
                    ok = False
            if ok:
                done.append(asof)
                manifest["folds"][year] = fold
                json.dump(manifest, open(manifest_p, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
            else:
                log.write(f"[driver] {asof} FAILED rc={rc}, 不重试\n")
                done.append(asof + "!FAIL")
        manifest["folds"][year] = fold
        json.dump(manifest, open(manifest_p, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        log.write(f"[driver] fold {year} done, asofs {len(done)}/"
                  f"{len(asofs)}\n")
        log.flush()

    log.write("[driver] ALL DONE\n")
    log.close()
    print("driver done")


if __name__ == "__main__":
    main()
