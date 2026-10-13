#!/usr/bin/env python3
"""
backfill_predictions.py — 回溯生成历史预测快照 (让回测/胜率矩阵立即有样本)

问题:
  CI 每天只存当天的预测快照, reflect/kline_sim 的多持有期胜率从 0 开始,
  要等 10 天才能验证 10 日持有期, 期间界面全是空。

方案:
  对过去 N 个交易日, 用「截至该日」的 K 线跑 engine_v5.score_stock,
  生成 engine_v5_YYYY-MM-DD.json (与 CI 产出格式一致)。
  reflect_engine 即可立即验证 1/3/5/10 日胜率。

无前视偏差:
  只用 <= asof 的 bars, 不用未来数据。

用法:
  python3 backfill_predictions.py --days 15
  python3 backfill_predictions.py --days 30 --limit 500
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine_v5 as E
from paths import BULK, AI_VIEW_DATA


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=15, help="回溯多少个交易日")
    ap.add_argument("--limit", type=int, default=300,
                    help="每天最多评多少只股票 (按最新成交额排序, 0=全部)")
    ap.add_argument("--top-per-day", type=int, default=50,
                    help="每天写入快照的 top N 条 (reflect 只验证这些)")
    ap.add_argument("--out-dir", default=os.path.join(AI_VIEW_DATA, "predictions"))
    args = ap.parse_args()

    print("[backfill] 加载 K线 ...", flush=True)
    klines = E.load_klines(BULK)
    if not klines:
        print("[backfill] 无 K线数据", file=sys.stderr)
        return 1

    ds = set()
    for rows in klines.values():
        for b in rows:
            ds.add(b[0])
    dates = sorted(ds, reverse=True)[: args.days]
    dates = sorted(dates)  # 升序, 早的日期先处理
    print(f"[backfill] {len(klines)} 只股票, 回溯 {len(dates)} 天: "
          f"{dates[0]} ~ {dates[-1]}", flush=True)

    # 股票优先级: 最近一根 bar 的 vol*close (近似成交额)
    def liquidity(rows):
        try:
            b = rows[-1]
            return float(b[4]) * float(b[5])
        except (ValueError, IndexError):
            return 0.0
    ranked = sorted(klines.items(), key=lambda kv: -liquidity(kv[1]))
    if args.limit:
        ranked = ranked[: args.limit]

    os.makedirs(args.out_dir, exist_ok=True)
    for asof in dates:
        out_path = os.path.join(args.out_dir, f"engine_v5_{asof}.json")
        results = []
        for code, rows in ranked:
            bars = [b for b in rows if b[0] <= asof]
            if len(bars) < 30:
                continue
            try:
                # bars 来自 engine_v5.load_klines: [date, open, close, high, low, vol]
                # 注意: b[1]=open, b[2]=close, b[3]=high, b[4]=low (不是直觉顺序!)
                close = [float(b[2]) for b in bars]
                high = [float(b[3]) for b in bars]
                low = [float(b[4]) for b in bars]
                vol = [float(b[5]) for b in bars]
                r = E.score_stock(close, high, low, vol, code_bare=code[2:] if code[:2] in ("sh", "sz", "bj") else code)
            except Exception:
                continue
            if not r:
                continue
            r = dict(r)
            r["code"] = code
            r["name"] = ""
            r["price"] = close[-1] if close else 0
            r["bars"] = len(bars)
            r["date"] = asof          # reflect_engine 用 top[0].date 作 pred_date
            results.append(r)
        results.sort(key=lambda x: -x.get("score", 0))
        if not results:
            print(f"[backfill] {asof}: 无结果, 跳过")
            continue
        payload = {
            "ts": f"{asof}T15:00:00+08:00",
            "engine": "v5 (回溯生成, 截至该日收盘的K线)",
            "n_scanned": len(results),
            "n_scored": len(results),
            "indicators": 17,
            "formula": "score = 50 + 50 * (Σ w·vote·strength / Σ w) * (0.6 + 0.4*共振率)",
            "top": results[: args.top_per_day],
            "meta": {
                "backfilled": True,
                "asof": asof,
                "method": "backfill_predictions.py (用截至该日的K线重算, 无前视偏差)",
                "caveat": "回溯信号仅用于验证 reflect/kline_sim 框架, 非实时预测",
            },
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        print(f"[backfill] {asof}: {len(results)} 只评分, top{args.top_per_day} 已存 "
              f"(最高 {results[0]['score']:.1f} {results[0]['code']})", flush=True)

    print(f"[backfill] 完成 → {args.out_dir} (共 {len(dates)} 天)")
    print("[backfill] 下一步: reflect_engine --pred-dir data/predictions --horizons 1,3,5,10")
    return 0


if __name__ == "__main__":
    sys.exit(main())
