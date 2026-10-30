#!/usr/bin/env python3
"""三本新书落地的红线测试（反转 MTR / 涨停结构 / 订单流近似）。

锁住的是「改坏了也不会报错、只会输出看起来合理的垃圾」的那几处：

1. 涨停阈值必须按板块分开（主板 10 / 创业与科创 20 / 北交 30）——统一按 10% 会把
   创业板 15% 的普通上涨误判成涨停，进而把整条连板高度算错。
2. 连板高度必须累进、断板必须归零——高度算错，龙头识别就是错的。
3. 强封只用「收盘 = 最高」近似，不能反推封单量（没有 L2）。
4. MTR 方向必须互斥：同一只票不能同时给出「顶确认」与「底确认」，
   否则下游拿到的两个信号互相抵消，还看不出是哪里错了。
5. clv 边界：收在最高 = +1，收在最低 = -1，无振幅时 = 0（不能除零）。
6. 无未来函数：截断到 t 的数据得出的结论，必须与全量数据在 t 点的结论一致。

全部用合成 K 线，不依赖 data/ 产物，CI 可跑。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import reversal_engine as RE  # noqa: E402
import limit_structure as LS  # noqa: E402
import orderflow_proxy as OF  # noqa: E402


def bar(o, c, h, l, v=1000.0, d="2026-01-01"):
    return {"date": d, "open": o, "close": c, "high": h, "low": l, "volume": v}


def series(prices, vols=None):
    """按收盘价序列造 K 线：high/low 取相邻极值，保证形态可算。"""
    out = []
    for i, p in enumerate(prices):
        prev = prices[i - 1] if i else p
        hi = max(p, prev) * 1.002
        lo = min(p, prev) * 0.998
        v = vols[i] if vols else 1000.0
        out.append(bar(prev, p, hi, lo, v, d=f"2026-01-{i+1:02d}"))
    return out


class LimitThresholdTest(unittest.TestCase):
    """涨停阈值按板块分档——这是一切连板计算的地基。"""

    def test_main_board_10pct(self):
        self.assertAlmostEqual(LS.limit_pct("sh600000"), 9.8)
        self.assertAlmostEqual(LS.limit_pct("sz000001"), 9.8)

    def test_chinext_and_star_20pct(self):
        self.assertAlmostEqual(LS.limit_pct("sz300750"), 19.8)
        self.assertAlmostEqual(LS.limit_pct("sz301001"), 19.8)
        self.assertAlmostEqual(LS.limit_pct("sh688981"), 19.8)

    def test_bse_30pct(self):
        self.assertAlmostEqual(LS.limit_pct("bj830799"), 29.8)


class LimitStreakTest(unittest.TestCase):
    """连板高度累进 + 断板归零 + 强封近似。"""

    def _two_boards_then_break(self):
        # 10 元 → 11 → 12.1（两个板）→ 12.3（断板，+1.65%）
        prices = [10.0] * 60 + [10.0, 11.0, 12.1, 12.3] + [12.3] * 10
        bars = series(prices)
        # 让涨停日收盘 = 最高，模拟封住
        for b, target in zip(bars[-13:-9], (10.0, 11.0, 12.1, 12.3)):
            b["close"] = target
            b["high"] = target
        return bars

    def test_streak_and_break(self):
        bars = self._two_boards_then_break()
        ev = LS.limit_events(bars, "sh600000")
        limits = [e for e in ev if "boards" in e and e["boards"]]
        self.assertGreaterEqual(len(limits), 2, "应识别出至少两个涨停日")
        boards = [e["boards"] for e in limits]
        self.assertEqual(boards[:2], [1, 2], f"连板高度应累进为 1,2，实际 {boards}")
        broken = [e for e in ev if e.get("broken")]
        self.assertTrue(broken, "断板必须被识别")
        self.assertEqual(broken[0]["prev_streak"], 2, "断板应记录断掉前的连板高度")

    def test_strong_seal_is_close_equals_high(self):
        bars = series([10.0] * 60 + [10.0, 11.0])
        bars[-1]["close"] = 11.0
        bars[-1]["high"] = 11.0
        ev = LS.limit_events(bars, "sh600000")
        self.assertTrue(ev and ev[-1].get("strong_seal"), "收盘=最高应判为强封")

    def test_known_limits_declared(self):
        """不可用项必须显式声明，别让上游以为封单算过了。"""
        kl = LS.known_limits()
        for k in ("seal_amount", "first_seal_time", "lhb_seats"):
            self.assertIn(k, kl)


class CLVTest(unittest.TestCase):
    """clv 是订单流代理的核心量，边界必须精确。"""

    def test_edges(self):
        self.assertAlmostEqual(OF.clv(bar(10, 11, 11, 10)), 1.0)    # 收在最高 = +1
        self.assertAlmostEqual(OF.clv(bar(11, 10, 11, 10)), -1.0)   # 收在最低 = -1
        self.assertAlmostEqual(OF.clv(bar(10, 10, 11, 9)), 0.0)     # 收在中点 = 0

    def test_no_range(self):
        self.assertAlmostEqual(OF.clv(bar(10, 10, 10, 10)), 0.0)


class ReversalDisciplineTest(unittest.TestCase):
    """MTR 的方向互斥与趋势前提。"""

    def _uptrend_then_break(self):
        # 60 根横盘 → 20 根上行（创新高）→ 急速跌破 → 回抽 → 再跌
        up = [10.0 * (1.01 ** i) for i in range(20)]
        down = [up[-1] * (1 - 0.012 * i) for i in range(1, 14)]
        return series([10.0] * 60 + up + down)

    def test_direction_mutually_exclusive(self):
        for bars in (series([10.0] * 90), self._uptrend_then_break()):
            r = RE.analyze(bars, "sh600000")
            if not r:
                continue
            tags = set(r["tags"])
            self.assertFalse(
                "mtr:top_confirmed" in tags and "mtr:bottom_confirmed" in tags,
                f"顶底确认不能同时出现：{sorted(tags)}",
            )

    def test_flat_market_no_confirmation(self):
        """纯横盘不该产出反转确认——没有趋势就没有反转。"""
        r = RE.analyze(series([10.0] * 120), "sh600000")
        self.assertIsNotNone(r)
        self.assertNotIn("mtr:top_confirmed", r["tags"])
        self.assertNotIn("mtr:bottom_confirmed", r["tags"])

    def test_deterministic_and_no_side_effect(self):
        """analyze 不得修改输入 K 线，同一输入两次结果一致（无全局状态 / 无未来函数）。

        真正验证「不看未来」靠代码审查（只看 bars 内部的索引），
        可测的那部分是：不越界、不改入参、不依赖调用次序。
        """
        bars = self._uptrend_then_break()
        snapshot = [dict(b) for b in bars]
        r1 = RE.analyze(bars, "sh600000")
        r2 = RE.analyze(bars, "sh600000")
        self.assertEqual(r1["tags"], r2["tags"], "同一输入必须给出同一结论")
        for a, b in zip(snapshot, bars):
            self.assertEqual(a, b, "analyze 不应修改输入 K 线")


class OrderflowDisciplineTest(unittest.TestCase):
    """订单流层必须自曝局限，不能假装是真 Delta。"""

    def test_known_limits(self):
        kl = OF.known_limits()
        for k in ("true_delta", "cvd", "poc_value_area"):
            self.assertIn(k, kl)

    def test_analyze_shape(self):
        bars = series([10.0 + 0.05 * (i % 7) for i in range(100)],
                      vols=[1000.0 + 100 * (i % 5) for i in range(100)])
        r = OF.analyze(bars, "sh600000")
        self.assertIsNotNone(r)
        self.assertIn("clv", r["detail"])
        self.assertIn("delta_norm_5d", r["detail"])
        self.assertIn("note", r)


if __name__ == "__main__":
    unittest.main(verbosity=2)
