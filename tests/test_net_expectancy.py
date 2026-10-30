#!/usr/bin/env python3
"""净收益口径测试：校准表里的 net_* 字段必须真正参与决策。

锁住的退化点：表里算了扣费后收益、字段也写进了 JSON，但下游仍按毛收益打分——
分数看起来完全合理，却系统性高估了真实优势。本项目里这是最容易静默复发的
一类 bug（上一轮就出现过「net 字段没人读」）。

四条红线：
1. 有净字段时，t 与期望收益都必须取净口径，毛收益只作为 gross_edge_5d_pct 留档。
2. 旧表没有净字段时必须退回毛收益（不能用 None 把整层打成无信号）。
3. 毛 t 过检、净 t 不过检的标签**不得计分**——宁可不出分，也不拿扣费后不显著的
   优势抬分。
4. 融合打分的层权重必须用净 t，不是毛 t。

全部用自造表注入缓存，不依赖 data/ 产物，CI 可跑。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

import signal_hub as SH  # noqa: E402


def _seed(tables, signals):
    """把自造校准表与信号注入 signal_hub 的懒加载缓存。"""
    SH._LAYER_CACHE.clear()
    for name, table in tables.items():
        SH._LAYER_CACHE[name] = {"table": table, "meta": {"horizon_days": 5}}
    for name, items in signals.items():
        SH._LAYER_CACHE["__sig__" + name] = {
            SH.bare_of(c): v for c, v in items.items()
        }


# 毛收益 2.00 / t=5.00，扣费后 1.70 / t=3.10
ROW_BOTH = {"n": 400, "avg_fwd": 2.00, "t": 5.00,
            "net_avg_fwd_15bp": 1.70, "net_t_15bp": 3.10,
            "win_rate": 0.6, "edge": "trend"}
# 毛够、净不够：2.00/t=5.00 但净 1.70/t=1.20
ROW_NET_FAILS = {"n": 400, "avg_fwd": 2.00, "t": 5.00,
                 "net_avg_fwd_15bp": 1.70, "net_t_15bp": 1.20,
                 "win_rate": 0.6, "edge": "trend"}
# 旧表：完全没有净字段
ROW_LEGACY = {"n": 400, "avg_fwd": 2.00, "t": 5.00,
              "win_rate": 0.6, "edge": "trend"}


def _signals_one(code, tags):
    return {"limit_signals.json": {code: {"tags": tags}}}


class NetExpectancyTest(unittest.TestCase):

    def setUp(self):
        SH._LAYER_CACHE.clear()

    def tearDown(self):
        SH._LAYER_CACHE.clear()

    def test_net_fields_win_over_gross(self):
        """有净字段：t / 期望取净口径，毛收益单独留档。"""
        _seed({"limit_calibration.json": {"limit:first_board": ROW_BOTH}},
              _signals_one("600000", ["limit:first_board"]))
        e = SH.layer_expectancy("limit", "600000")
        self.assertIsNotNone(e)
        self.assertAlmostEqual(e["t"], 3.10, places=6)
        self.assertAlmostEqual(e["edge_5d_pct"], 1.70, places=6)
        self.assertAlmostEqual(e["gross_edge_5d_pct"], 2.00, places=6)

    def test_falls_back_to_gross_without_net_fields(self):
        """旧表没有净字段：退回毛收益，不能变成无信号。"""
        _seed({"limit_calibration.json": {"limit:first_board": ROW_LEGACY}},
              _signals_one("600000", ["limit:first_board"]))
        e = SH.layer_expectancy("limit", "600000")
        self.assertIsNotNone(e, "缺净字段不得导致整层无信号")
        self.assertAlmostEqual(e["t"], 5.00, places=6)
        self.assertAlmostEqual(e["edge_5d_pct"], 2.00, places=6)

    def test_gross_passes_but_net_fails_gate(self):
        """毛 t=5.00 过检、净 t=1.20 不过检 → 不给分。"""
        _seed({"limit_calibration.json": {"limit:first_board": ROW_NET_FAILS}},
              _signals_one("600000", ["limit:first_board"]))
        self.assertIsNone(SH.layer_expectancy("limit", "600000"),
                          "扣费后不显著的标签不应被当成有效期望")

    def test_structure_expectancy_also_uses_net(self):
        """结构层字段名不同（net_*_5d_15bp），同样要取净口径。"""
        SH._CALIB["loaded"] = False
        SH._LAYER_CACHE.clear()
        # 结构层不走 _LAYER_CACHE：structure_expectancy 自己 load_json 到 _CALIB
        SH._CALIB["table"] = {"phase:accumulation": {
            "n": 500, "avg_fwd_5d": 0.90, "t": 4.00,
            "net_avg_fwd_5d_15bp": 0.75, "net_t_5d_15bp": 2.60,
            "win_rate": 0.55, "edge": "trend"}}
        SH._CALIB["meta"] = {"horizon_days": 5}
        SH._CALIB["loaded"] = True
        # 结构层走独立的 _STRUCT 缓存（不经 _signals）
        SH._STRUCT["by_code"] = {SH.bare_of("600000"): {"tags": ["phase:accumulation"]}}
        SH._STRUCT["loaded"] = True
        try:
            e = SH.structure_expectancy("600000")
            self.assertIsNotNone(e)
            self.assertAlmostEqual(e["t"], 2.60, places=6)
            self.assertAlmostEqual(e["edge_5d_pct"], 0.75, places=6)
            self.assertAlmostEqual(e["gross_edge_5d_pct"], 0.90, places=6)
        finally:
            SH._CALIB["loaded"] = False
            SH._CALIB["table"] = {}
            SH._CALIB["meta"] = {}
            SH._STRUCT["loaded"] = False
            SH._STRUCT["by_code"] = {}
            SH._LAYER_CACHE.clear()

    def test_confluence_weights_use_net_t(self):
        """层权重取净 t：构造毛/净 t 相反的两层，分数必须按净 t 归。"""
        _seed({
            "reversal_calibration.json": {"mtr:break_up": {
                "n": 400, "avg_fwd": 1.00, "t": 4.00,
                "net_avg_fwd_15bp": 1.00, "net_t_15bp": 2.00,
                "win_rate": 0.6, "edge": "trend"}},
            "orderflow_calibration.json": {"flow:delta_down": {
                "n": 400, "avg_fwd": 2.00, "t": 2.00,
                "net_avg_fwd_15bp": 2.00, "net_t_15bp": 4.00,
                "win_rate": 0.6, "edge": "trend"}},
            "limit_calibration.json": {},
        }, {
            "reversal_signals.json": {"600000": {"tags": ["mtr:break_up"]}},
            "orderflow_signals.json": {"600000": {"tags": ["flow:delta_down"]}},
            "limit_signals.json": {"600000": {"tags": []}},
        })
        s = SH.confluence_score("600000")
        self.assertIsNotNone(s)
        # 净权重：w1=2/4=0.5, w2=4/4=1.0 → exp=(1.0*0.5+2.0*1.0)/1.5=1.6667
        # 毛权重会得 (1.0*1.0+2.0*0.5)/1.5=1.3333，两者分数不同，可区分
        self.assertAlmostEqual(s["expected_5d_pct"], 1.67, places=2)
        self.assertAlmostEqual(s["score"], 77.8, places=1)
        dims = {d["layer"]: d for d in s["dimensions"]}
        self.assertAlmostEqual(dims["orderflow"]["weight"], 1.0, places=3)
        self.assertAlmostEqual(dims["reversal"]["weight"], 0.5, places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
