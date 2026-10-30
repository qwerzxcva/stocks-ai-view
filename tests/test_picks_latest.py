#!/usr/bin/env python3
"""picks_latest 跨仓契约的口径测试。

锁三件事（踩过的坑，不是装饰）：
1. 6 位代码必须带市场前缀：skapp 侧行情 map 是 sh600519 口径，裸 6 位查不到，
   picks 会全退化成 ref_price=None，复盘恒 0。
2. asof 取条目日期众数，不取文件 date：today_picks.json 的 date 会领先条目两天，
   写错会让 skapp 快照的 stale 标记说谎。
3. 认不出市场的代码进 skipped，不静默丢。
"""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import build_picks_latest as BPL  # noqa: E402

CST = timezone(timedelta(hours=8))


class ToSymbolTest(unittest.TestCase):
    def test_bare_six_digits_get_exchange_prefix(self):
        self.assertEqual(BPL.to_symbol("002590"), "sz002590")
        self.assertEqual(BPL.to_symbol("600519"), "sh600519")
        self.assertEqual(BPL.to_symbol("000039"), "sz000039")

    def test_board_segments(self):
        self.assertEqual(BPL.to_symbol("688111"), "sh688111")
        self.assertEqual(BPL.to_symbol("689009"), "sh689009")
        self.assertEqual(BPL.to_symbol("300750"), "sz300750")
        self.assertEqual(BPL.to_symbol("301029"), "sz301029")
        self.assertEqual(BPL.to_symbol("830799"), "bj830799")
        self.assertEqual(BPL.to_symbol("920001"), "bj920001")
        # 900 B 股走沪市，920 北交所新段走 bj —— 两个 9 段不能混
        self.assertEqual(BPL.to_symbol("900901"), "sh900901")

    def test_already_normalised_forms(self):
        self.assertEqual(BPL.to_symbol("sh600519"), "sh600519")
        self.assertEqual(BPL.to_symbol("600519.SH"), "sh600519")
        self.assertEqual(BPL.to_symbol("600519sh"), "sh600519")
        self.assertEqual(BPL.to_symbol(" SZ000039 "), "sz000039")

    def test_unknown_returns_none(self):
        for bad in ("", None, "abc", "12345", "777777", "6005199"):
            self.assertIsNone(BPL.to_symbol(bad), bad)


class AsofTest(unittest.TestCase):
    def test_mode_of_item_dates_not_file_date(self):
        doc = {
            "date": "2026-10-30",
            "picks": {"signals": [
                {"code": "002590", "date": "2026-10-28", "score": 1.0},
                {"code": "603037", "date": "2026-10-28", "score": 0.9},
                {"code": "000058", "date": "2026-10-29", "score": 0.8},
            ]},
        }
        out = BPL.build(doc, now=datetime(2026, 10, 30, 15, 32, tzinfo=CST))
        self.assertEqual(out["asof"], "2026-10-28")

    def test_tie_takes_later_date(self):
        self.assertEqual(
            BPL.pick_asof(["2026-10-28", "2026-10-29"], "2026-10-30"), "2026-10-29")

    def test_fallback_to_file_date_without_item_dates(self):
        doc = {"date": "2026-10-30", "picks": {"signals": [{"code": "002590"}]}}
        out = BPL.build(doc, now=datetime(2026, 10, 30, 15, 32, tzinfo=CST))
        self.assertEqual(out["asof"], "2026-10-30")


class BuildShapeTest(unittest.TestCase):
    def test_items_ranked_and_prefixed(self):
        doc = {"date": "2026-10-30", "picks": {"signals": [
            {"code": "002590", "name": "奇瑞A", "score": 72.4, "price": 10.08, "date": "2026-10-28"},
            {"code": "603037", "name": "凯众股份", "score": 80.0, "price": 21.5, "date": "2026-10-28"},
        ]}}
        out = BPL.build(doc)
        self.assertEqual([it["symbol"] for it in out["items"]], ["sh603037", "sz002590"])
        self.assertEqual([it["rank"] for it in out["items"]], [1, 2])
        self.assertEqual(out["items"][1]["code"], "002590")
        self.assertEqual(out["source"], "today_picks_v5/signals")
        self.assertEqual(out["n_items"], 2)

    def test_unknown_code_goes_to_skipped(self):
        doc = {"date": "2026-10-30", "picks": {"signals": [
            {"code": "002590", "score": 1.0, "date": "2026-10-28"},
            {"code": "777777", "score": 2.0, "date": "2026-10-28"},
        ]}}
        out = BPL.build(doc)
        self.assertEqual(out["n_items"], 1)
        self.assertEqual(out["skipped"][0]["code"], "777777")

    def test_missing_numbers_stay_null_not_zero(self):
        doc = {"date": "2026-10-30", "picks": {"signals": [{"code": "002590"}]}}
        item = BPL.build(doc)["items"][0]
        self.assertIsNone(item["score"])
        self.assertIsNone(item["price"])

    def test_limit_and_empty_source(self):
        doc = {"date": "2026-10-30", "picks": {"signals": [
            {"code": "00259%d" % i, "score": i, "date": "2026-10-28"} for i in range(5)]}}
        self.assertEqual(len(BPL.build(doc, limit=3)["items"]), 3)
        self.assertEqual(BPL.build({"picks": {"signals": []}})["items"], [])


if __name__ == "__main__":
    unittest.main()
