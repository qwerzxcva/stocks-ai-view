#!/usr/bin/env python3
"""
probe_rate_limit.py — 实测 push2delay 在 60 秒频率下的限流行为

为什么必须实测 (子代理指出的前置风险):
  分钟级方案成立的前提是采集源能扛住 60s 频率。子代理基于腾讯旧结论
  (qt.gtimg.cn 单 IP 1750 次后 501) 推演「19 分钟触发限流」, 但我们的
  realtime_minute.py 用的是 push2delay 全市场快照 (60 页/轮),
  配额模型完全不同 → 必须实测。

测法:
  模拟真实采集: 每轮 60 页 (pz=100), 轮间隔 60 秒, 连续跑 N 轮,
  记录每页的 HTTP 状态/rc 字段/耗时, 统计失败率与首次失败时间。

用法:
  python3 probe_rate_limit.py --rounds 30 --interval 60     # 30 分钟实测
  python3 probe_rate_limit.py --rounds 3 --interval 5       # 快速冒烟 (15 秒)
"""
import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://quote.eastmoney.com/"}
URL = ("https://push2delay.eastmoney.com/api/qt/clist/get"
       "?pn={page}&pz=100&po=1&np=1"
       "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
       "&fields=f12,f14,f2,f3,f6,f62&fid=f3")


def fetch_page(page, timeout=20):
    t0 = time.time()
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(URL.format(page=page), headers=UA), timeout=timeout)
        b = r.read()
        el = time.time() - t0
        try:
            d = json.loads(b.decode("utf-8"))
            rc = d.get("rc")
            n = len((d.get("data") or {}).get("diff") or [])
            return {"http": r.status, "rc": rc, "n": n, "ms": int(el * 1000),
                    "ok": rc == 0 and n > 0}
        except Exception:
            return {"http": r.status, "rc": None, "n": 0, "ms": int(el * 1000),
                    "ok": False, "err": "json-parse"}
    except Exception as e:
        return {"http": None, "rc": None, "n": 0, "ms": int((time.time() - t0) * 1000),
                "ok": False, "err": f"{type(e).__name__}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=30)
    ap.add_argument("--interval", type=int, default=60, help="轮间隔秒")
    ap.add_argument("--pages", type=int, default=60, help="每轮页数")
    ap.add_argument("--out", help="结果 JSON 路径")
    args = ap.parse_args()

    print(f"=== push2delay 限流实测 ===")
    print(f"轮数={args.rounds} 间隔={args.interval}s 每轮页数={args.pages}")
    print(f"预计耗时 {args.rounds * args.interval / 60:.1f} 分钟\n")

    results = []
    first_fail_round = None
    total_pages = fail_pages = 0
    for rnd in range(1, args.rounds + 1):
        round_fail = 0
        round_t0 = time.time()
        for pg in range(1, args.pages + 1):
            r = fetch_page(pg)
            r["round"] = rnd
            r["page"] = pg
            r["at"] = datetime.now(CST).isoformat(timespec="seconds")
            results.append(r)
            total_pages += 1
            if not r["ok"]:
                fail_pages += 1
                round_fail += 1
                if first_fail_round is None:
                    first_fail_round = rnd
                    print(f"  ⚠️ 第 {rnd} 轮第 {pg} 页首次失败: {r}")
            time.sleep(0.1)
        el = time.time() - round_t0
        ok_pages = args.pages - round_fail
        print(f"  轮 {rnd:3d}/{args.rounds}: {ok_pages}/{args.pages} 页成功, "
              f"耗时 {el:.1f}s, 累计失败 {fail_pages}/{total_pages} "
              f"({100*fail_pages/total_pages:.1f}%)")
        if rnd < args.rounds:
            time.sleep(args.interval)

    print(f"\n=== 结论 ===")
    print(f"总请求 {total_pages} 页, 失败 {fail_pages} "
          f"({100*fail_pages/total_pages:.2f}%)")
    if first_fail_round:
        print(f"首次失败在第 {first_fail_round} 轮 "
              f"(约 {first_fail_round * args.interval / 60:.1f} 分钟后)")
        print(f"→ 分钟级方案{'不可行' if fail_pages > total_pages * 0.1 else '可行但需重试兜底'}")
    else:
        print(f"全程无失败 → push2delay 在 {args.interval}s 频率下稳定, "
              f"分钟级方案可行 ✅")

    if args.out:
        summary = {
            "probe_at": datetime.now(CST).isoformat(timespec="seconds"),
            "source": "push2delay.eastmoney.com",
            "rounds": args.rounds, "interval_s": args.interval,
            "pages_per_round": args.pages,
            "total_pages": total_pages, "fail_pages": fail_pages,
            "fail_rate": round(fail_pages / total_pages, 4) if total_pages else 0,
            "first_fail_round": first_fail_round,
            "verdict": ("stable" if first_fail_round is None else
                        "needs_retry" if fail_pages <= total_pages * 0.1 else
                        "unstable"),
            "results": results,
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
