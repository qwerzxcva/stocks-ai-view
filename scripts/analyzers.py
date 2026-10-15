#!/usr/bin/env python3
"""
analyzers.py — 回测指标分析器 (Analyzer 插件框架)

借鉴来源: cloudQuant/backtrader (GPL-3.0) + backtrader 官方文档
  https://github.com/cloudQuant/backtrader
  https://www.backtrader.com/docu/analyzers-reference
  GPL-3.0 不允许复制代码, 因此这里**只借鉴其 Analyzer 架构设计**
  (统一接口 + 可插拔 + notify/stop 两阶段), 数学式按公开定义独立实现。

## 借鉴的架构 (不是代码)

backtrader 的 Analyzer 是一个抽象基类, 所有指标分析器继承它:
    class Analyzer:
        def start(self): ...          # 回测开始前初始化
        def next(self): ...           # 每根K线调用 (可选)
        def stop(self): ...           # 回测结束后定稿 (可选)
        def get_analysis(self): ...   # 返回 dict 给外部

两种使用模式 (官方文档明确):
  1. **执行中累积** — 在 notify_xxx / next 里边跑边收数据, next 时算当前值
     例: TradeAnalyzer 用 notify_trade 逐笔累积
  2. **单次汇总** — 只在 stop 时一次性算完
     例: SharpeRatio 用 TimeReturn 的年报数据一次性算

插拔方式: cerebro.addanalyzer(SharpeRatio, _name='sharpe'),
跑完 strat.analyzers.sharpe.get_analysis() 取出。指标之间可互相依赖
(SharpeRatio 内部用 AnnualReturn 的输出), 从分析器对用户透明。

## 为什么值得我们借鉴 (我们的现状)

我们有 kline_sim (K线模拟回测) 和 winrate_matrix (胜率矩阵), 但指标散落在
各处、口径不一: kline_sim 只给收益率, winrate_matrix 只给胜率,
没有统一的「策略健康度」视图。缺的关键指标:
  - Sharpe / Sortino / Calmar (风险调整收益)
  - 最大回撤 + 回撤时长
  - 盈亏比 / 连续盈亏 / 平均持仓时长
  - 与随机基准的对比 (我们的 factor_lab 已证明这是必须的)

## 本实现

不引入 backtrader (GPL 传染 + 依赖重), 用同一架构思想实现轻量版:
  - Analyzer 基类 + create_analysis/stop/get_analysis 三阶段
  - 内置 8 个分析器 (见下)
  - 输入是「交易列表」而非事件流 (我们的场景是一次性回测, 不是逐 bar 推进)

用法:
    from analyzers import BacktestResult, SharpeRatio, DrawDown, TradeAnalyzer
    res = BacktestResult(trades)                 # trades: [{ret_pct, entry_date, exit_date, ...}]
    for A in (SharpeRatio, Sortino, Calmar, DrawDown, TradeAnalyzer, SQN):
        res.add(A)
    print(res.get('sharpe'))                     # → {'sharperatio': 1.83}
"""
import math
from collections import OrderedDict


# ─── 基类 ──────────────────────────────────────────────────────────
class Analyzer:
    """分析器基类。子类实现 create_analysis / stop, 或直接覆写 analyze。"""

    def __init__(self, result=None):
        self.result = result
        self.rets = OrderedDict()

    def create_analysis(self):
        """初始化分析结构 (对应 backtrader 的 create_analysis)。"""
        self.rets = OrderedDict()

    def analyze(self, trades):
        """一次性计算 (对应 backtrader 的 stop)。子类覆写。"""
        raise NotImplementedError

    def get_analysis(self):
        return self.rets


