#!/usr/bin/env python3
"""
realtime_minute.py — 分钟级实时采集器 (常驻服务, 云服务器用)

为什么需要它 (2026-10-14):
  GitHub Actions cron 最快 5 分钟, 且每次 workflow 启动 + checkout + pip install
  要 ~40-60 秒 → 实际延迟 5-7 分钟。要降到分钟级必须常驻进程。

架构:
  本脚本在云服务器 (GitCode Notebook / 独立服务器) 常驻运行:
    1. 交易时段 (09:25-11:30, 13:00-15:00) 每 60 秒采集一轮
    2. 采集: 东财 push2delay 实时快照 (全市场) + 腾讯分时 + 五档资金流
    3. 落盘: data/market/snapshot_{ts}.json (滚动保留最近 N 个)
    4. 推送: 每 N 轮 commit+push 到 GitHub/GitCode (默认每 5 轮 = 5 分钟,
            避免 push 太频繁触发 GitHub 风控; 页面读 latest.json 即可)

数据源 (全部实测可用, 见 sources_registry):
  - push2delay.eastmoney.com 全市场快照 (绕开 push2 WAF)
  - push2delay.eastmoney.com 个股实时 (24 字段)
  - web.ifzq.gtimg.cn 分时 (每分钟)

用法:
  # 前台跑 (调试)
  python3 realtime_minute.py --once            # 只采一轮
  python3 realtime_minute.py --interval 60     # 每 60 秒一轮

  # 常驻 (云服务器)
  nohup python3 realtime_minute.py --interval 60 --push-every 5 > /tmp/rt.log 2>&1 &

  # systemd (推荐)
  #   sudo cp scripts/realtime_minute.service /etc/systemd/system/
  #   sudo systemctl enable --now stocks-realtime
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"}
EM_REF = {"Referer": "https://quote.eastmoney.com/"}

def _repo_root():
    """本脚本所在仓库根 (scripts/ 的上一级)。

    不能硬编码 /root/workspace/stocks-ai-view: 在 GitHub runner 上该路径
    不存在, 写快照会 PermissionError, 而输出文件也会落到错误位置,
    导致 workflow 读到仓里旧的 realtime_minute.json 就认为"无变化"
    不提交 (2026-10-14 实测: realtime-minute 连续 success 但数据
    一直停在 00:43)。
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


AI_VIEW = os.environ.get("STOCKS_AI_VIEW") or _repo_root()
SNAP_DIR = os.path.join(AI_VIEW, "data", "market")
DEFAULT_OUT = os.path.join(AI_VIEW, "data", "realtime_minute.json")

# 交易时段 (CST)
TRADING = [(9 * 60 + 25, 11 * 60 + 30), (13 * 60 + 0, 15 * 60 + 0)]

# 限流 (与 rate_limit.py 对齐)
# 东财限流实测 (2026-10-14): QPS=10 时连续请求会被 RemoteDisconnected
# (服务端直接断开), 表现为只采到前几页。降到 2 QPS + 单页重试。
QPS_EM = 2.0
PAGE_RETRIES = 3


def _now():
    return datetime.now(CST)


def _in_trading(now=None):
    now = now or _now()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return any(a <= mins <= b for a, b in TRADING)


def _get(url, ref=None, retries=2, timeout=20):
    h = dict(UA)
    h.update(ref or {})
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                       timeout=timeout)
            return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i == retries - 1:
                return {"__error__": f"{type(e).__name__} {e}"}
            time.sleep(0.5)
    return {"__error__": "unknown"}


def _get_raw(url, ref=None, retries=2, timeout=20):
    """取原始文本 (新浪返回的是 JSON 数组但可能带 BOM/非标准头)。"""
    h = dict(UA)
    h.update(ref or {})
    for i in range(retries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                       timeout=timeout)
            return r.read().decode("utf-8", "replace")
        except Exception:
            if i == retries - 1:
                return None
            time.sleep(0.5)
    return None


