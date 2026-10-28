#!/usr/bin/env python3
"""build_limit_history.py -- 每日个股涨停名单历史（GitHub 上的、端上不再本地存）。

为什么需要：
  端上 `MarketDayArchive` 每天落一份 `market_days/YYYY-MM-DD.dat.gz`，保留 420 天，
  用来算连板高度 / 晋级率 / 首板二板占比。但东财只给「此刻」的涨停池，删了就补不回来，
  所以这份存档一直是端上必须囤的东西——和「数据在 GitHub、端上只留当天」冲突。

  这个脚本把那份历史搬到云端：用全市场日线（bulk）回算每天谁涨停，输出
  `data/limit_history.json`（最近 N 个交易日的代码名单）。端上只要拉这一份就能算连板，
  本地不必再囤日级快照。

输入（只读，不联网）：
  <bulk>/day_qfq_*.csv   全市场前复权日线，格式两种历史版本混用：
                         `code|date|open|close|high|low|vol`（早期）
                         `code|date,open,close,high,low,vol`（后期）
                         解析时把逗号统一成管道。

输出：
  <out>/limit_history.json
    {ts, days:[{date, n, codes:[6位代码]}], caliber, caveat, warnings}

设计规则：
  * 只算涨停，不猜「封单量 / 炸板」——日线里没有这些，硬凑就是编数据
  * 阈值按板块：主板 9.9% / 双创 19.9% / 北交所 29.9%；ST 的 5% 涨停会漏判，
    新股首日无涨跌幅限制会误判，两者都写进 caveat 而不是悄悄算进去
  * 缺数据的日期直接不出现（宁缺毋假），不要补 0 天

用法：
  python3 scripts/build_limit_history.py --bulk ../stocks-data-kline/data/bulk
  python3 scripts/build_limit_history.py --days 120 --out data/limit_history.json
"""
import argparse
import glob
import json
import os
import sys
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

CALIBER = "涨停=主板>=9.9% / 创业板科创板>=19.9% / 北交所>=29.9%（前复权日涨幅）"
CAVEAT = ("按前复权收盘价涨幅判定，不等同交易所涨停价：ST 股 5% 涨停会漏判，"
          "新股上市首日无涨跌幅限制会误判；名单只含代码，不含封单量与炸板信息（日线没有）")


def threshold_of(code: str) -> float:
    """按代码前缀给涨停阈值（百分点）。"""
    n = code[2:] if code[:2] in ("sh", "sz", "bj") else code
    if n.startswith(("30", "68")):
        return 19.9
    if n.startswith(("43", "83", "87", "88", "92")):
        return 29.9
    return 9.9


def load_series(bulk_dir: str, tail: int) -> dict:
    """{code: deque[(date, close)]}，每个代码只留最近 tail 条，日期严格递增。

    格式混用两种分隔符：早期 `code|date|...`，后期 `code|date,...,`。
    先把逗号换成管道再切，两种都能正确解析。

    ## 必需去重：分片里同一代码会把两套价格整段拼在一起

    实测 sz000001 在 2026-04-13 的行序是 `10.441 → 10.461 → 11.050 → 11.070`，
    前两条是前复权、后两条是不复权，日期退回 04-13——相当于同一只股票写了两遍。
    直接 append 会让相邻两日跨基准相比（11.070/10.461 = +5.8%），大量股票因此被
    误判成涨停（2026-04-14 一天冒出 1560 只）。

    去重规则：同一代码的行日期必须严格递增，一旦出现日期倒退就跳过——那正是第二套
    序列的起点（或重复段）。比按 (code,date) 建全局 set 省内存，也比事后排序快。
    """
    series = defaultdict(lambda: deque(maxlen=tail))
    last_date = {}
    shards = sorted(glob.glob(os.path.join(bulk_dir, "day_qfq_*.csv")))
    if not shards:
        return {}
    for path in shards:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").replace(",", "|").split("|")
                if len(parts) < 6:
                    continue
                code, date_s = parts[0], parts[1]
                if len(date_s) < 10:
                    continue
                date_s = date_s[:10]
                prev_date = last_date.get(code)
                if prev_date is not None and date_s <= prev_date:
                    continue
                try:
                    close = float(parts[3])
                except ValueError:
                    continue
                if close <= 0:
                    continue
                last_date[code] = date_s
                series[code].append((date_s, close))
    return series


def build_days(series: dict) -> dict:
    """{date: set(codes)}，只保留涨停日的代码。

    |涨幅| 超过 35% 的一律不当涨停：A 股单日限幅最大 30%（北交所），越过这个线只可能是
    数据本身有问题（复权基准切换、拆分、脏行），照单全收就会像修复前那样一天误判上千只。
    """
    by_date = defaultdict(set)
    skipped = 0
    for code, rows in series.items():
        limit = threshold_of(code)
        short = code[2:] if code[:2] in ("sh", "sz", "bj") else code
        prev = None
        for date_s, close in rows:
            if prev is not None and prev > 0:
                pct = (close / prev - 1.0) * 100.0
                if abs(pct) > 35.0:
                    skipped += 1
                elif pct >= limit:
                    by_date[date_s].add(short)
            prev = close
    if skipped:
        print(f"  跳过 {skipped} 个越界涨幅（|pct|>35%，判为数据异常）")
    return by_date


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bulk", default=os.path.join("..", "stocks-data-kline", "data", "bulk"))
    ap.add_argument("--out", default=os.path.join("data", "limit_history.json"))
    ap.add_argument("--days", type=int, default=260,
                    help="保留最近 N 个有数据的交易日（端上 RPS250 要 260 天）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(args.bulk):
        print(f"分片目录不存在: {args.bulk}", file=sys.stderr)
        return 1

    # 每个代码留 days+5 条：最后一条没有前一根，算不了涨幅
    series = load_series(args.bulk, args.days + 5)
    if not series:
        print("没读到任何日线", file=sys.stderr)
        return 1

    by_date = build_days(series)
    dates = sorted(by_date)[-args.days:]
    days = [{"date": d, "n": len(by_date[d]), "codes": sorted(by_date[d])} for d in dates]

    warnings = []
    empty = [d["date"] for d in days if d["n"] == 0]
    if empty:
        warnings.append(f"{len(empty)} 个交易日没有涨停记录（可能是数据缺口，不是零涨停）")
    if len(days) < args.days:
        warnings.append(f"只有 {len(days)} 个交易日数据，不足 {args.days} 天")

    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "engine": "build_limit_history",
        "coverage": {
            "n_symbols": len(series),
            "n_days": len(days),
            "first_date": days[0]["date"] if days else None,
            "last_date": days[-1]["date"] if days else None,
        },
        "caliber": CALIBER,
        "caveat": CAVEAT,
        "days": days,
        "warnings": warnings,
    }

    if args.dry_run:
        print(json.dumps(out["coverage"], ensure_ascii=False))
        print("样例:", days[-1] if days else None)
        return 0

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    size_kb = os.path.getsize(args.out) / 1024
    print(f"{len(days)} 个交易日 / {len(series)} 只股票 → {args.out}（{size_kb:.0f}KB）")
    for w in warnings:
        print(f"  警告: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
