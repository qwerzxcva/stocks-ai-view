#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/phase_runner.py ═══
# 上游 commit: db4c5a0d38e3 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
# 适配: 2026-10-13 路径规范 (duckdb stockdb → bulk CSV 自算涨停结构; --out 落盘 data/market_phase.json)
"""
phase_runner.py — 市场阶段机数据供给器: 从日K自算涨停结构 → 六阶段 → market_phase.json

数据源 (本项目): stocks-data-kline bulk day_qfq_*.csv (code|date|open|close|high|low|vol)
  上游原版读 stockdb/stock.duckdb 的 daily 表 — 本项目无该库, 改用 bulk CSV 等价计算。
涨停口径与上游一致: 收盘/前收-1 >= 9.5% (主板10%近似)。
"""
import argparse
import csv
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import BULK, AI_VIEW_DATA as DATA_DIR
except Exception:
    BULK = os.path.join(os.path.dirname(os.path.dirname(HERE)), "stocks-data-kline", "data", "bulk")
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

from market_phase import classify

LABELS = {"ice": "冰点", "ignite": "启动", "rally": "主升", "climax": "高潮", "ebb": "退潮", "repair": "修复"}


def load_daily(bulk_dir: str, days: int = 90):
    """bulk day_qfq_*.csv → {code: [(date, close), ...] 升序}"""
    closes = defaultdict(list)
    for p in sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv"))):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("|") if "|" in line else line.split(",")
                if len(parts) < 6:
                    continue
                try:
                    closes[parts[0]].append((parts[1], float(parts[2])))
                except ValueError:
                    continue
    for c in closes:
        closes[c].sort(key=lambda x: x[0])
    return closes


def daily_structure(closes: dict, days: int = 90):
    """自算每日涨停梯队: [{date, first_board, ge2, height, promo}]"""
    # 每票逐日涨幅
    per_code = {}
    for code, rows in closes.items():
        rets = []
        for i in range(1, len(rows)):
            pc = rows[i - 1][1]
            if pc > 0:
                rets.append((rows[i][0], rows[i][1] / pc - 1))
        per_code[code] = rets

    all_dates = sorted({d for rets in per_code.values() for d, _ in rets})
    if not all_dates:
        return []
    recent = set(all_dates[-days:])

    prev_zt = {}
    out = []
    for dt in all_dates[-days:]:
        fb = ge2 = 0
        height = 0
        today_zt = {}
        for code, rets in per_code.items():
            r = next((x for x in rets if x[0] == dt), None)
            if r is None or r[1] < 0.095:
                continue
            n = prev_zt.get(code, 0) + 1
            today_zt[code] = n
            height = max(height, n)
            if n == 1:
                fb += 1
            else:
                ge2 += 1
        promo_ok = sum(1 for c, n in today_zt.items()
                       if prev_zt.get(c, 0) >= 1 and prev_zt.get(c, 0) + 1 == n)
        pool = sum(1 for n in prev_zt.values() if n >= 1)
        out.append({"date": dt, "first_board": fb, "ge2": ge2, "height": height,
                    "promo": (promo_ok / pool) if pool >= 10 else None, "seal": None})
        prev_zt = today_zt
    return out


def main():
    ap = argparse.ArgumentParser(description="市场阶段机数据供给器 (bulk CSV 自算涨停结构)")
    ap.add_argument("--bulk", default=None, help="K线 bulk 目录 (默认 paths.BULK)")
    ap.add_argument("--days", type=int, default=90, help="回看交易日数")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "market_phase.json"))
    args = ap.parse_args()

    bulk = args.bulk or BULK
    closes = load_daily(bulk)
    if not closes:
        print(f"[phase_runner] bulk 无数据: {bulk}", file=sys.stderr)
        return 1
    daily = daily_structure(closes, args.days)
    if not daily:
        print("[phase_runner] 涨停结构计算为空", file=sys.stderr)
        return 1
    phases = classify(daily)
    last = phases[-1]
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "dates": [x["date"] for x in daily],
        "phases": phases,
        "phase": last,
        "label": LABELS.get(last, "?"),
        "recent": list(zip([x["date"] for x in daily[-5:]], phases[-5:])),
        "structure": daily[-10:],
        "source": f"bulk day_qfq ({os.path.basename(bulk)}), 涨停口径=收盘/前收>=9.5%",
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print("phase:", last, LABELS.get(last), "| recent:", payload["recent"][-3:])
    print("->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
