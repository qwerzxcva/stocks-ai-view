#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/global_kline.py ═══
# 上游 commit: 92c573679c66 · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
# 适配: 2026-10-13 路径规范 (输出 stockdb/wh → data/global_kline.json; --out)
"""
global_kline.py — 海外指数/全球资产历史日K (腾讯usfqkline/hkfqkline, 2026-10-13实测通)
关键: param={sym},day,,,N,qfq (count位留空!) ; 港股用hkfqkline
标的: usDJI/usIXIC/usINX(标普)/hkHSI/hkHSTECH/usVIX等
输出: data/global_kline.json (全量) — 供全球传导引擎/云推演用
"""
import argparse
import datetime
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
SYMS = ["usDJI", "usIXIC", "usINX", "hkHSI", "hkHSTECH", "hkHSTECH50"]


def fetch(sym, n=250):
    ep = "hkfqkline" if sym.startswith("hk") else "usfqkline"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/{ep}/get?param={sym},day,,,{n},qfq"
    d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read())
    node = d.get("data", {}).get(sym)
    if not isinstance(node, dict):
        return []
    rows = node.get("qfqday") or node.get("day") or []
    out = []
    for r in rows:
        if isinstance(r, list) and len(r) >= 6:
            try:
                out.append([r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])])
            except (ValueError, TypeError):
                pass
    return out


def main():
    ap = argparse.ArgumentParser(description="海外指数/全球资产历史日K")
    ap.add_argument("--bars", type=int, default=250, help="每标的拉取根数")
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "global_kline.json"))
    args = ap.parse_args()

    out = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "symbols": {}}
    for s in SYMS:
        try:
            bars = fetch(s, args.bars)
            if bars:
                out["symbols"][s] = {"n": len(bars), "bars": bars}
                last = bars[-1]
                prev = bars[-2][2] if len(bars) > 1 else last[2]
                out["symbols"][s]["last"] = {"date": last[0], "close": last[2], "chg_pct": round((last[2] / prev - 1) * 100, 2) if prev else None}
                print(s, len(bars), "bars, last", last[0], last[2])
            else:
                print(s, "EMPTY")
        except Exception as e:
            print(s, "FAIL", str(e)[:60])
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
