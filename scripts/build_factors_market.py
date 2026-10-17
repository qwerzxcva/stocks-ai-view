#!/usr/bin/env python3
"""build_factors_market.py — 全市场后复权因子表构建 (2026-10-17)。

数据源: 东财 datacenter RPT_SHAREBONUS_DET (零鉴权, shard a 已用同域)。
口径验证 (validate_ground_truth + 基准 CSV):
- 字段量纲为「每10股」: PRETAX_BONUS_RMB=派现总额/10, BONUS_RATIO=送股/10,
  IT_RATIO=转增/10 (茅台 2002 实证: 10转1.00派6.00 → 1/0.6/0.1)
- 步骤公式: step = prev_close / ((prev_close - D) / (1 + S + T))
- 除权日不在交易日历 (休市/停牌): 应用到其后第一根 K 线, prev 用间隔前收盘
- 数据起点前的历史事件: 逆序反推 (post≈ref 近似), 事件链的乘积进首根因子

与 cnfin 基准的差异 (600519 实测): 2006 年股改期单事件差 17%, 此后 12 年
比值恒定 1.173 逐日吻合; 前复权归一化下历史常数抵消, 近端窗口 (指标用)
完全准确。002242 (2015 上市) 历史事件全在数据起点前, 逆推链与基准终值
3.84 对齐到 2% 内。

产出: data/adjusted/factors/{code}.json (与 build_factors_from_gt 同构)。
用法:
  python3 build_factors_market.py --codes 600519,002242   # 指定
  python3 build_factors_market.py --shard 0 --n-shards 3  # 全市场分片
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR, BULK
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")
    BULK = os.path.join(os.path.dirname(HERE), "..", "stocks-data-kline",
                        "data", "bulk")

FACTORS_DIR = os.path.join(DATA_DIR, "adjusted", "factors")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}


def em_events(code, retries=3):
    """东财分红送配 (每10股口径)。返回按除权日排序的 (ex, D, S, T)。"""
    filt = urllib.parse.quote(f'(SECURITY_CODE="{code}")')
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPT_SHAREBONUS_DET&columns=ALL"
           f"&filter={filt}&sortColumns=EX_DIVIDEND_DATE&sortTypes=1"
           "&pageSize=200")
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                       timeout=15)
            rows = (json.loads(r.read().decode()).get("result") or {}).get("data") or []
            ev = []
            for e in rows:
                ex = (e.get("EX_DIVIDEND_DATE") or "")[:10]
                if not ex or e.get("ASSIGN_PROGRESS") != "实施分配":
                    continue
                ev.append((ex,
                           float(e.get("PRETAX_BONUS_RMB") or 0) / 10,
                           float(e.get("BONUS_RATIO") or 0) / 10,
                           float(e.get("IT_RATIO") or 0) / 10))
            return sorted(ev)
        except Exception:
            if i < retries - 1:
                time.sleep(3)
    return None   # 网络失败与无数据区分: None=失败


def load_raw_close(code_pref):
    import glob
    out = {}
    for p in glob.glob(os.path.join(BULK, "day_qfq_*.csv")):
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith(code_pref + "|"):
                    parts = line.strip().split("|")
                    if len(parts) >= 4:
                        try:
                            out[parts[1]] = float(parts[3])
                        except ValueError:
                            pass
    return out


def build_factor(events, raw_close):
    """事件链 + 原始收盘价 → {date: hfq_factor}"""
    dates = sorted(raw_close)
    if not dates:
        return {}
    factor = 1.0
    ftab = {}
    ei = 0
    # 数据起点前的历史事件: 逆序反推 post≈ref
    pre = [e for e in events if e[0] <= dates[0]]
    live = [e for e in events if e[0] > dates[0]]
    if pre:
        P = raw_close[dates[0]]
        for ex, D, S, T in reversed(pre):
            prev = P * (1 + S + T) + D
            if prev > D:
                factor *= prev / ((prev - D) / (1 + S + T))
            P = prev
    for i, d in enumerate(dates):
        while ei < len(live) and live[ei][0] <= d:
            _, D, S, T = live[ei]
            prev = raw_close[dates[i - 1]] if i > 0 else None
            if prev and prev - D > 0:
                factor *= prev / ((prev - D) / (1 + S + T))
            ei += 1
        ftab[d] = factor
    return ftab


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", help="逗号分隔裸码 (调试)")
    ap.add_argument("--shard", type=int, default=None)
    ap.add_argument("--n-shards", type=int, default=3)
    ap.add_argument("--sleep", type=float, default=0.35)
    ap.add_argument("--out-dir", default=FACTORS_DIR)
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    if args.codes:
        codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()]
    else:
        # 从 bulk 全集按分片取
        raw_all = set()
        import glob
        for p in glob.glob(os.path.join(BULK, "day_qfq_*.csv")):
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("#") or "|" not in line:
                        continue
                    head = line.split("|", 1)[0]
                    bare = head[2:] if head[:2] in ("sh", "sz", "bj") else head
                    if len(bare) == 6 and bare.isdigit():
                        raw_all.add(bare)
        codes = sorted(c for c in raw_all
                       if hash(c) % args.n_shards == args.shard)

    n_ok = n_fail = n_noev = 0
    for i, code in enumerate(codes):
        pref = ("sh" if code[0] in "69" else "sz") + code
        events = em_events(code)
        if events is None:
            n_fail += 1
            continue
        if not events:
            n_noev += 1
            continue
        raw_close = load_raw_close(pref)
        ftab = build_factor(events, raw_close)
        if not ftab:
            n_fail += 1
            continue
        dest = os.path.join(args.out_dir, f"{pref}.json")
        with open(dest, "w", encoding="utf-8") as f:
            json.dump({"code": pref, "source": "em_sharebonus_det",
                       "n": len(ftab), "n_events": len(events),
                       "factors": ftab}, f, ensure_ascii=False)
        n_ok += 1
        if (i + 1) % 200 == 0:
            print(f"[factors] {i+1}/{len(codes)} ok={n_ok} fail={n_fail}",
                  flush=True)
        time.sleep(args.sleep)
    print(f"[factors] 完成: {n_ok} 只建表, {n_noev} 无分红事件, {n_fail} 失败")
    return 0


if __name__ == "__main__":
    sys.exit(main())
