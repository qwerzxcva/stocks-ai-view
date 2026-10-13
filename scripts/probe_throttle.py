#!/usr/bin/env python3
"""
probe_throttle.py — 探测节流 (同一源 24h 内不重复实测)

配合 domain_lease.py 使用:
  domain_lease  管「哪个 agent 负责哪个域」 (横向分工)
  probe_throttle 管「哪个源最近被测过」   (纵向防重复)

问题: 三个子代理都去测腾讯行情/东财/新浪, 同一端点被请求几十次,
既浪费额度又可能触发 WAF。

机制:
  probe_log.json 记录 {source_id: {last_probe, http_code, ok, agent}}
  --check <source>  退出码 0=可以测, 1=24h 内已测过 (跳过)
  --record <source> --code 200 --ok 1 --agent 破甲
  --show            看全部记录

用法 (在采集脚本里):
  from probe_throttle import should_probe, record_probe
  if should_probe("tencent_quote", agent="破甲"):
      ... 实测 ...
      record_probe("tencent_quote", 200, True, "破甲")
  else:
      print("24h 内已测过, 跳过")
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG = os.environ.get("STOCKS_PROBE_LOG") or os.path.abspath(os.path.join(
    os.path.dirname(os.path.dirname(HERE)), "stocks-data-live", "data",
    "research", "probe_log.json"))

TTL_H = 24


def _load(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"probes": {}}


def _save(path, d):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    d["updated_at"] = datetime.now(CST).isoformat(timespec="seconds")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def should_probe(source_id, ttl_h=TTL_H, log_path=None):
    """True = 可以测 (从未测过或已过期)。"""
    path = log_path or DEFAULT_LOG
    d = _load(path)
    rec = (d.get("probes") or {}).get(source_id)
    if not rec:
        return True
    last = rec.get("last_probe")
    if not last:
        return True
    try:
        age_h = (datetime.now(CST) - datetime.fromisoformat(last)).total_seconds() / 3600
    except ValueError:
        return True
    return age_h > ttl_h


def record_probe(source_id, http_code=None, ok=None, agent="", log_path=None, note=""):
    path = log_path or DEFAULT_LOG
    d = _load(path)
    d.setdefault("probes", {})[source_id] = {
        "last_probe": datetime.now(CST).isoformat(timespec="seconds"),
        "http_code": http_code, "ok": ok, "agent": agent, "note": note,
    }
    _save(path, d)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", help="日志路径")
    ap.add_argument("--check", metavar="SOURCE", help="检查某源是否可测 (退出码 0/1)")
    ap.add_argument("--record", metavar="SOURCE", help="记录一次探测")
    ap.add_argument("--code", type=int, help="HTTP 码")
    ap.add_argument("--ok", type=int, choices=[0, 1], help="1=成功 0=失败")
    ap.add_argument("--agent", default="", help="探测者")
    ap.add_argument("--note", default="")
    ap.add_argument("--ttl", type=float, default=TTL_H)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    path = args.log or DEFAULT_LOG

    if args.check:
        ok = should_probe(args.check, args.ttl, path)
        d = _load(path)
        rec = (d.get("probes") or {}).get(args.check)
        if ok:
            print(f"✅ {args.check}: 可探测" + (f" (上次 {rec.get('last_probe')[:19]}, 已过期)"
                                              if rec else " (从未探测)"))
            return 0
        print(f"⏭️  {args.check}: {args.ttl}h 内已探测 "
              f"({rec.get('last_probe')[:19]}, code={rec.get('http_code')}, "
              f"agent={rec.get('agent')}) → 跳过")
        return 1

    if args.record:
        record_probe(args.record, args.code, bool(args.ok) if args.ok is not None else None,
                     args.agent, path, args.note)
        print(f"✅ 记录 {args.record} code={args.code} ok={args.ok} agent={args.agent}")
        return 0

    if args.show:
        d = _load(path)
        probes = d.get("probes") or {}
        if not probes:
            print("(无探测记录)")
            return 0
        print(f"{'source':28s} {'last_probe':20s} {'code':6s} {'ok':4s} agent")
        print("-" * 80)
        for sid in sorted(probes):
            r = probes[sid]
            print(f"{sid:28s} {(r.get('last_probe') or '')[:19]:20s} "
                  f"{str(r.get('http_code')):6s} {str(r.get('ok')):4s} {r.get('agent','')}")
        print(f"\n共 {len(probes)} 个源")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