class BacktestResult:
    """回测结果容器: 持有一组交易, 可挂多个分析器 (对应 cerebro)。"""

    def __init__(self, trades=None, risk_free=0.0, periods_per_year=244):
        # trades: [{ret_pct, entry_date, exit_date, hold_bars, side, ...}]
        self.trades = [t for t in (trades or []) if t.get("ret_pct") is not None]
        self.risk_free = risk_free
        self.periods_per_year = periods_per_year
        self._analyzers = {}

    def add(self, analyzer_cls, name=None):
        a = analyzer_cls(self)
        nm = name or getattr(analyzer_cls, "name", analyzer_cls.__name__.lower())
        a.create_analysis()
        a.analyze(self.trades)
        self._analyzers[nm] = a
        return a

    def get(self, name):
        a = self._analyzers.get(name)
        return a.get_analysis() if a else None

    def all(self):
        return {k: v.get_analysis() for k, v in self._analyzers.items()}


# ─── 收益类 ────────────────────────────────────────────────────────
class TimeReturn(Analyzer):
    """收益率序列 (最基础, 其他收益类分析器依赖它)。"""
    name = "timereturn"

    def analyze(self, trades):
        self.rets = OrderedDict()
        for i, t in enumerate(trades):
            self.rets[t.get("exit_date") or f"t{i}"] = t["ret_pct"] / 100.0


class AnnualReturn(Analyzer):
    """年度收益 (按 exit_date 的年份归组, 复利累计)。"""
    name = "annualreturn"

    def analyze(self, trades):
        by_year = {}
        for t in trades:
            d = str(t.get("exit_date") or "")
            y = d[:4] or "unknown"
            by_year.setdefault(y, []).append(1 + t["ret_pct"] / 100.0)
        self.rets = OrderedDict()
        for y in sorted(by_year):
            v = 1.0
            for r in by_year[y]:
                v *= r
            self.rets[y] = (v - 1) * 100


class SharpeRatio(Analyzer):
    """夏普比率 = (平均收益 - 无风险) / 收益标准差 × sqrt(年化周期数)。

    原版用 AnnualReturn; 这里用逐笔收益 (更细粒度), 并注明口径差异。
    """
    name = "sharpe"

    def analyze(self, trades):
        if len(trades) < 2:
            self.rets = {"sharperatio": None, "note": "样本不足 (n<2)"}
            return
        rets = [t["ret_pct"] / 100.0 for t in trades]
        n = len(rets)
        mean = sum(rets) / n
        sd = (sum((x - mean) ** 2 for x in rets) / (n - 1)) ** 0.5
        rf = self.result.risk_free / self.result.periods_per_year
        if sd == 0:
            self.rets = {"sharperatio": None, "note": "收益无波动, 无法计算"}
            return
        self.rets = {
            "sharperatio": round((mean - rf) / sd * math.sqrt(self.result.periods_per_year), 3),
            "mean_ret_pct": round(mean * 100, 3),
            "std_pct": round(sd * 100, 3),
            "n": n,
            "annualized": "按每笔为一个周期, sqrt(%d) 年化"
                          % self.result.periods_per_year,
        }


class SortinoRatio(Analyzer):
    """索提诺比率: 只用下行偏差 (惩罚下跌, 不惩罚上涨)。"""
    name = "sortino"

    def analyze(self, trades):
        if len(trades) < 2:
            self.rets = {"sortino": None, "note": "样本不足"}
            return
        rets = [t["ret_pct"] / 100.0 for t in trades]
        n = len(rets)
        mean = sum(rets) / n
        rf = self.result.risk_free / self.result.periods_per_year
        downside = [min(0.0, x - rf) for x in rets]
        dd = (sum(x * x for x in downside) / n) ** 0.5
        if dd == 0:
            self.rets = {"sortino": None, "note": "无下行偏差"}
            return
        self.rets = {
            "sortino": round((mean - rf) / dd * math.sqrt(self.result.periods_per_year), 3),
            "downside_std_pct": round(dd * 100, 3),
        }