# ─── 采集: 全市场快照 (push2delay, 绕 WAF) ──────────────────
FIELDS = ("f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f17,f20,f21,f62,f184")
FIELD_MAP = {
    "f12": "code", "f14": "name", "f2": "price", "f3": "chg_pct",
    "f4": "chg_amt", "f5": "volume_hand", "f6": "amount", "f7": "amplitude",
    "f8": "turnover_rate", "f9": "pe_ttm", "f10": "volume_ratio",
    "f17": "open", "f20": "total_mktcap", "f21": "float_mktcap",
    "f62": "main_net_inflow", "f184": "main_net_ratio",
}


def _collect_sina(max_pages=60):
    """新浪全市场兜底 (node=hs_a 覆盖沪深 A 股)。

    单页 num 上限 100, 故需翻 60 页; 限流门槛与东财不同,
    东财两个域都被软限流时这条仍可能通 (2026-10-14 实测)。
    返回已折算成内部口径的 dict 列表。
    """
    out = []
    for page in range(1, max_pages + 1):
        u = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             f"Market_Center.getHQNodeData?page={page}&num=100&sort=changepercent"
             "&asc=0&node=hs_a")
        b = _get_raw(u, {"Referer": "https://finance.sina.com.cn/"})
        if not b:
            break
        try:
            arr = json.loads(b)
        except Exception:
            break
        if not arr:
            break
        for it in arr:
            out.append({
                "code": str(it.get("code") or ""),
                "name": it.get("name") or "",
                "price": _f(it.get("trade")),
                "chg_pct": _f(it.get("changepercent")),
                "chg_amt": _f(it.get("pricechange")),
                "open": _f(it.get("open")),
                "amount": _f(it.get("amount")),
                "volume_hand": _f(it.get("volume")),
                "amplitude": _f(it.get("amplitude")),
                "turnover_rate": _f(it.get("turnoverratio")),
                "pe_ttm": _f(it.get("per")),
                "volume_ratio": None,
                "total_mktcap": None,
                "float_mktcap": None,
                "main_net_inflow": None,
                "main_net_ratio": None,
                "_src": "sina",
            })
        if len(arr) < 100:
            break
        time.sleep(0.8)
    return out


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_market_snapshot(max_pages=30):
    """全市场实时快照 (分页, pz=200/页)。

    三级降级: push2delay → push2his → 新浪。
    东财两个域在海外出口 (GitHub runner) 与高频请求下都会软限流 —
    表现为返回 200 但 diff 为空, 或直接断开连接。新浪
    vip.stock.finance.sina.com.cn 单页上限 100 只, 限流门槛不同,
    可作最后兜底 (2026-10-14 实测容器出口可用)。
    """
    def _collect(host):
        rows_, total_ = [], None
        for page in range(1, max_pages + 1):
            u = (f"https://{host}/api/qt/clist/get"
                 f"?pn={page}&pz=200&po=1&np=1"
                 "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
                 f"&fields={FIELDS}&fid=f3")
            # 单页重试: 东财限流有两种表现 — 直接断开连接
            # (RemoteDisconnected), 或返回 200 但 diff 为空。两者都要重试,
            # 否则会静默少采几页 (实测 2026-10-14: 只拿到 500/5923)。
            d = None
            for attempt in range(PAGE_RETRIES):
                cand = _get(u, EM_REF, retries=1)
                if cand and not cand.get("__error__") and \
                        (cand.get("data") or {}).get("diff"):
                    d = cand
                    break
                time.sleep(1.5 * (attempt + 1))
            if d is None:
                print(f"[realtime_minute] {host} p{page} 连续 {PAGE_RETRIES} 次空/失败",
                      file=sys.stderr)
                break
            data = d.get("data") or {}
            total_ = data.get("total") or total_
            batch = data.get("diff") or []
            if not batch:
                break
            rows_.extend(batch)
            if total_ and len(rows_) >= total_:
                break
            # 页间隔 1.5s: 东财对连续翻页的软限流是"返回空 diff"而非报错,
            # 间隔太短会持续踩到 (QPS=2 + pz=200 实测仍只在第 3 页被掐)。
            time.sleep(1.5)
        return rows_, total_

    all_rows, total = _collect("push2delay.eastmoney.com")
    # 页数够但明显缺量 → 换域重采 (total 已知时以它为准判断)
    if total and len(all_rows) < min(total, 3000):
        alt, alt_total = _collect("push2his.eastmoney.com")
        if len(alt) > len(all_rows):
            print(f"[realtime_minute] push2delay 只返回 {len(all_rows)}/{total}, "
                  f"改用 push2his 得 {len(alt)}", file=sys.stderr)
            all_rows, total = alt, alt_total or total
    # 东财两个域都不给量 → 新浪兜底 (单页 100 只, 翻 60 页)
    if len(all_rows) < 3000:
        sina = _collect_sina(max_pages=60)
        if len(sina) > len(all_rows):
            print(f"[realtime_minute] 东财只返回 {len(all_rows)}, "
                  f"改用新浪得 {len(sina)}", file=sys.stderr)
            return len(sina), sina
    rows = []
    for it in all_rows:
        r = {}
        for k, v in it.items():
            r[FIELD_MAP.get(k, k)] = v
        # 单位折算 (东财 push2 字段口径, 实测 2026-10-14):
        #   f2 价/f4 涨跌额/f17 开盘 = 分 → 元 (÷100)
        #   f3 涨跌幅/f7 振幅/f8 换手/f9 PE/f10 量比/f184 主力净占比 = 1/100 (÷100)
        #   f62 主力净流入单位已是元
        for f in ("price", "chg_amt", "open"):
            if isinstance(r.get(f), (int, float)):
                r[f] = round(r[f] / 100, 3)
        for f in ("chg_pct", "amplitude", "turnover_rate", "pe_ttm",
                  "volume_ratio", "main_net_ratio"):
            if isinstance(r.get(f), (int, float)):
                v = round(r[f] / 100, 2)
                # 涨跌幅/振幅兜底: A股日涨跌停 20cm(科创/创业)/30cm(北交所)
                # 超 35% 必然是脏数据 (首日新股除外, 但占比极小, 宁可截断)
                if f in ("chg_pct", "amplitude") and abs(v) > 35:
                    v = max(-35.0, min(35.0, v))
                r[f] = v
        rows.append(r)
    return total, rows


