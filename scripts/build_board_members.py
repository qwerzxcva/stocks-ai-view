#!/usr/bin/env python3
"""build_board_members.py — 板块/概念成分股构建 (2026-10-19)

为什么必须新建: 现有 concept_map.json 有 180 个概念, 但**每个概念只有 1 只股票**
(白酒Ⅱ 只有 000995, 银行只有 601328), 是残留的残缺数据。
news_impact 拿它做「消息 -> 个股」映射时找不到成分股, 于是退化到 ETF,
导致 60 条消息里 30 条映射到 159931/512600 之类的指数 ETF —— 消息面选股
实际上完全失效。

数据源: 东财板块成分 (push2.eastmoney.com clist, fs=b:BKxxxx)
  实测 BK0475 (银行) 返回 42 只真实成分股。
校验: 剔除 ETF/基金代码 (159/510/511/512/513/515/588), 记录每板块成分数,
      成分数 < 3 的板块标记为可疑 (与旧 map 同病) 而不是静默保留。

产出: data/board_members.json
  {"ts","source","n_boards","boards":{BK0475:{"name","n","stocks":[{code,name}]}}}
用法:
  python3 build_board_members.py                 # 全量板块
  python3 build_board_members.py --boards BK0475,BK0476
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

HDR = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"
ETF_PREFIX = ("159", "510", "511", "512", "513", "515", "516", "518",
              "560", "561", "562", "563", "588", "501", "502")


def _curl(url, timeout=25):
    """用 curl 而不是 urllib: 东财对 urllib 的 TLS 指纹直接断连。"""
    try:
        r = subprocess.run(["curl", "-s", "-m", str(timeout), "-A", HDR, url],
                           capture_output=True, timeout=timeout + 10)
        return r.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def list_boards():
    """全部板块列表 (行业 + 概念)。fs=m:90+t:2 行业, m:90+t:3 概念。"""
    out = {}
    for fs, kind in (("m:90+t:2", "industry"), ("m:90+t:3", "concept")):
        url = ("https://push2.eastmoney.com/api/qt/clist/get?"
               "pn=1&pz=500&po=1&np=1&fltt=2&invt=2&fid=f3"
               f"&fs={fs}&fields=f12,f14")
        raw = _curl(url)
        try:
            d = json.loads(raw)
        except Exception:
            print(f"[boards] {kind} 列表解析失败", file=sys.stderr)
            continue
        for x in ((d.get("data") or {}).get("diff") or []):
            code = x.get("f12")
            name = x.get("f14")
            if code and name:
                out[code] = {"name": name, "kind": kind}
        time.sleep(0.8)
    return out


def board_stocks(bk):
    """单板块全部成分股 (分页拉, 剔除 ETF)。"""
    stocks, pn = [], 1
    while pn <= 20:
        url = ("https://push2.eastmoney.com/api/qt/clist/get?"
               f"pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
               f"&fs=b:{bk}&fields=f12,f14")
        raw = _curl(url)
        try:
            d = json.loads(raw)
        except Exception:
            break
        dd = d.get("data") or {}
        diff = dd.get("diff") or []
        if not diff:
            break
        for x in diff:
            code = str(x.get("f12") or "")
            name = x.get("f14") or ""
            if not code or code in ("-",):
                continue
            if code[:3] in ETF_PREFIX:
                continue
            stocks.append({"code": code, "name": name})
        if len(diff) < 100:
            break
        pn += 1
        time.sleep(0.35)
    return stocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", help="逗号分隔板块代码, 留空=全部")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "board_members.json"))
    ap.add_argument("--max-boards", type=int, default=0)
    args = ap.parse_args()
    now = datetime.now(CST)

    if args.boards:
        targets = {b.strip(): {"name": "", "kind": "manual"}
                   for b in args.boards.split(",") if b.strip()}
    else:
        targets = list_boards()
        print(f"[boards] 板块总数: {len(targets)}", flush=True)
    if args.max_boards:
        targets = dict(list(targets.items())[:args.max_boards])

    boards, suspect = {}, []
    t0 = time.time()
    for i, (bk, meta) in enumerate(targets.items()):
        st = board_stocks(bk)
        if not st:
            continue
        boards[bk] = {"name": meta.get("name") or bk,
                      "kind": meta.get("kind") or "unknown",
                      "n": len(st), "stocks": st}
        if len(st) < 3:
            suspect.append(bk)
        if (i + 1) % 20 == 0:
            print(f"[boards] {i+1}/{len(targets)} ok={len(boards)} "
                  f"{time.time()-t0:.0f}s", flush=True)
        time.sleep(0.3)

    payload = {
        "ts": now.isoformat(timespec="seconds"),
        "source": "eastmoney clist b:BKxxxx (ETF excluded)",
        "n_boards": len(boards),
        "n_pairs": sum(b["n"] for b in boards.values()),
        "suspect_boards": suspect[:50],
        "boards": boards,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print(f"[boards] {len(boards)} 个板块, {payload['n_pairs']} 条板块-个股对, "
          f"可疑(<3只) {len(suspect)} 个")
    print(f"[boards] -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())