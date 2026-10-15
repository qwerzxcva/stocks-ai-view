#!/usr/bin/env python3
"""
fetchers_ticks.py — 腾讯逐笔成交采集器 (分笔明细, 非 Level-2 逐笔委托)

来源: 借鉴 simonlin1212/a-stock-data (Apache-2.0) §1.4 tencent_ticks()
  https://github.com/simonlin1212/a-stock-data
  上游 2026-09-22 针对通达信公开服务器 K线/盘口/逐笔命令失效 (#52) 的替代方案:
  mootdx transaction() 返回空后, 逐笔改走腾讯行情页「成交明细」HTTP 接口。

为什么需要它 (我们此前的缺口):
  1. ban_prob 的封单强度此前只能估算, 有了逐笔可算真实主动买卖占比
  2. premarket_picks 的「竞价抢筹」此前只有 zt_time 一个字段,
     逐笔里 09:25 那一笔就是开盘集合竞价的撮合结果 (价格/量/额)
  3. sector_engine / 打板分析缺「封板后是否还有持续主动买」的判断依据

口径与边界 (务必读, 这些是上游实测结论, 不是猜的):
  - 约 3 秒一笔的**分笔** (同一时刻撮合的多笔合并), 不是交易所 Level-2
    逐笔委托 / 逐笔成交。09:25 那一笔 = 开盘集合竞价撮合结果。
  - 只有**最近一个交易日**, 没有历史; 覆盖沪深个股 (含创业板/科创板) 与 ETF。
    北交所、指数不支持 (直接抛 ValueError)。
  - volume 单位「手」, amount 单位元。科创板 K 线按股成交, 但逐笔每笔取整成手,
    全天累加与真实股数有误差 (成交越清淡误差越大: 上游实测 688981 +0.013%,
    清淡的 688184 +0.20%)。因此**只用 amount 做核对, 不用 volume**。
  - 收盘后 15:05-15:30 的盘后定价 (固定价格) 成交也在结果里 (time > 15:00:59),
    不计入腾讯行情快照的当日成交额。
  - 完整性核对: 收盘后调用时, 连续竞价段 (<=15:00:59) 成交额合计需与行情快照
    的当日成交额相符, 差超过 0.1%+1000 元判为不全。翻页遇空页即结束。
  - 腾讯偶尔缓存旧页会缺笔: 连续竞价段缺号直接判失败; 盘后段缺号记入
    missing_seq。15:31 以后再用快照的盘后成交额核对 → complete 标志。
  - 开盘前 (09:25 撮合前) 调用可能拿不到上一交易日逐笔。

用法:
  python3 fetchers_ticks.py --code 000001
  python3 fetchers_ticks.py --codes 000001,600519,300750 --out data/ticks_latest.json
  python3 fetchers_ticks.py --code 688981 --json     # 单只全量逐笔

输出: data/ticks_<date>.json (逐只) 或 --out 指定的合并文件
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA
except Exception:
    AI_VIEW_DATA = os.path.join(os.path.dirname(HERE), "data")

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
    "Referer": "https://gu.qq.com/",
}

TICK_URL = "https://stock.gtimg.cn/data/index.php"
QT_URL = "https://qt.gtimg.cn/q="
MAX_PAGES = 300            # 一页 70 笔; 上游实测最活跃的票全天约 4800 笔 / 69 页
SESSION_END = "15:00:59"   # 连续竞价 + 收盘集合竞价到此为止, 之后是盘后定价


def _http(url, params=None, timeout=15, retries=3):
    """带重试的 GET; 返回 bytes (空表示失败)。"""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    for i in range(retries):
        try:
            return urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=timeout).read()
        except Exception:
            if i < retries - 1:
                time.sleep(0.4 * (i + 1))
    return b""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _prefix(code):
    """裸 6 位码 → (prefix, ticker)。与腾讯代码规则一致。"""
    c = str(code).strip().lower()
    c = re.sub(r"^(sh|sz|bj)", "", c)
    c = c.zfill(6)
    if c[0] in "69":
        return "sh", c
    if c[0] in "03":
        return "sz", c
    if c[0] in "920":
        return "bj", c
    if c[0] in "4568":
        return "sh", c      # 沪市基金/转债
    return "sz", c


def qt_snapshot(symbol):
    """腾讯行情快照 → (交易日YYYY-MM-DD, 时刻HHMMSS, 当日成交额元, 盘后成交额元)。

    盘后成交额是下标 58 字段 (万元, 4 位小数); 缺失时为 None (只跳过盘后核对)。
    """
    body = _http(QT_URL + symbol)
    if not body:
        raise RuntimeError(f"腾讯快照 {symbol} 请求失败")
    text = body.decode("gbk", "replace")
    if "v_pv_none_match" in text:
        raise ValueError(f"腾讯没有 {symbol} 这个代码")
    m = re.search(rf'v_{symbol}="([^"]*)"', text)
    if not m:
        raise RuntimeError(f"腾讯快照 {symbol} 返回格式改变 (无 v_{symbol} 变量)")
    fields = m.group(1).split("~")
    if len(fields) < 36 or not re.fullmatch(r"[0-9]{14}", fields[30]):
        raise RuntimeError(
            f"腾讯快照 {symbol} 字段数 {len(fields)} 或时间字段不对, 格式可能已变")
    parts = fields[35].split("/")          # 最新价/成交量/成交额(元)
    if len(parts) != 3:
        raise RuntimeError(f"腾讯快照 {symbol} 价/量/额字段是 {fields[35]!r}, 格式可能已变")
    after = fields[58] if len(fields) > 58 else ""
    after = round(float(after) * 1e4, 2) if re.fullmatch(r"\d+(\.\d+)?", after) else None
    return fields[30][:8], fields[30][8:], _num(parts[2]) or 0.0, after


def tick_page(symbol, page):
    """第 page 页逐笔 (0 起) → 记录列表; 翻过最后一页腾讯返回空内容 → None。"""
    body = _http(TICK_URL, params={"appn": "detail", "action": "data",
                                   "c": symbol, "p": page})
    if not body:
        return None
    text = body.decode("gbk", "replace").strip()
    if not text:
        return None
    m = re.fullmatch(rf'v_detail_data_{symbol}=\[(\d+),"([^"]*)"\];?', text)
    if not m or int(m.group(1)) != page:
        raise RuntimeError(f"腾讯逐笔 {symbol} 第 {page} 页不是预期格式: {text[:80]!r}")
    if not m.group(2):
        return None
    records = []
    try:
        for item in m.group(2).split("|"):
            seq, clock, price, change, volume, amount, side = item.split("/")
            if not re.fullmatch(r"\d\d:\d\d:\d\d", clock) or side not in ("B", "S", "M"):
                raise ValueError(item)
            records.append({"seq": int(seq), "time": clock,
                            "price": _num(price), "change": _num(change),
                            "volume": _num(volume), "amount": _num(amount),
                            "side": side})
    except ValueError as exc:
        raise RuntimeError(f"腾讯逐笔 {symbol} 第 {page} 页记录格式改变: {exc}") from exc
    return records


def fetch_ticks(code):
    """单只股票逐笔 → dict (rows + meta)。异常按类型抛出, 由调用方决定跳过。"""
    prefix, ticker = _prefix(code)
    if prefix == "bj":
        raise ValueError("腾讯逐笔不支持北交所")
    if (prefix, ticker[:3]) in (("sh", "000"), ("sz", "399")):
        raise ValueError(f"{prefix}{ticker} 是指数, 没有逐笔成交")
    symbol = prefix + ticker

    day, clock, amount_before, ah_before = qt_snapshot(symbol)
    if amount_before == 0:
        raise ValueError(f"{symbol} 在 {day} 没有成交 (停牌/未开盘/竞价未撮合)")

    rows, missing = [], []
    for page in range(MAX_PAGES):
        records = tick_page(symbol, page)
        if records is None:
            break
        for r in records:
            expected = rows[-1]["seq"] + 1 if rows else 0
            if r["seq"] < expected or (rows and r["time"] < rows[-1]["time"]):
                raise RuntimeError(
                    f"腾讯逐笔 {symbol} 序号或时间倒退 (第 {page} 页 {r['seq']} {r['time']})")
            if r["seq"] > expected:
                if r["time"] <= SESSION_END:
                    raise RuntimeError(
                        f"腾讯逐笔 {symbol} 缺序号 {expected}-{r['seq']-1} "
                        f"({r['time']} 之前, 第 {page} 页), 腾讯该页缓存不完整")
                missing.extend(range(expected, r["seq"]))
            rows.append(r)
        time.sleep(0.1)
    else:
        raise RuntimeError(f"腾讯逐笔 {symbol} 翻到第 {MAX_PAGES} 页仍未结束")

    if not rows:
        if clock < "092500":
            raise ValueError(f"{symbol} 集合竞价尚未撮合 ({clock}), 还没有逐笔")
        raise RuntimeError(
            f"{symbol} 在 {day} 成交 {amount_before:.0f} 元但逐笔为空 "
            "(开盘前腾讯可能已清空上一交易日明细, 否则接口变了)")

    day_after, clock_after, amount_after, ah_after = qt_snapshot(symbol)
    if day_after != day:
        raise RuntimeError(f"取数期间交易日从 {day} 变成 {day_after}, 请重试")

    session = sum(r["amount"] for r in rows if r["time"] <= SESSION_END)
    amount_ok = True
    # 两次快照成交额相同 → 取数期间无新成交, 此时连续竞价段合计应等于当日成交额
    if amount_after == amount_before and \
            abs(session - amount_before) > amount_before * 0.001 + 1000:
        amount_ok = False

    ah_rows = [r["amount"] for r in rows if r["time"] > SESSION_END]
    complete = None
    if missing:
        complete = False
    elif (amount_after == amount_before and ah_before is not None
          and ah_after == ah_before and clock >= "153100"):
        complete = abs(sum(ah_rows) - ah_before) <= len(ah_rows) + 1

    # 派生指标: 主动买卖占比 / 竞价撮合 / 大单集中度 (供 ban_prob/premarket 用)
    buy_amt = sum(r["amount"] for r in rows if r["side"] == "B" and r["time"] <= SESSION_END)
    sell_amt = sum(r["amount"] for r in rows if r["side"] == "S" and r["time"] <= SESSION_END)
    neutral_amt = sum(r["amount"] for r in rows if r["side"] == "M" and r["time"] <= SESSION_END)
    active_total = buy_amt + sell_amt
    auction = [r for r in rows if r["time"] < "09:30:00"]

    return {
        "code": symbol,
        "date": day,
        "snapshot_clock": clock,
        "n_ticks": len(rows),
        "session_amount": round(session, 2),
        "snapshot_amount": round(amount_before, 2),
        "amount_matches": amount_ok,
        "after_hours_amount": round(sum(ah_rows), 2),
        "after_hours_snapshot": ah_before,
        "complete": complete,
        "missing_seq": missing,
        "buy_amount": round(buy_amt, 2),
        "sell_amount": round(sell_amt, 2),
        "neutral_amount": round(neutral_amt, 2),
        "buy_ratio": round(buy_amt / active_total * 100, 2) if active_total else None,
        "auction": auction,
        "auction_amount": round(sum(r["amount"] for r in auction), 2),
        "ticks": rows,
    }


def main():
    ap = argparse.ArgumentParser(description="腾讯逐笔成交采集 (分笔明细)")
    ap.add_argument("--code", help="单只代码 (裸码或带 sh/sz 前缀)")
    ap.add_argument("--codes", help="多只, 逗号分隔")
    ap.add_argument("--out", help="输出文件 (默认 data/ticks_<date>.json)")
    ap.add_argument("--json", action="store_true",
                    help="单只模式输出全量逐笔 (默认输出汇总)")
    ap.add_argument("--summary-only", action="store_true",
                    help="不写 ticks 明细, 只留派生指标 (文件小很多)")
    args = ap.parse_args()

    if not args.code and not args.codes:
        ap.error("需要 --code 或 --codes")
    codes = [c for c in (args.codes or args.code).split(",") if c.strip()]

    now = datetime.now(CST)
    results, failed = [], {}
    for c in codes:
        c = c.strip()
        try:
            r = fetch_ticks(c)
            if args.summary_only:
                r.pop("ticks", None)
            results.append(r)
            print(f"  {r['code']} {r['date']} {r['n_ticks']}笔 "
                  f"主动买占比{r['buy_ratio']}% 竞价{r['auction_amount']:.0f}元 "
                  f"complete={r['complete']} amount_ok={r['amount_matches']}", flush=True)
        except Exception as e:
            failed[c] = f"{type(e).__name__}: {e}"
            print(f"  {c} FAILED {failed[c]}", file=sys.stderr, flush=True)

    if args.code and args.json:
        payload = results[0] if results else None
    else:
        payload = {
            "ts": now.isoformat(timespec="seconds"),
            "engine": "fetchers_ticks.py",
            "source": "腾讯 stock.gtimg.cn/data/index.php 成交明细 (HTTP 分页)",
            "origin": "借鉴 simonlin1212/a-stock-data (Apache-2.0) §1.4",
            "n_ok": len(results),
            "n_failed": len(failed),
            "failed": failed,
            "items": results,
            "meta": {
                "caliber": "约3秒一笔的分笔 (非Level-2逐笔委托); 09:25那笔=开盘集合竞价撮合",
                "coverage": "沪深个股(含创业板/科创板)+ETF; 不含北交所与指数",
                "unit": "volume=手, amount=元; 只用amount核对 (科创板volume累加有误差)",
                "integrity": "收盘后核对连续竞价段成交额 vs 快照 (容差0.1%+1000元); "
                             "盘后段缺笔记 missing_seq; 15:31后用快照盘后额核对→complete",
                "caveat": "只有最近一个交易日, 无历史; 开盘前调用可能拿到空",
            },
        }

    out = args.out or os.path.join(AI_VIEW_DATA, f"ticks_{now.strftime('%Y%m%d')}.json")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"→ {out}  (ok={len(results)} failed={len(failed)})")


if __name__ == "__main__":
    main()