class CalmarRatio(Analyzer):
    """卡玛比率 = 年化收益 / 最大回撤。"""
    name = "calmar"

    def analyze(self, trades):
        if not trades:
            self.rets = {"calmar": None}
            return
        # 逐笔复利净值曲线
        eq = [1.0]
        for t in trades:
            eq.append(eq[-1] * (1 + t["ret_pct"] / 100.0))
        peak = eq[0]
        maxdd = 0.0
        for v in eq:
            peak = max(peak, v)
            if peak > 0:
                maxdd = max(maxdd, (peak - v) / peak)
        ar = self._analyzers_annual(trades)
        if maxdd == 0:
            self.rets = {"calmar": None, "note": "无回撤", "annual_return_pct": ar}
            return
        self.rets = {"calmar": round(ar / (maxdd * 100), 3),
                     "annual_return_pct": round(ar, 2),
                     "max_drawdown_pct": round(maxdd * 100, 2)}

    @staticmethod
    def _analyzers_annual(trades):
        by_year = {}
        for t in trades:
            y = str(t.get("exit_date") or "unknown")[:4]
            by_year.setdefault(y, []).append(1 + t["ret_pct"] / 100.0)
        if not by_year:
            return 0.0
        # 简化年化: 总收益的几何平均 (不足一年按比例)
        v = 1.0
        for t in trades:
            v *= (1 + t["ret_pct"] / 100.0)
        years = max(1, len(by_year))
        return (v ** (1 / years) - 1) * 100


class DrawDown(Analyzer):
    """回撤统计: 当前/最大回撤 (百分比与金额), 回撤时长与最长回撤时长。"""
    name = "drawdown"

    def analyze(self, trades):
        if not trades:
            self.rets = {"max": {"drawdown": 0.0}}
            return
        eq = [1.0]
        for t in trades:
            eq.append(eq[-1] * (1 + t["ret_pct"] / 100.0))
        peak = eq[0]
        peak_i = 0
        max_dd = 0.0
        max_len = 0
        cur_len = 0
        cur_dd = 0.0
        for i, v in enumerate(eq):
            if v >= peak:
                peak = v
                peak_i = i
                cur_len = 0
                cur_dd = 0.0
            else:
                cur_len = i - peak_i
                cur_dd = (peak - v) / peak if peak > 0 else 0.0
                if cur_dd >= max_dd:
                    max_dd = cur_dd
                    max_len = cur_len
        self.rets = {
            "drawdown": round(cur_dd, 4),
            "len": cur_len,
            "max": {
                "drawdown": round(max_dd, 4),
                "len": max_len,
            },
            "final_equity": round(eq[-1], 4),
            "n_trades": len(trades),
        }


class TradeAnalyzer(Analyzer):
    """交易统计: 总/盈/亏笔数, 连续盈亏, 盈亏额, 平均与最长/最短持仓。"""
    name = "trades"

    def analyze(self, trades):
        n = len(trades)
        if n == 0:
            self.rets = OrderedDict(total=OrderedDict(total=0))
            return
        won = [t for t in trades if t["ret_pct"] > 0]
        lost = [t for t in trades if t["ret_pct"] <= 0]
        won_pnl = [t["ret_pct"] for t in won]
        lost_pnl = [t["ret_pct"] for t in lost]

        # 连续盈亏
        max_win_streak = max_lose_streak = 0
        cur_win = cur_lose = 0
        for t in trades:
            if t["ret_pct"] > 0:
                cur_win += 1
                cur_lose = 0
            else:
                cur_lose += 1
                cur_win = 0
            max_win_streak = max(max_win_streak, cur_win)
            max_lose_streak = max(max_lose_streak, cur_lose)

        holds = [t.get("hold_bars") for t in trades if t.get("hold_bars") is not None]
        avg_win = sum(won_pnl) / len(won_pnl) if won_pnl else 0.0
        avg_loss = sum(lost_pnl) / len(lost_pnl) if lost_pnl else 0.0
        pl_ratio = (abs(avg_win / avg_loss) if avg_loss else None)

        self.rets = OrderedDict([
            ("total", OrderedDict([("total", n), ("open", 0), ("closed", n)])),
            ("streak", OrderedDict([("won", OrderedDict([("current", cur_win),
                                                          ("longest", max_win_streak)])),
                                    ("lost", OrderedDict([("current", cur_lose),
                                                          ("longest", max_lose_streak)]))])),
            ("won", OrderedDict([("total", len(won)),
                                 ("pnl", OrderedDict([("total", round(sum(won_pnl), 2)),
                                                      ("average", round(avg_win, 3)),
                                                      ("max", round(max(won_pnl), 3) if won_pnl else 0.0)]))])),
            ("lost", OrderedDict([("total", len(lost)),
                                  ("pnl", OrderedDict([("total", round(sum(lost_pnl), 2)),
                                                       ("average", round(avg_loss, 3)),
                                                       ("max", round(min(lost_pnl), 3) if lost_pnl else 0.0)]))])),
            ("pnl", OrderedDict([("net", OrderedDict([("total", round(sum(t["ret_pct"] for t in trades), 2))]))])),
            ("win_rate", round(len(won) / n * 100, 2)),
            ("profit_loss_ratio", round(pl_ratio, 3) if pl_ratio else None),
            ("length", OrderedDict([
                ("total", sum(holds) if holds else 0),
                ("average", round(sum(holds) / len(holds), 2) if holds else None),
                ("max", max(holds) if holds else None),
                ("min", min(holds) if holds else None),
            ])),
        ])


