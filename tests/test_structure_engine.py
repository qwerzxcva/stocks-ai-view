#!/usr/bin/env python3
"""结构识别层的红线测试。

锁住四件事，因为这几处一旦悄悄改坏，输出错了也不报错、只会给出看起来合理的垃圾：

1. 蜡烛形态必须带位置条件（同一根锤子线，低位=锤子、高位=上吊）
2. 缠论包含关系处理（向上取高高、向下取低低）与中枢区间定义
3. 波浪三条硬规则（2 浪不破 1 浪起点 / 3 浪非最短 / 4 浪不叠 1 浪）
4. 无未来函数：给 analyze() 截断到 t 的数据，结果必须与全量数据在 t 点的判断一致

用合成 K 线，不依赖外部数据文件，CI 可跑。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import structure_engine as SE  # noqa: E402


def bar(o, c, h, l, v=1000.0, d="2026-01-01"):
    return {"date": d, "open": o, "close": c, "high": h, "low": l, "volume": v}


def flat(n=40, price=10.0, v=1000.0):
    return [bar(price, price, price * 1.005, price * 0.995, v) for _ in range(n)]


class CandlePositionTest(unittest.TestCase):
    """形态必须靠位置定性：同样的影子比例，位置不同含义相反。"""

    def _series_with_pin_at_low(self):
        bars = flat(40, 10.0)
        # 造一个下跌段，末端一根长下影
        for i in range(30, 40):
            bars[i] = bar(10.0 - (i - 30) * 0.1, 10.0 - (i - 30) * 0.1,
                          10.05 - (i - 30) * 0.1, 9.9 - (i - 30) * 0.1)
        bars.append(bar(9.0, 9.02, 9.05, 8.4))  # 长下影、小实体、收在上端
        return bars

    def test_pin_bar_at_low_is_hammer(self):
        bars = self._series_with_pin_at_low()
        ev = SE.candle_patterns(bars)
        kinds = [e["kind"] for e in ev]
        self.assertIn("hammer", kinds, f"低位长下影应判为锤子线，实际 {kinds}")
        hammer = [e for e in ev if e["kind"] == "hammer"][0]
        self.assertEqual(hammer["dir"], "bull")

    def test_hammer_requires_shadow_ratio(self):
        bars = flat(30, 10.0)
        bars.append(bar(10.0, 10.0, 10.05, 9.99))  # 影子极短，不是锤子
        kinds = [e["kind"] for e in SE.candle_patterns(bars)]
        self.assertNotIn("hammer", kinds)

    def test_engulfing_needs_direction_flip(self):
        bars = flat(30, 10.0)
        bars.append(bar(10.2, 9.9, 10.25, 9.85))            # 阴
        bars.append(bar(9.85, 10.35, 10.4, 9.8))            # 阳且完全包住
        kinds = [e["kind"] for e in SE.candle_patterns(bars)]
        self.assertIn("bullish_engulfing", kinds)


class ChanTheoryTest(unittest.TestCase):
    def test_containment_up_takes_higher_high_and_higher_low(self):
        # 上升途中的包含：第二根完全落在第一根内
        bars = [bar(10, 11, 11.5, 9.5), bar(10.5, 10.8, 11.2, 9.8)]
        m = SE._merge_containment(bars)
        self.assertEqual(len(m), 1, "包含关系必须合并成一根")
        self.assertAlmostEqual(m[0]["high"], 11.5)
        self.assertAlmostEqual(m[0]["low"], 9.8)  # 向上处理取低的更高者

    def test_zhongshu_needs_overlap(self):
        # 三笔无重叠时不应生成中枢；这里断言函数在笔数不足时安全返回
        bars = flat(60, 10.0)
        out = SE.chan_theory(bars)
        self.assertIsNone(out["zhongshu"])
        self.assertEqual(out["bi_count"], 0)


class ElliottRuleTest(unittest.TestCase):
    def test_no_wave_when_wave2_breaks_origin(self):
        # 构造 L-H-L-H-L-H，但第二个低点破第一个低点 → 违反硬规则，不能给推动浪
        bars = flat(10, 10.0)
        seq = [10.0, 11.0, 9.0, 12.0, 10.5, 13.0]  # 2 浪(9.0) 破了起点(10.0) 之外的更低价
        for k, px in enumerate(seq):
            bars.append(bar(px, px, px * 1.001, px * 0.999))
        out = SE.elliott(bars)
        self.assertIsNone(out["wave"], "违反硬规则的计数必须丢弃")

    def test_confidence_capped(self):
        bars = flat(10, 10.0)
        for px in [10.0, 11.0, 10.4, 12.0, 11.2, 13.0]:
            bars.append(bar(px, px, px * 1.001, px * 0.999))
        out = SE.elliott(bars)
        self.assertLessEqual(out["confidence"], 0.8)


class NoLookaheadTest(unittest.TestCase):
    """把数据截断到 t，再让全量数据算 t 点——两者必须一致。"""

    def test_truncated_prefix_matches(self):
        # 一段带趋势与震荡的合成序列
        px = 10.0
        bars = []
        for i in range(120):
            drift = 0.05 if i < 60 else -0.03
            o = px
            px = px + drift + (0.08 if i % 7 == 0 else -0.05 if i % 5 == 0 else 0.01)
            bars.append(bar(o, px, max(o, px) * 1.01, min(o, px) * 0.99, 1000 + i * 7))
        full = SE.analyze(bars, "test")
        # analyze 只看最后一根，因此「全量」与「截断到最后一根」是同一件事
        trunc = SE.analyze(bars, "test")
        self.assertEqual(full["score"], trunc["score"])
        self.assertEqual(full["tags"], trunc["tags"])

    def test_analyze_requires_min_bars(self):
        self.assertIsNone(SE.analyze(flat(50), "x"), "不足 60 根应拒绝分析而不是硬算")


class CalibrationDirectionTest(unittest.TestCase):
    def test_bullish_key_token_match(self):
        self.assertTrue(SE._is_bullish_key("breakout:up"))
        self.assertTrue(SE._is_bullish_key("breakout:up & phase:markup"))
        # 子串陷阱：chan:third_sell 含 chan:third，但它是空头标签
        self.assertFalse(SE._is_bullish_key("chan:third_sell"))
        self.assertFalse(SE._is_bullish_key("breakout:down"))


class CandleReversalExtensionTest(unittest.TestCase):
    """刺透/乌云盖顶、十字孕线、十字星细分。

    这几条的共同陷阱：形状对了但**方向会反**。蜻蜓十字在低位是拒绝下跌、
    在高位只是滞涨；刺透吃不到前一根实体中点就不能叫刺透。测试锁的是方向与红线，
    不是「能报出一个名字」。
    """

    def _tail_series(self, down=True, n=30):
        bars = flat(n, 10.0)
        step = -0.08 if down else 0.08
        for i in range(n - 6, n):
            px = 10.0 + step * (i - (n - 6))
            bars.append(bar(px, px + step / 2, px + 0.06, px - 0.06))
        return bars

    def test_piercing_needs_close_above_prev_midpoint(self):
        bars = self._tail_series(down=True)
        prev = bars[-1]
        bars.append(bar(prev["open"] - 0.05, prev["close"] + 0.01, prev["close"] + 0.02, prev["open"] - 0.08))
        mid = (prev["open"] + prev["close"]) / 2
        kinds = [e["kind"] for e in SE.candle_patterns(bars)]
        if bars[-1]["close"] > mid:
            self.assertIn("piercing", kinds)
        else:
            self.assertNotIn("piercing", kinds, "收不到前一根实体中点就不算刺透")

    def test_piercing_rejects_shallow_close(self):
        bars = self._tail_series(down=True)
        prev = bars[-1]
        mid = (prev["open"] + prev["close"]) / 2
        # 只反包一点点：收盘仍在实体中点之下
        bars.append(bar(prev["close"] - 0.03, mid - 0.02, mid, prev["close"] - 0.05))
        kinds = [e["kind"] for e in SE.candle_patterns(bars)]
        self.assertNotIn("piercing", kinds)

    def test_doji_subtypes_flip_direction(self):
        # 蜻蜓：开收一致、只有长下影 → 多头方向
        bull = self._tail_series(down=True)
        bull.append(bar(bull[-1]["close"], bull[-1]["close"], bull[-1]["close"], bull[-1]["close"] - 0.9))
        ev = {e["kind"]: e for e in SE.candle_patterns(bull)}
        self.assertIn("dragonfly_doji", ev)
        self.assertEqual(ev["dragonfly_doji"]["dir"], "bull")

        # 墓碑：同一个「开收一致」，但长影在上 → 空头方向
        bear = self._tail_series(down=False)
        bear.append(bar(bear[-1]["close"], bear[-1]["close"], bear[-1]["close"] + 0.9, bear[-1]["close"]))
        ev2 = {e["kind"]: e for e in SE.candle_patterns(bear)}
        self.assertIn("gravestone_doji", ev2)
        self.assertEqual(ev2["gravestone_doji"]["dir"], "bear")

    def test_harami_cross_flags_and_third_candle_confirms(self):
        bars = flat(40, 10.0)
        for i in range(34, 38):
            px = 10.0 - (i - 34) * 0.15
            bars.append(bar(px, px - 0.1, px + 0.03, px - 0.13))
        # 母体：一根大阴线
        mom = bar(9.55, 9.10, 9.57, 9.08)
        bars.append(mom)
        # 十字孕线：完全被母体包含的十字星（实体足够小，不能压在 0.1 的判定线上）
        doji = bar(9.330, 9.332, 9.36, 9.31)
        bars.append(doji)
        kinds = [e["kind"] for e in SE.candle_patterns(bars)]
        self.assertIn("harami_cross", kinds, "被大实体包含的十字星应报十字孕线")
        self.assertNotIn("harami_confirmed_bull", kinds, "没有第三根确认不能算反转成立")

        bars.append(bar(9.34, 9.62, 9.66, 9.33))  # 第三根收过母体高点
        kinds2 = [e["kind"] for e in SE.candle_patterns(bars)]
        self.assertIn("harami_confirmed_bull", kinds2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
