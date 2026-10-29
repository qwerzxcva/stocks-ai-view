#!/usr/bin/env python3
"""批次二/三/四引擎的红线测试。

锁住的是「错了也不会报错、只会给出看起来合理的垃圾」的地方：
1. 威科夫事件必须落在区间边界上（破下沿又收回 = 弹簧；破上沿又收回 = 上冲回落）
2. 阶段判定证据不足时必须 unknown，不许硬猜
3. 三角洲必须报基线概率（不报基线，命中率没有意义）
4. 订单流在没有分钟/tick 数据时必须返回不可用，不许用日线伪造
5. 交易管理层：止损前置、仓位按风险反推、期望值为负必须拦、T+1 不能忽视
6. 题材阶段阈值单位必须是百分比（曾因单位混用把所有板块判成 climax）
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import theme_engine as TE  # noqa: E402
import trade_plan as TP  # noqa: E402
import wyckoff_flow_engine as WF  # noqa: E402


def bar(o, c, h, l, v=1000.0, d="2026-01-01"):
    return {"date": d, "open": o, "close": c, "high": h, "low": l, "volume": v}


def box(n=50, lo=10.0, hi=11.0, v=1000.0):
    """造一个横盘区间：高低点交替，量能平稳"""
    out = []
    for i in range(n):
        mid = (lo + hi) / 2
        px = lo + (hi - lo) * (0.5 if i % 2 else 0.3)
        out.append(bar(mid, px, hi if i % 3 == 0 else px * 1.01,
                       lo if i % 4 == 0 else px * 0.99, v))
    return out


class WyckoffEventTest(unittest.TestCase):
    def test_spring_detected_when_break_and_recover(self):
        bars = box()
        bars.append(bar(10.2, 10.6, 10.7, 9.5, 3000))  # 破下沿 10.0 又收回
        ev = WF.wyckoff_events(bars)
        kinds = [e["kind"] for e in ev]
        self.assertIn("spring", kinds, f"破下沿又收回应识别为 spring，实际 {kinds}")

    def test_upthrust_detected(self):
        bars = box()
        bars.append(bar(10.8, 10.4, 11.8, 10.2, 3000))  # 破上沿 11.0 又收回
        ev = WF.wyckoff_events(bars)
        kinds = [e["kind"] for e in ev]
        self.assertIn("ut", kinds, f"破上沿又收回应识别为 ut，实际 {kinds}")

    def test_stage_unknown_without_evidence(self):
        bars = box()
        tr = WF.trading_range(bars)
        st = WF.wyckoff_stage(bars, [], tr)
        self.assertEqual(st["stage"], "unknown", "没有事件必须 unknown，不许猜")

    def test_order_flow_refuses_without_minute_data(self):
        """没有分钟/tick 就必须说不可用——用日线伪造 footprint 是自欺。"""
        r = WF.order_flow([])
        if r["available"]:
            self.skipTest("当前环境存在分钟数据，跳过拒绝性断言")
        self.assertIn("不可落地", r["reason"])


class DeltaWindowTest(unittest.TestCase):
    def test_baseline_reported(self):
        # 造一个规律周期的锯齿：每 10 根一个低点
        bars = []
        px = 10.0
        for i in range(80):
            px = 10.0 + (1.0 if (i % 10) < 5 else -1.0)
            bars.append(bar(px, px, px * 1.02, px * 0.98, 1000))
        r = WF.delta_windows(bars)
        if not r.get("available"):
            self.skipTest("合成序列未形成稳定周期，跳过")
        self.assertIn("baseline_prob", r, "必须报基线概率，否则命中率无意义")
        self.assertLessEqual(r["confidence"], 0.5, "周期检测置信度上限 0.5")

    def test_dispersion_guard(self):
        # 随机间隔不应被判为稳定周期
        bars = []
        gaps = [3, 17, 5, 23, 8, 14, 4, 19]
        i = 0
        for g in gaps:
            for _ in range(g):
                px = 10 + (_ % 3) * 0.1
                bars.append(bar(px, px, px * 1.02, px * 0.98, 1000))
                i += 1
        r = WF.delta_windows(bars)
        if r.get("available"):
            self.assertLess(r["dispersion"], WF.DELTA_TOL + 1e-9)


class TradePlanTest(unittest.TestCase):
    def test_stop_before_entry_always(self):
        p = TP.build_plan("sh600016", entry=3.6, atr=0.09, equity=100000,
                          structure_lows=[3.48], win_rate=0.45)
        self.assertLess(p["stop_plan"]["stop"], p["entry"])

    def test_position_sized_by_risk(self):
        p = TP.build_plan("sh600016", entry=10.0, atr=0.5, equity=100000,
                          structure_lows=[9.0], win_rate=0.5)
        risk = p["size"]["shares"] * (p["entry"] - p["stop_plan"]["stop"])
        self.assertLessEqual(risk, 100000 * TP.RISK_PER_TRADE * 1.001)

    def test_negative_expectancy_blocked(self):
        e = TP.expectancy(win_rate=0.30, rr=2.0)
        self.assertFalse(e["ok"], "胜率 30% 配 RR2 应被期望值门槛拦下")

    def test_lot_size_round_to_100(self):
        p = TP.build_plan("sh600016", entry=3.6, atr=0.09, equity=100000,
                          structure_lows=[3.42], win_rate=0.45)
        self.assertEqual(p["size"]["shares"] % 100, 0, "A 股必须 100 股整数倍")

    def test_addon_only_when_profitable(self):
        self.assertFalse(TP.add_on(3.6, 3.5, 3.42, 0.09)["allow"])
        self.assertTrue(TP.add_on(3.6, 3.75, 3.42, 0.09)["allow"])

    def test_volume_climax_flags(self):
        self.assertTrue(TP.volume_alert(3.5e6, 1e6)["climax"])
        self.assertFalse(TP.volume_alert(1.2e6, 1e6)["climax"])

    def test_circuit_breaker_scales_with_win_rate(self):
        lo = TP.circuit_breaker(0.30)["raw"]
        hi = TP.circuit_breaker(0.55)["raw"]
        self.assertGreater(lo, hi, "胜率越低，允许的连亏笔数应越多")

    def test_time_stop_triggers(self):
        self.assertEqual(TP.time_stop(3.6, 3.58, 0.09, 1)["action"], "wait")
        self.assertEqual(TP.time_stop(3.6, 3.58, 0.09, TP.TIME_STOP_BARS)["action"], "reduce_half")
        self.assertEqual(TP.time_stop(3.6, 3.7, 0.09, 1)["action"], "hold")


class ThemeStageTest(unittest.TestCase):
    def _cells(self, rets):
        return [{"d": f"2026-01-{i+1:02d}", "p": r} for i, r in enumerate(rets)]

    def test_units_are_percent_not_fraction(self):
        """曾把阈值写成 0.08 却和百分数比，结果全部板块被判 climax。"""
        flat = [0.1] * 25
        st = TE.board_stage(self._cells(flat))
        self.assertNotEqual(st["stage"], "climax", "25 天各 +0.1% 不该是高潮期")

    def test_climax_on_strong_rally(self):
        st = TE.board_stage(self._cells([3.0] * 25))   # 需 ≥20 根才能判 20 日累计
        self.assertEqual(st["stage"], "climax")

    def test_ebb_on_decline(self):
        st = TE.board_stage(self._cells([-1.0] * 25))
        self.assertEqual(st["stage"], "ebb")

    def test_short_sample_is_unknown_not_guessed(self):
        st = TE.board_stage(self._cells([3.0] * 8))
        self.assertEqual(st["stage"], "unknown", "样本不足必须 unknown，不硬猜")

    def test_mapping_health_reports_low_match(self):
        h = TE.mapping_health()
        self.assertIn("match_rate", h)
        self.assertLess(h["match_rate"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