class SQN(Analyzer):
    """System Quality Number = sqrt(n) × (平均收益/标准差) × 100。

    Van Tharp 的 SQN, 衡量系统稳健性: <1.6 弱, 1.6-2.0 一般,
    2.0-2.5 好, 2.5-3.0 优, >3.0 极优。
    """
    name = "sqn"

    def analyze(self, trades):
        if len(trades) < 2:
            self.rets = {"sqn": None, "note": "样本不足"}
            return
        rets = [t["ret_pct"] for t in trades]
        n = len(rets)
        mean = sum(rets) / n
        sd = (sum((x - mean) ** 2 for x in rets) / (n - 1)) ** 0.5
        if sd == 0:
            self.rets = {"sqn": None, "note": "收益无波动"}
            return
        sqn = math.sqrt(n) * mean / sd
        grade = ("极优" if sqn > 3 else "优" if sqn > 2.5 else "好" if sqn > 2
                 else "一般" if sqn > 1.6 else "弱")
        self.rets = {"sqn": round(sqn, 3), "grade": grade, "n": n}


# 便捷: 一次挂全部
DEFAULT_ANALYZERS = [SharpeRatio, SortinoRatio, CalmarRatio, DrawDown,
                     TradeAnalyzer, SQN]


def run_all(trades, risk_free=0.0):
    """输入交易列表 → 全部分析器结果 dict。"""
    res = BacktestResult(trades, risk_free=risk_free)
    for A in DEFAULT_ANALYZERS:
        res.add(A)
    res.add(TimeReturn)
    res.add(AnnualReturn)
    return res.all()


if __name__ == "__main__":
    # 自验: 构造两组交易 (一组正期望, 一组负期望)
    import json

    good = [{"ret_pct": 2.0, "exit_date": "2026-01-0%d" % (i + 1), "hold_bars": 3}
            for i in range(5)] + \
           [{"ret_pct": -1.0, "exit_date": "2026-01-1%d" % (i + 1), "hold_bars": 2}
            for i in range(5)]
    bad = [{"ret_pct": -2.0, "exit_date": "2026-02-0%d" % (i + 1), "hold_bars": 3}
           for i in range(5)] + \
          [{"ret_pct": 1.0, "exit_date": "2026-02-1%d" % (i + 1), "hold_bars": 2}
           for i in range(5)]

    for label, trades in (("good(正期望)", good), ("bad(负期望)", bad)):
        r = run_all(trades)
        print(f"=== {label} ===")
        print("  sharpe   :", r["sharpe"])
        print("  sortino  :", r["sortino"])
        print("  calmar   :", r["calmar"])
        print("  drawdown :", r["drawdown"])
        print("  win_rate :", r["trades"]["win_rate"], "%")
        print("  pl_ratio :", r["trades"]["profit_loss_ratio"])
        print("  sqn      :", r["sqn"])
        print("  annual   :", dict(r["annualreturn"]))
