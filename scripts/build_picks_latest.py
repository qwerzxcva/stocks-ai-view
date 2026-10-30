#!/usr/bin/env python3
"""build_picks_latest.py -- 产 skapp 规范形状的 data/engine/picks_latest.json。

## 为什么要这个文件

skapp-app 的 eod-snapshot.yml 每天 15:35 CST 直接拉一个 raw 文件当引擎推股输入。
此前拉的是 skapp-data/data/engine/picks_latest.json，asof 停在 2026-10-15（陈旧 15 天），
端上每日快照的 picks 一直是两周前的老名单。本脚本把本仓 today_picks.json 的
picks.signals 转成规范形状，由 engine-chain 每天发布，skapp 侧只换一个 URL。

## 输出形状（跨仓契约，改字段必须同步 skapp-app/skapp_pkg/pipeline/eod_sync.py）

    {
      "source": "today_picks_v5/signals",
      "asof": "2026-10-28",            # 依据日期，取 items 日期的众数（不是文件生成日）
      "generated_at": "2026-10-30T15:32:10+08:00",
      "n_items": 30,
      "skipped": [{"code": "...", "reason": "无法识别市场前缀"}],
      "items": [{"symbol": "sz002590", "code": "002590", "name": "", "rank": 1,
                 "score": 72.4, "price": 10.08, "date": "2026-10-28"}, ...]
    }

## 红线

- `symbol` 必须带市场前缀（sh/sz/bj）：skapp 侧的行情 map 是 sh600519 口径，
  裸 6 位查不到 → picks 全部 ref_price=None，复盘恒 0（这条踩过）。
- `asof` 取条目日期而不是文件生成时间：today_picks.json 会出现在文件名写今天、
  signals 里 date 还是两天前的情况，asof 写错会让 skapp 快照的 stale 标记说谎。
- 认不出市场的 code 不静默丢：进 skipped 并打警告，宁可下游看见缺口。
- 缺数据写 null，不拿 0 冒充。

## 用法

    python3 scripts/build_picks_latest.py
    python3 scripts/build_picks_latest.py --src data/today_picks.json \\
        --out data/engine/picks_latest.json --limit 30
    python3 scripts/build_picks_latest.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 6 位代码 -> 交易所前缀。北交所 920 新段与沪市 900 B 股不能混（900 走 sh、920 走 bj）。
PREFIX_RULES = (
    ("sh", ("60", "68", "90")),
    ("sz", ("00", "30", "20")),
    ("bj", ("43", "83", "87", "88", "92")),
)


def market_prefix(code: str) -> str | None:
    """6 位数字代码 -> sh/sz/bj；认不出返回 None。"""
    digits = str(code or "").strip()
    if len(digits) != 6 or not digits.isdigit():
        return None
    for prefix, heads in PREFIX_RULES:
        if digits.startswith(heads):
            return prefix
    return None


def to_symbol(code: str) -> str | None:
    """任意常见写法 -> 无点小写 sh600519 / sz000001 / bj830799；认不出返回 None。

    支持 600519 / 600519.SH / sh600519 / 600519sh。
    """
    raw = str(code or "").strip().lower().replace(".", "")
    if not raw:
        return None
    for prefix in ("sh", "sz", "bj"):
        body = raw[len(prefix):]
        if raw.startswith(prefix) and len(body) == 6 and body.isdigit():
            return prefix + body
    if len(raw) == 8 and raw[:6].isdigit() and raw[6:] in ("sh", "sz", "bj"):
        return raw[6:] + raw[:6]
    prefix = market_prefix(raw)
    return prefix + raw if prefix else None


def pick_asof(dates: list[str], fallback: str) -> str:
    """条目日期取众数，平票取较晚的那天；没有可用日期就用 fallback。"""
    valid = [str(d).strip() for d in dates if str(d or "").strip()[:10].count("-") == 2]
    if not valid:
        return fallback
    counts = Counter(valid)
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _num(value):
    """缺数据保持 null，不拿 0 冒充。"""
    if isinstance(value, (int, float)):
        return value
    return None


def build(doc: dict, limit: int = 30, now: datetime | None = None) -> dict:
    """today_picks.json -> picks_latest.json 规范形状。"""
    picks = doc.get("picks") if isinstance(doc.get("picks"), dict) else {}
    signals = picks.get("signals") or []

    items: list[dict] = []
    skipped: list[dict] = []
    for row in signals:
        if not isinstance(row, dict):
            continue
        symbol = to_symbol(row.get("code"))
        if not symbol:
            skipped.append({"code": str(row.get("code")), "reason": "无法识别市场前缀"})
            continue
        items.append({
            "symbol": symbol,
            "code": symbol[2:],
            "name": row.get("name") or "",
            "score": _num(row.get("score")),
            "price": _num(row.get("price")),
            "date": str(row.get("date") or "")[:10] or None,
        })

    # score 降序，缺分排后面；同分保持输入顺序（signals 本身已按引擎强弱排好）
    items.sort(key=lambda it: (it["score"] is None, -(it["score"] or 0.0)))
    items = items[:limit]
    for rank, item in enumerate(items, 1):
        item["rank"] = rank

    stamp = (now or datetime.now(CST)).astimezone(CST).isoformat(timespec="seconds")
    fallback = str(doc.get("date") or stamp[:10])[:10]
    return {
        "source": "today_picks_v5/signals",
        "asof": pick_asof([it["date"] for it in items], fallback),
        "generated_at": stamp,
        "n_items": len(items),
        "skipped": skipped,
        "items": items,
    }


def write_json(path: str, payload: dict) -> int:
    """原子写：先写同目录临时文件再 replace，避免下游读到半个文件。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return os.path.getsize(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="产 skapp 规范形状的 picks_latest.json")
    ap.add_argument("--src", default=os.path.join(ROOT, "data", "today_picks.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "engine", "picks_latest.json"))
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        with open(args.src, encoding="utf-8") as handle:
            doc = json.load(handle)
    except Exception as exc:
        print("::error::读不到源文件 %s: %s（跨仓契约产物不能缺席）" % (args.src, exc))
        return 1

    payload = build(doc, limit=args.limit)
    if not payload["items"]:
        print("::error::%s 里没有可用的 picks.signals（源文件 date=%s），拒绝产空名单"
              % (args.src, doc.get("date")))
        return 1

    for row in payload["skipped"]:
        print("::warning::跳过无法识别市场的代码 %s（%s）" % (row["code"], row["reason"]))

    if args.dry_run:
        print("asof=%s n=%d 首项=%s" % (payload["asof"], payload["n_items"],
                                       payload["items"][0]["symbol"]))
        return 0

    size = write_json(args.out, payload)
    print("picks_latest: asof=%s items=%d skipped=%d %.1fKB -> %s"
          % (payload["asof"], payload["n_items"], len(payload["skipped"]), size / 1024, args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