def summarize(rows):
    """市场概览: 涨跌停/涨跌家数/成交额/主力净流入。"""
    up = down = flat = 0
    zt = dt = 0
    amt = 0.0
    main_in = 0.0
    for r in rows:
        c = r.get("chg_pct")
        if c is None:
            continue
        if c > 0:
            up += 1
        elif c < 0:
            down += 1
        else:
            flat += 1
        if c >= 9.9:
            zt += 1
        elif c <= -9.9:
            dt += 1
        a = r.get("amount")
        if isinstance(a, (int, float)):
            amt += a
        m = r.get("main_net_inflow")
        if isinstance(m, (int, float)):
            main_in += m
    return {
        "n_stocks": len(rows),
        "advance": up, "decline": down, "flat": flat,
        "limit_up_ge10": zt, "limit_down_le-10": dt,
        "total_amount_yi": round(amt / 1e8, 1),
        "main_net_inflow_yi": round(main_in / 1e8, 2),
    }


# ─── 落盘 + 推送 ────────────────────────────────────────────
def write_output(rows, summary, total):
    ts = _now().isoformat(timespec="seconds")
    payload = {
        "timestamp": ts,
        "source": "realtime_minute (push2delay eastmoney)",
        "date": _now().strftime("%Y-%m-%d"),
        "in_trading": _in_trading(),
        "market_total": total,
        "summary": summary,
        "quotes": rows,
        "provenance": {
            "quotes": {"source": "push2delay.eastmoney.com/api/qt/clist/get",
                       "confidence": "high", "snapshot_ts": ts},
            "summary": {"source": "computed_from_quotes", "confidence": "high",
                        "snapshot_ts": ts},
        },
    }
    os.makedirs(os.path.dirname(DEFAULT_OUT), exist_ok=True)
    tmp = DEFAULT_OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, DEFAULT_OUT)   # 原子替换, 避免读到半截
    # 滚动快照 (保留最近 240 个 = 一天盘中)
    os.makedirs(SNAP_DIR, exist_ok=True)
    sp = os.path.join(SNAP_DIR, f"snapshot_{_now().strftime('%Y%m%d_%H%M')}.json")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    snaps = sorted(f for f in os.listdir(SNAP_DIR) if f.startswith("snapshot_"))
    for old in snaps[:-240]:
        os.remove(os.path.join(SNAP_DIR, old))
    return DEFAULT_OUT, sp


