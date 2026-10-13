#!/usr/bin/env python3
"""
domain_lease.py — 子代理域认领表 (防重复抓取/重复搜索)

问题 (2026-10-13 实测):
  三个子代理并行跑, 都去搜「股票数据 API」, 三个都去测东财/新浪/雪球,
  合计烧掉 2900 万 token, 结论大量重复。用户要求: 每个账号分开抓取、分开搜索,
  减少重复和浪费 API 额度。

机制:
  1. 域认领表 (domain_lease.json): 记录每个 agent 认领了哪些域 (domain),
     哪些域已完成 (status=done), 哪些域已探测 (probed_at)。
  2. 派发前必查: `--available` 列出未被认领/已过期的域, 主会话只派这些。
  3. 派发时必须 `--claim <agent> <domain>` 认领, 否则报错。
  4. 完成后 `--release <agent> <domain> --status done` 标记, 附带产出文件。
  5. 节流: 同一域的探测记录 24h 内不重复派 (除非 --force)。

域定义 (与 sources_registry 的 data domain 对齐):
  realtime_quote   实时行情 (腾讯/新浪/东财 push2/push2his/TradingView)
  kline            K 线 (日K/周K/分钟K/复权)
  capital_flow     资金流/龙虎榜/两融/陆股通/大宗
  financial        财务/财报/业绩快报/三大报表
  news             新闻/快讯/舆情
  social           社交热度 (抖音/头条/雪球/小红书/股吧)
  fund_etf         基金/ETF 净值/持仓/费率
  macro            宏观 (CPI/GDP/PMI/货币)
  events           公告/停复牌/定增/新股/可转债
  exchange_official 交易所官方 API (上交所/深交所/巨潮)
  index            指数 (全史/周期/成分股)
  sector           板块/行业/概念
  algo             算法/因子/回测 (不涉及外部接口, 无节流)
  paid_api         付费 API 调研 (Tushare/问财/iTick/AllTick/TickDB 等)

用法:
  python3 domain_lease.py --list                     # 看全表
  python3 domain_lease.py --available                # 看可派发的域
  python3 domain_lease.py --claim 破甲 realtime_quote # 认领
  python3 domain_lease.py --release 破甲 realtime_quote --status done --output x.json
  python3 domain_lease.py --expire-stale             # 清理 24h 未完成的认领
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TABLE = os.environ.get("STOCKS_DOMAIN_LEASE") or os.path.abspath(os.path.join(
    os.path.dirname(os.path.dirname(HERE)), "stocks-data-live", "data",
    "research", "domain_lease.json"))

DOMAINS = {
    "realtime_quote": "实时行情 (腾讯/新浪/东财/TradingView)",
    "kline": "K 线 (日/周/分钟/复权)",
    "capital_flow": "资金流/龙虎榜/两融/陆股通/大宗",
    "financial": "财务/财报/业绩快报",
    "news": "新闻/快讯/舆情",
    "social": "社交热度 (抖音/头条/雪球/股吧)",
    "fund_etf": "基金/ETF 净值/持仓/费率",
    "macro": "宏观 (CPI/GDP/PMI)",
    "events": "公告/停复牌/新股/可转债",
    "exchange_official": "交易所官方 API",
    "index": "指数 (全史/周期/成分股)",
    "sector": "板块/行业/概念",
    "algo": "算法/因子/回测 (无外部接口)",
    "paid_api": "付费 API 调研 (Tushare/问财/iTick 等)",
    # ── 2026-10-13 新增 ──
    "level2": "Level2 逐笔/盘口/主力资金 (每分钟买卖数据)",
    "adjusted_full": "全市场复权数据 (qfq/hfq 批量)",
    "github_research": "GitHub 开源算法/引擎/架构调研",
}

LEASE_TTL_H = 24  # 认领有效期


def _load(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"table_version": 1, "updated_at": None, "leases": {}}


def _save(path, d):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    d["updated_at"] = datetime.now(CST).isoformat(timespec="seconds")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", help="认领表路径")
    ap.add_argument("--list", action="store_true", help="列出全表")
    ap.add_argument("--available", action="store_true", help="列出可派发的域")
    ap.add_argument("--claim", nargs=2, metavar=("AGENT", "DOMAIN"), help="认领域")
    ap.add_argument("--release", nargs=2, metavar=("AGENT", "DOMAIN"), help="释放/完成域")
    ap.add_argument("--status", default="done", help="--release 的状态: done/failed/partial")
    ap.add_argument("--output", help="--release 的产出文件")
    ap.add_argument("--note", help="--release 的备注")
    ap.add_argument("--expire-stale", action="store_true", help="清理过期认领")
    ap.add_argument("--force", action="store_true", help="忽略 TTL 强制认领")
    args = ap.parse_args()

    path = args.table or DEFAULT_TABLE
    d = _load(path)

    if args.list:
        print(f"{'domain':20s} {'agent':16s} {'status':10s} {'claimed_at':20s} output")
        print("-" * 90)
        leases = d.get("leases") or {}
        for dom in DOMAINS:
            L = leases.get(dom)
            if not L:
                print(f"{dom:20s} {'-':16s} {'free':10s}")
            else:
                print(f"{dom:20s} {L.get('agent',''):16s} {L.get('status',''):10s} "
                      f"{(L.get('claimed_at') or '')[:19]:20s} {L.get('output') or ''}")
        return 0

    if args.available:
        leases = d.get("leases") or {}
        now = datetime.now(CST)
        free, stale, busy = [], [], []
        for dom, desc in DOMAINS.items():
            L = leases.get(dom)
            if not L:
                free.append(dom)
                continue
            if L.get("status") == "done":
                continue
            ca = L.get("claimed_at")
            if ca:
                try:
                    age_h = (now - datetime.fromisoformat(ca)).total_seconds() / 3600
                    if age_h > LEASE_TTL_H:
                        stale.append((dom, L.get("agent"), age_h))
                        continue
                except ValueError:
                    pass
            busy.append((dom, L.get("agent"), L.get("status")))
        print("=== 可派发的域 (未被认领) ===")
        for dom in free:
            print(f"  {dom:20s} {DOMAINS[dom]}")
        if stale:
            print("\n=== 认领已过期 (>24h, 可重新派或先 --expire-stale) ===")
            for dom, ag, h in stale:
                print(f"  {dom:20s} 原认领者={ag} {h:.1f}h 前")
        if busy:
            print("\n=== 占用中 ===")
            for dom, ag, st in busy:
                print(f"  {dom:20s} {ag} ({st})")
        if not free and not stale:
            print("  (无) 所有域已被认领或完成")
        return 0

    if args.claim:
        agent, dom = args.claim
        if dom not in DOMAINS:
            print(f"未知域: {dom}\n可用: {list(DOMAINS)}", file=sys.stderr)
            return 1
        leases = d.setdefault("leases", {})
        L = leases.get(dom)
        if L and not args.force:
            if L.get("status") == "done":
                # done 是永久锁定: 该域已完成, 不再重复派 (除非 --force)
                print(f"❌ 域 {dom} 已由 {L.get('agent')} 完成 "
                      f"({(L.get('released_at') or L.get('claimed_at') or '')[:19]})",
                      file=sys.stderr)
                print(f"   产出: {L.get('output') or '(无)'}  备注: {L.get('note') or ''}",
                      file=sys.stderr)
                print(f"   若确需重派: 加 --force (会覆盖完成标记)", file=sys.stderr)
                return 1
            if L.get("status") != "expired":
                ca = L.get("claimed_at")
                age_h = None
                if ca:
                    try:
                        age_h = (datetime.now(CST) -
                                 datetime.fromisoformat(ca)).total_seconds() / 3600
                    except ValueError:
                        pass
                if age_h is None or age_h <= LEASE_TTL_H:
                    print(f"❌ 域 {dom} 已被 {L.get('agent')} 认领 "
                          f"({age_h:.1f}h 前, status={L.get('status')})", file=sys.stderr)
                    print(f"   若确需重派: 加 --force, 或先 --release {L.get('agent')} {dom}",
                          file=sys.stderr)
                    return 1
        leases[dom] = {
            "agent": agent, "status": "claimed",
            "claimed_at": datetime.now(CST).isoformat(timespec="seconds"),
            "domain_desc": DOMAINS[dom],
            "output": None,
        }
        _save(path, d)
        print(f"✅ {agent} 认领 {dom} ({DOMAINS[dom]})")
        return 0

    if args.release:
        agent, dom = args.release
        leases = d.setdefault("leases", {})
        L = leases.get(dom) or {}
        L.update({
            "agent": agent, "status": args.status,
            "claimed_at": L.get("claimed_at") or datetime.now(CST).isoformat(timespec="seconds"),
            "released_at": datetime.now(CST).isoformat(timespec="seconds"),
            "domain_desc": DOMAINS.get(dom, ""),
            "output": args.output,
            "note": args.note or "",
        })
        leases[dom] = L
        _save(path, d)
        print(f"✅ {agent} 释放 {dom} (status={args.status}, output={args.output})")
        return 0

    if args.expire_stale:
        leases = d.get("leases") or {}
        now = datetime.now(CST)
        n = 0
        for dom, L in list(leases.items()):
            if L.get("status") == "done":
                continue
            ca = L.get("claimed_at")
            if not ca:
                continue
            try:
                age_h = (now - datetime.fromisoformat(ca)).total_seconds() / 3600
            except ValueError:
                continue
            if age_h > LEASE_TTL_H:
                L["status"] = "expired"
                L["note"] = (L.get("note", "") +
                             f" | expired after {age_h:.1f}h (auto)").strip(" |")
                n += 1
        _save(path, d)
        print(f"标记 {n} 个过期认领为 expired")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
