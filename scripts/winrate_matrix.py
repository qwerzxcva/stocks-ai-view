#!/usr/bin/env python3
"""
winrate_matrix.py — 持有期 × 市场阶段 交叉胜率矩阵

数据来源: data/reflect_latest.json (reflect 引擎的历史预测 vs 实际涨跌统计)
矩阵维度: 持有期 (1/3/5/10) × 市场阶段 (bull/bear, 来自 reflect 分析中的多空分类)

输出结构:
  {
    "ts": "...",
    "source": "reflect_latest.json",
    "source_ts": "...",
    "horizons": {
      "1": {
        "overall": 0.395,
        "avg_ret_pct": -0.51,
        "n": 76,
        "by_regime": {"bull": 0.387, "bear": 0.400},
        "regime_n": {"bull": 62, "bear": 10},
        "sufficient": true
      },
      ...
    }
  }

说明:
  - 市场阶段标签 (bull/bear) 与 reflect 一致, 代表信号日的判断方向
  - raw_win_rate 是总体胜率 (%), 转换为小数 (0.0-1.0)
  - bull_win_rate / bear_win_rate 同样转换
  - 样本不足 30 条时 sufficient=false, 结论不可信 (沿用 reflect meta 阈值)
  - 持有期若有 n=0 或缺失记录, 整体指标为 null
  - 10 日周期若 reflect 无数据, 以 null 占位

用法:
  python3 scripts/winrate_matrix.py
  python3 scripts/winrate_matrix.py --out data/winrate_matrix.json
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

REFLECT_FILE = os.path.join(DATA_DIR, "reflect_latest.json")
DEFAULT_OUT = os.path.join(DATA_DIR, "winrate_matrix.json")
HORIZONS = [1, 3, 5, 10]
MIN_SAMPLE = 30


def to_rate(v):
    """百分数 → 小数 (0.0-1.0), None 透传。"""
    if v is None:
        return None
    try:
        return round(float(v) / 100, 4)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(description="持有期 × 市场阶段 交叉胜率矩阵")
    ap.add_argument("--in", default=REFLECT_FILE, dest="reflect_file", help="reflect_latest.json 路径")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    if not os.path.exists(args.reflect_file):
        print(f"[winrate_matrix] 源文件不存在: {args.reflect_file}", file=sys.stderr)
        sys.exit(1)
    src = json.load(open(args.reflect_file, encoding="utf-8"))

    overall = (src.get("analysis") or {}).get("overall") or {}
    meta = src.get("meta") or {}

    result = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "source": os.path.basename(args.reflect_file),
        "source_ts": src.get("ts"),
        "horizons": {},
        "meta": {
            "method": meta.get("method", "预测时点的多空票 vs 预测后 N 日实际涨跌"),
            "evidence": meta.get("evidence", ""),
            "min_sample_hint": f"样本>={MIN_SAMPLE} 方可信",
        },
    }

    for h in HORIZONS:
        rec = overall.get(str(h)) or overall.get(h) or {}
        n = int(rec.get("n", 0) or 0)
        raw_wr = rec.get("raw_win_rate")
        bull_wr = rec.get("bull_win_rate")
        bear_wr = rec.get("bear_win_rate")
        avg_ret = rec.get("avg_ret_pct")
        bull_n = int(rec.get("bull_n") or 0)
        bear_n = int(rec.get("bear_n") or 0)
        entry = {
            "overall": to_rate(raw_wr),
            "avg_ret_pct": round(float(avg_ret), 3) if avg_ret is not None else None,
            "n": n,
            "by_regime": {
                "bull": to_rate(bull_wr),
                "bear": to_rate(bear_wr),
            },
            "regime_n": {"bull": bull_n, "bear": bear_n},
            "sufficient": n >= MIN_SAMPLE,
        }
        result["horizons"][str(h)] = entry

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"[winrate_matrix] {args.out}")
    for h in HORIZONS:
        e = result["horizons"][str(h)]
        tag = "✓" if e["sufficient"] else "⚠"
        print(f"  持有 {h:>2d} 日: {tag} n={e['n']} 胜率={e['overall']} "
              f"平均收益={e['avg_ret_pct']}% bull={e['by_regime']['bull']}({e['regime_n']['bull']}) "
              f"bear={e['by_regime']['bear']}({e['regime_n']['bear']})")
    print(f"  meta: {result['meta'].get('method')}")
    if result["meta"].get("evidence"):
        print(f"  evidence: {result['meta']['evidence']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
