#!/usr/bin/env python3
"""build_training_report.py — 拉取 t1 仓库 v7 训练结果并转为网站可读报告。

数据源 (另一台 AI 昨晚训练并上传, 只读拉取, 零鉴权):
  https://raw.githubusercontent.com/qwerzxcva/t1/main/meta/test{2016..2020}_v7_rounds.json
  https://raw.githubusercontent.com/qwerzxcva/t1/main/meta/v7_no_cash_recoup_report.md

v7 = 全程持仓引擎 (不空仓, 回本口径), 2016-2020 每年 60 轮共 300 轮。
win = 持有期内曾回本 (c >= 买入价); recoup22/recoup60 = 22/60 交易日内曾回本比例。

升级自纯 Markdown/JSON 展示: 站点只读渲染, 不带实际历史验证。诚实标注
「回本≠盈利」与数据来源, 不编造。

用法: python3 build_training_report.py [--out data/training_v7.json] [--dry-run]
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(ROOT, "data")

T1_BASE = "https://raw.githubusercontent.com/qwerzxcva/t1/main/meta/"
YEARS = list(range(2016, 2021))


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "stocks-ai-view/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fetch_rounds(year):
    """拉单年 rounds JSON, 返回 ({agg}, [rounds]) 或 (None, None)。"""
    url = f"{T1_BASE}test{year}_v7_rounds.json"
    try:
        d = json.loads(_get(url))
        return d.get("agg"), d.get("rounds")
    except Exception as e:
        print(f"[training] {year}: 拉取失败 {type(e).__name__}: {str(e)[:80]}",
              file=sys.stderr)
        return None, None


def fetch_report_md():
    """拉 v7 报告 Markdown, 仅提取结论行(表格前几段), 失败返回 None。"""
    try:
        md = _get(f"{T1_BASE}v7_no_cash_recoup_report.md")
        lines = [ln.strip() for ln in md.splitlines() if ln.strip()]
        head = []
        for ln in lines:
            if ln.startswith("|") or ln.startswith("#"):
                head.append(ln)
            elif head:
                break
        return {"head_md": "\n".join(head[:40])[:1500]}
    except Exception as e:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(DATA_DIR, "training_v7.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    now = datetime.now(CST)

    years = {}
    totals = {"rounds": 0, "recoup22_ge50": 0}
    for y in YEARS:
        agg, rounds = fetch_rounds(y)
        if not agg:
            continue
        years[str(y)] = {
            "rounds": agg.get("rounds"),
            "recoup22_mean": agg.get("recoup22_mean"),
            "recoup60_mean": agg.get("recoup60_mean"),
            "final_win_mean": agg.get("final_win_mean"),
            "beat_rate": agg.get("beat_rate"),
            "recoup22_ge50": agg.get("round_recoup22_ge50"),
            "t0_first": rounds[0].get("t0") if rounds else None,
            "t0_last": rounds[-1].get("t0") if rounds else None,
        }
        totals["rounds"] += agg.get("rounds") or 0
        totals["recoup22_ge50"] += agg.get("round_recoup22_ge50") or 0

    report_md = fetch_report_md()

    out = {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "engine": "v7 全程持仓 (不空仓, 回本口径)",
        "source": "qwerzxcva/t1/meta/test*_v7_rounds.json + v7_no_cash_recoup_report.md",
        "totals": totals,
        "years": years,
        "report": report_md,
        "publish_gate": {
            "required_years": len(YEARS),
            "fetched_years": len(years),
            "pass": len(years) >= 3,   # 至少 3 年才对外展示, 防止局部数据冒充全量
        },
        "disclaimer": "回本口径(曾达到买入价)≠盈利; 期末强平 win 才是落袋胜率。"
                      "历史回测不代表未来。",
    }

    if args.dry_run:
        print(json.dumps(out, ensure_ascii=False, indent=1)[:2500])
        return 0

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[training] -> {args.out} ({len(years)}/{len(YEARS)} 年)")
    print(f"[training] totals: {json.dumps(totals, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())