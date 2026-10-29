#!/usr/bin/env python3
"""涨停/主板口径的红线测试。

口径一旦被某处复制成私有阈值，端云与各脚本就会各判各的。这里锁住三件事：
1. 各板块的涨跌幅上限与触发线；
2. 涨停判定的边界（含 ST 5% 与 20cm/30cm）；
3. SQL 生成器与 Python 判定在同样输入下必须同结论（DuckDB 路径）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import limit_caliber as LC  # noqa: E402


class LimitPctTest(unittest.TestCase):
    def test_board_limits(self):
        self.assertEqual(LC.limit_pct("sh600000"), 10.0)
        self.assertEqual(LC.limit_pct("sz000001"), 10.0)
        self.assertEqual(LC.limit_pct("sz300750"), 20.0)
        self.assertEqual(LC.limit_pct("sh688111"), 20.0)
        self.assertEqual(LC.limit_pct("sh689009"), 20.0)
        self.assertEqual(LC.limit_pct("bj430047"), 30.0)
        self.assertEqual(LC.limit_pct("830799"), 30.0)

    def test_st_overrides_board(self):
        self.assertEqual(LC.limit_pct("600001", "*ST某某"), 5.0)
        self.assertEqual(LC.limit_pct("300001", "ST某某"), 5.0)
        self.assertEqual(LC.limit_pct_ratio("600001", "*ST某某"), 0.05)

    def test_bare_strips_prefix_and_suffix(self):
        self.assertEqual(LC.bare("sh600000"), "600000")
        self.assertEqual(LC.bare("600000.SH"), "600000")
        self.assertEqual(LC.limit_pct("600000.SH"), 10.0)


class LimitJudgementTest(unittest.TestCase):
    def test_main_board(self):
        self.assertTrue(LC.is_limit_up("sh600000", 9.99))
        self.assertTrue(LC.is_limit_up("sh600000", 9.85))
        self.assertFalse(LC.is_limit_up("sh600000", 9.67))

    def test_20cm_board(self):
        self.assertTrue(LC.is_limit_up("sz300750", 19.99))
        self.assertFalse(LC.is_limit_up("sz300750", 18.92))
        self.assertFalse(LC.is_limit_up("sh688519", 13.11))

    def test_30cm(self):
        self.assertTrue(LC.is_limit_up("bj430047", 29.99))
        self.assertFalse(LC.is_limit_up("bj430047", 19.99))

    def test_st_five_percent(self):
        self.assertTrue(LC.is_limit_up("600001", 4.98, "*ST某某"))
        self.assertFalse(LC.is_limit_up("600001", 4.7, "*ST某某"))

    def test_limit_down_mirrors(self):
        self.assertTrue(LC.is_limit_down("sz300750", -19.99))
        self.assertFalse(LC.is_limit_down("sz300750", -9.99))

    def test_pair_helper(self):
        self.assertTrue(LC.is_limit_up_pair("sh600000", 10.0, 11.0))
        self.assertFalse(LC.is_limit_up_pair("sh600000", 0, 11.0))

    def test_series_helper_matches_scalar(self):
        codes = ["sh600000", "sz300750", "bj430047"]
        rets = [9.99, 19.99, 19.99]
        self.assertEqual(LC.is_limit_up_series(codes, rets),
                         [LC.is_limit_up(c, r) for c, r in zip(codes, rets)])


class MainBoardTest(unittest.TestCase):
    def test_prefixes(self):
        for c in ("sh600000", "sh601318", "sh603466", "sh605580", "sz000001", "sz001979", "sz002594"):
            self.assertTrue(LC.is_main_board(c), c)
        for c in ("sz300750", "sh688111", "bj430047"):
            self.assertFalse(LC.is_main_board(c), c)


class SqlCaliberTest(unittest.TestCase):
    """SQL 生成器是第二条判定路径，必须与 Python 同结论。"""

    @classmethod
    def setUpClass(cls):
        try:
            import duckdb
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("duckdb 不可用")
        cls.con = duckdb.connect()
        cls.con.execute("CREATE TABLE t(sym VARCHAR, ret DOUBLE)")
        cls.rows = [
            ("sh600000", 0.0999), ("sh600000", 0.0986), ("sh600000", 0.0967),
            ("sz300750", 0.1999), ("sz300750", 0.0999), ("sh688111", 0.1999),
            ("bj430047", 0.2999), ("bj430047", 0.1999), ("sz301001", 0.1999),
        ]
        cls.con.executemany("INSERT INTO t VALUES (?,?)", cls.rows)

    def test_sql_matches_python(self):
        case = LC.sql_limit_up_case("ret", "sym")
        got = self.con.execute(f"SELECT sym, ret, {case} AS zt FROM t").fetchall()
        for sym, ret, zt in got:
            self.assertEqual(bool(zt), LC.is_limit_up(sym, ret * 100), f"{sym} {ret}")

    def test_pct_scale(self):
        case = LC.sql_limit_up_case("ret * 100", "sym", scale="pct")
        got = self.con.execute(f"SELECT sym, ret * 100 AS r, {case} AS zt FROM t").fetchall()
        for sym, ret_pct, zt in got:
            self.assertEqual(bool(zt), LC.is_limit_up(sym, ret_pct), f"{sym} {ret_pct}")


if __name__ == "__main__":
    unittest.main()