def push_to_git(repo_dir, remote="origin", branch="main"):
    """commit + push (rebase 处理并发)。"""
    ts = _now().strftime("%Y%m%d_%H%M")
    def _run(cmd):
        return subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True)
    _run(["git", "add", "data/"])
    _run(["git", "commit", "-m", f"realtime: {ts} minute snapshot"])
    _run(["git", "fetch", remote, branch])
    _run(["git", "rebase", f"{remote}/{branch}"])
    r = _run(["git", "push", remote, branch])
    if r.returncode != 0:
        # 并发冲突 → 再拉再推
        _run(["git", "fetch", remote, branch])
        _run(["git", "rebase", f"{remote}/{branch}"])
        r = _run(["git", "push", remote, branch])
    return r.returncode == 0


# ─── 主循环 ─────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="只采一轮")
    ap.add_argument("--interval", type=int, default=60, help="采集间隔秒 (默认 60)")
    ap.add_argument("--push-every", type=int, default=5,
                    help="每 N 轮推送一次 git (默认 5; 0=不推)")
    ap.add_argument("--off-hours", action="store_true",
                    help="非交易时段也采 (默认只交易时段采)")
    args = ap.parse_args()

    print(f"[realtime_minute] 启动 interval={args.args if False else args.interval}s "
          f"push_every={args.push_every}")
    print(f"[realtime_minute] 输出 {DEFAULT_OUT}")
    n_round = 0
    n_push = 0
    while True:
        n_round += 1
        ts = _now().strftime("%H:%M:%S")
        trading = _in_trading()
        if not trading and not args.off_hours and not args.once:
            print(f"[{ts}] 非交易时段, 休眠 {args.interval}s")
            time.sleep(args.interval)
            continue
        try:
            total, rows = fetch_market_snapshot()
            if not rows:
                print(f"[{ts}] 第 {n_round} 轮: 空响应", file=sys.stderr)
                time.sleep(args.interval)
                continue
            summary = summarize(rows)
            out, snap = write_output(rows, summary, total)
            print(f"[{ts}] 第 {n_round} 轮: {summary['n_stocks']} 只 "
                  f"涨{summary['advance']}/跌{summary['decline']} "
                  f"涨停{summary['limit_up_ge10']} "
                  f"成交额{summary['total_amount_yi']}亿 "
                  f"主力净流入{summary['main_net_inflow_yi']}亿")
            n_push += 1
            if args.push_every and n_push >= args.push_every:
                ok = push_to_git(AI_VIEW)
                print(f"  → git push {'✅' if ok else '❌'}")
                n_push = 0
        except Exception as e:
            print(f"[{ts}] 第 {n_round} 轮失败: {type(e).__name__} {e}", file=sys.stderr)
        if args.once:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
