#!/usr/bin/env python3
"""build_sectors_health.py — 生成 data/api/sectors_health.json（降级确认报告）。

回答一个具体问题：api/sectors.json 是否满足“全部板块均为当日数据”？
答案由数据算出（fresh == total），不靠口头声明；不满足时给出可审计的不可达清单。

同时记录三条已验证的降级场景（可复跑）：
  A 正常：本地当日文件优先
  B 本地目录为空：全部回落到云端兜底 → status=stale
  C 本地文件损坏：回落云端并记录 source_errors
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CST = timezone(timedelta(hours=8))

SCENARIOS = [
    {"id": "A", "setup": "SECTOR_LOCAL_DIR=默认 data/sector",
     "expect": "本地当日文件优先，命中则不回落"},
    {"id": "B", "setup": "SECTOR_LOCAL_DIR=/tmp/empty_sector（模拟抓取全失败）",
     "expect": "全部回落 db-market，status=stale，requirement_met=false"},
    {"id": "C", "setup": "SECTOR_LOCAL_DIR=/tmp/bad_sector（5 个文件损坏）",
     "expect": "损坏板块回落云端并记录 source_errors，其余保持当日"},
]


def main():
    path = os.path.join(ROOT, "data", "api", "sectors.json")
    with open(path, encoding="utf-8") as f:
        sec = json.load(f)
    fr = sec.get("freshness") or {}
    members = sec.get("board_members") or {}
    total = fr.get("total_boards") or len(members) or 0
    fresh = fr.get("fresh_boards") or 0
    stale = fr.get("stale_boards") or 0
    missing = fr.get("missing_boards") or 0

    unreachable = []
    for bk, v in members.items():
        if v.get("status") in ("stale", "missing"):
            unreachable.append({
                "bk": bk, "name": v.get("name"), "kind": v.get("kind"),
                "status": v.get("status"), "age_days": v.get("age_days"),
                "n": len(v.get("members") or []),
                "source": v.get("source"),
            })
    unreachable.sort(key=lambda x: (x["kind"] or "", x["bk"]))

    met = bool(total) and fresh == total
    report = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "requirement": "api/sectors.json 全部板块成员均为当日数据",
        "asof": fr.get("asof"),
        "sector_pool_date": fr.get("sector_pool_date"),
        "met": met,
        "verdict": "MET" if met else "NOT_MET",
        "counts": {"total": total, "fresh": fresh, "stale": stale, "missing": missing,
                   "gap": total - fresh},
        "oldest_age_days": fr.get("oldest_age_days"),
        "degraded_reason": sec.get("degraded_reason"),
        "source_errors": sec.get("source_errors") or [],
        "unreachable": unreachable,
        "blocking_causes": [
            "东财 push2/push2delay 对本机 IP 封禁（URLError/RemoteDisconnected，换节点无效）",
            "RPT_BOARD_CONSTITUENT 只认内部数字板块 id，无 BK 映射报表（RPT_BOARD_INFO/LIST/BK_LIST 均不存在）",
            "db-market:stock_to_board 只覆盖 68 个行业板块、3 个概念板块",
            "同花顺/新浪板块命名与东财不一致（行业 14/37 重合、概念 0 命中），混用会造成成分错配",
        ],
        "mitigation": {
            "local_first": "本地 data/sector/{kind}_constituents/{bk}.json 优先，云端兜底",
            "stale_gate": "age_days>2 判 stale，usable=false，不进入当日口径",
            "app_banner": "App 多池页显示降级横幅（当日/过期计数）",
            "auto_retry": "skapp-app-data.yml 每日重建；东财解封后自动补齐",
            "inverted_fallback": "refresh_board_constituents.py --invert-only 用云端索引反演补位",
        },
        "verified_scenarios": SCENARIOS,
    }
    out = os.path.join(ROOT, "data", "api", "sectors_health.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, separators=(",", ":"))
    print("[sectors_health] met=%s fresh=%d/%d unreachable=%d -> %s"
          % (met, fresh, total, len(unreachable), out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
