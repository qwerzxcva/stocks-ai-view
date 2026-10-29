#!/usr/bin/env python3
"""app_test_matrix.py — 把端上 gradle 测试结果拆成逐用例 PASS/FAIL 矩阵。

## 为什么

`./gradlew :engine-algo:test` 只给你一句 `BUILD SUCCESSFUL` 和一个总数，
问不出「哪些用例真的跑了」「哪个类一条用例都没有」「新增的执法点到底在不在里面」。
这个脚本直接读 gradle 的 JUnit XML（`build/test-results/test/*.xml`），
逐用例列出，并按类汇总，最后把「0 用例的测试类」和「测试完全没提及的屏」点出来。

    ./gradlew :engine-algo:test :engine-runtime:test     # 先跑
    python3 scripts/app_test_matrix.py                    # 再出矩阵
    python3 scripts/app_test_matrix.py -k parity          # 过滤
    python3 scripts/app_test_matrix.py --summary-only     # 只看类级汇总
"""
import argparse
import glob
import os
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(os.path.dirname(ROOT), "skapp-app")

# 端上屏幕清单（与 verify_apk_surface.py 的 SCREENS 对齐）：用来看「这一屏有没有任何测试提及」
SCREENS = [
    "DecisionScreen", "EnginesScreen", "SectorsScreen", "PicksScreen", "BoardScreen",
    "PoolsScreen", "DashboardScreen", "SummaryScreen", "MarketScreen", "NewsScreen",
    "AiScreen", "UnifiedAskScreen", "RealtimeSummaryScreen", "StockDetailSheet",
    "StockDetailCharts", "StockDetailTail", "DiagScreen", "ThemeScreen", "HubScreen",
]


def find_results():
    return sorted(glob.glob(os.path.join(APP, "*", "build", "test-results", "test", "TEST-*.xml")))


def parse(path):
    items = []
    root = ET.parse(path).getroot()
    suite = root.get("name") or os.path.basename(path)
    for case in root.iter("testcase"):
        name = case.get("name") or "?"
        cls = case.get("classname") or suite
        failure = case.find("failure")
        skipped = case.find("skipped")
        status = "FAIL" if failure is not None else ("SKIP" if skipped is not None else "PASS")
        detail = ""
        if failure is not None:
            detail = (failure.get("message") or "").strip().splitlines()[0][:160]
        items.append((cls, name, status, float(case.get("time") or 0), detail))
    return suite, items


def main():
    ap = argparse.ArgumentParser(description="端上逐用例测试矩阵")
    ap.add_argument("-k", "--filter", help="只显示名字含该子串的用例/类")
    ap.add_argument("--summary-only", action="store_true")
    args = ap.parse_args()

    paths = find_results()
    if not paths:
        print("[app-matrix] 没有测试结果，先跑 ./gradlew :engine-algo:test")
        return 2

    all_items = []
    for p in paths:
        _, items = parse(p)
        all_items.extend(items)

    shown = [i for i in all_items if not args.filter
             or args.filter in i[0] or args.filter in i[1]]
    failed = [i for i in shown if i[2] == "FAIL"]
    skipped = [i for i in shown if i[2] == "SKIP"]

    if not args.summary_only:
        for cls, name, status, secs, detail in sorted(shown):
            if status == "PASS":
                continue
            print("%-4s %s.%s  %.2fs  %s" % (status, cls.rsplit(".", 1)[-1], name, secs, detail))

    by_class = {}
    for cls, name, status, secs, _ in shown:
        agg = by_class.setdefault(cls, {"PASS": 0, "FAIL": 0, "SKIP": 0, "t": 0.0})
        agg[status] += 1
        agg["t"] += secs

    print("\n[app-matrix] 按测试类：")
    for cls in sorted(by_class):
        a = by_class[cls]
        total = a["PASS"] + a["FAIL"] + a["SKIP"]
        print("  %-62s %3d 例  %5.2fs  %s" % (
            cls, total, a["t"],
            "OK" if a["FAIL"] == 0 else ("FAIL %d 例" % a["FAIL"])))

    # 覆盖审计：哪些屏没有任何测试提及
    names = " ".join(cls + " " + nm for cls, nm, *_ in all_items)
    unmentioned = [s for s in SCREENS if s not in names]
    print("\n[app-matrix] 测试完全未提及的屏幕（%d/%d）：" % (len(unmentioned), len(SCREENS)))
    print("  " + ("、".join(unmentioned) if unmentioned else "（无，全部有提及）"))

    print("\n[app-matrix] %d 个用例：%d 通过 / %d 失败 / %d 跳过（%d 个测试类）" % (
        len(shown), len(shown) - len(failed) - len(skipped), len(failed), len(skipped), len(by_class)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
