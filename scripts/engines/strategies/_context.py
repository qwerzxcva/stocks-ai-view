#!/usr/bin/env python3
"""_context.py — 策略共享数据源上下文（独立实现，策略规则来自用户提供）。

统一加载 stocks-ai-view 本地引擎/实证数据源成 context dict（只读），供策略 evaluate 使用：
  - data/engines/market_anomaly_engine.json → 大盘环境（涨跌/异动事件计数，含跌停潮 dt_flood）
  - data/engines/regime_router.json        → 大盘路由（regime/phase/regime_score）
  - data/engine_fusion.json / engine_v5.json → 大盘评分（E 值 / top 榜）
  - data/ban_prob.json                     → 连板实证概率（打板/回踩参考）
  - data/limitup_ml_results.json           → 涨停 ML 结果
  - stocks-data-live 落地文件：moneyflow/latest.txt（资金流，含换手率/主力净流入）、
    research/lhb_*.json（龙虎榜）、pool/*.dtpool（跌停池，精确跌停家数）

降级原则：任一源加载失败/文件缺失时，context 对应键为 None 并记入
context['_degraded_sources']，绝不抛异常、绝不伪造数据。stocks-data-live 缺失时
对应键为 None 并在 degraded 标注「stocks-data-live 不可用」。

无前视 asof 模式（load_context(asof='YYYY-MM-DD')，前视偏差修复版新增）：
  - market_score：不再读 engine_fusion 当前快照 E，改为按 asof 当日从 stocks-data-kline
    上证综指日线（data/index/history/sh000001.txt）重算（market_score_asof，0-100）。
  - dt_count / dt_flood_count：不再读当前跌停池快照，改为按 asof 当日从
    stocks-ai-view data/bulk 主分片全市场扫描统计主板（sh60*/sz00*）跌幅 ≤ -9.8% 家数
    （前收=同 code 上一交易日行；分片内 code 块日期升序，无未来函数）。
  - 其余源（regime_router/ban_prob/limitup_ml/moneyflow/龙虎榜 等）均无历史序列、
    只有当前快照：asof 模式下统一置 None 并记 degraded「无历史序列，asof 模式降级
    跳过（防前视）」——宁可降级也不拿当前快照冒充历史。
"""
from __future__ import annotations

import glob
import json
import os
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "..", "data"))
DEFAULT_LIVE_DIR = os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "..", "stocks-data-live"))
DEFAULT_INDEX_HIST = os.path.normpath(os.path.join(
    HERE, "..", "..", "..", "..", "stocks-data-kline", "data", "index", "history",
    "sh000001.txt"))
# asof 跌停统计扫描的分片（主板股票落在 00-09 / 60 主分片，与 _loader 口径一致）
_DT_SCAN_SHARDS = ["00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "60"]
# 全市场主板「每股每交易日跌幅」表：{date: [chg_frac,...]}，一次扫描构建、O(1) 查任意日。
# 主板 ~2000 只 × ~600 交易日 ≈ 120 万行，内存 < 100MB，远优于逐日重复扫 800MB。
_DT_CHG_TABLE: Optional[dict] = None
_DT_CHG_TABLE_DIR: Optional[str] = None


def _load_json(path: str, key: str, context: dict, degraded: list) -> Optional[dict]:
    """加载 JSON 到 context[key]；失败置 None 并登记降级源。"""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            context[key] = json.load(fh)
        return context[key]
    except FileNotFoundError:
        degraded.append(f"{key}: 文件缺失 {path}")
    except (json.JSONDecodeError, OSError) as exc:
        degraded.append(f"{key}: 加载失败 {exc}")
    context[key] = None
    return None


def _load_moneyflow(live_dir: str, context: dict, degraded: list) -> None:
    """解析 stocks-data-live moneyflow/latest.txt 成 {code: {...}}。
    字段：code|date|close|chg%|turnover%|netinflow|netrate%|mainnet(r0)|mainrate%"""
    path = os.path.join(live_dir, "data", "moneyflow", "latest.txt")
    flows = {}
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                p = line.split("|")
                if len(p) < 9:
                    continue
                try:
                    flows[p[0]] = {
                        "date": p[1], "close": float(p[2]), "chg_pct": float(p[3]),
                        "turnover_pct": float(p[4]), "netinflow": float(p[5]),
                        "netrate_pct": float(p[6]), "mainnet": float(p[7]),
                        "mainrate_pct": float(p[8]),
                    }
                except ValueError:
                    continue
    except (FileNotFoundError, OSError) as exc:
        degraded.append(f"moneyflow: 不可用 {exc}")
        context["moneyflow"] = None
        return
    context["moneyflow"] = flows
    if flows:
        dates = {v["date"] for v in flows.values()}
        context["moneyflow_date"] = max(dates)
    else:
        context["moneyflow_date"] = None
        degraded.append("moneyflow: latest.txt 无有效数据行")


def _load_lhb(live_dir: str, context: dict, degraded: list) -> None:
    """龙虎榜：stocks-data-live/data/research/lhb_*.json 取最新一份。"""
    pats = sorted(glob.glob(os.path.join(live_dir, "data", "research", "lhb_*.json")))
    if not pats:
        context["lhb"] = None
        degraded.append("lhb: 龙虎榜文件缺失（research/lhb_*.json）")
        return
    try:
        with open(pats[-1], encoding="utf-8", errors="ignore") as fh:
            context["lhb"] = json.load(fh)
        context["lhb_file"] = os.path.basename(pats[-1])
    except (json.JSONDecodeError, OSError) as exc:
        context["lhb"] = None
        degraded.append(f"lhb: 加载失败 {exc}")


def _load_dt_pool(live_dir: str, context: dict, degraded: list) -> None:
    """跌停池：stocks-data-live/data/pool/*.dtpool 取最新，计精确跌停家数。"""
    pats = sorted(glob.glob(os.path.join(live_dir, "data", "pool", "*.dtpool")))
    if not pats:
        context["dt_pool"] = None
        context["dt_count"] = None
        degraded.append("dt_pool: 跌停池文件缺失（pool/*.dtpool）")
        return
    latest = pats[-1]
    try:
        with open(latest, encoding="utf-8", errors="ignore") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        context["dt_pool"] = None
        context["dt_count"] = None
        degraded.append(f"dt_pool: 读取/解析失败 {exc}")
        return
    # .dtpool 是单行 JSON：{"data": {"tc": 精确跌停家数, "pool": [...]}}
    data = payload.get("data") or {}
    pool = data.get("pool") or []
    tc = data.get("tc")
    dt_count = tc if isinstance(tc, int) else len(pool)
    context["dt_pool"] = {"file": os.path.basename(latest), "pool": pool, "tc": tc}
    context["dt_count"] = dt_count
    # 从文件名取日期（如 20261015.dtpool）
    base = os.path.basename(latest).split(".")[0]
    context["dt_pool_date"] = f"{base[:4]}-{base[4:6]}-{base[6:]}" if len(base) == 8 else base


def _load_index_closes(index_hist: str) -> Optional[list]:
    """读上证综指日线 txt（code|date|open|close|high|low|vol），返回 [(date, close)] 升序。"""
    rows = []
    try:
        with open(index_hist, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                p = line.split("|")
                if len(p) < 4:
                    continue
                try:
                    rows.append((p[1], float(p[3])))
                except ValueError:
                    continue
    except (FileNotFoundError, OSError):
        return None
    rows.sort(key=lambda r: r[0])
    return rows or None


def market_score_asof(asof: str, index_hist: str = DEFAULT_INDEX_HIST) -> Optional[float]:
    """无前视大盘评分：只用 asof 当日及之前的上证指数数据计算，0-100 归一。

    算法（三个分量等权，各 0-100）：
      1. MA20 位置：(close/MA20 - 1) ∈ [-10%, +10%] 线性映射到 [0,100]，50=贴线。
      2. MA60 位置：同上（中期趋势）。
      3. 近 20 日动量：涨跌幅 ∈ [-10%, +10%] 线性映射到 [0,100]。
    数据不足（asof 前不足 60 根 K 线）或文件缺失时返回 None（调用方记降级）。
    """
    rows = _load_index_closes(index_hist)
    if not rows:
        return None
    upto = [c for d, c in rows if d <= asof]
    if len(upto) < 60:
        return None
    close = upto[-1]
    ma20 = sum(upto[-20:]) / 20.0
    ma60 = sum(upto[-60:]) / 60.0
    mom20 = close / upto[-21] - 1.0

    def _pos(x: float) -> float:  # 偏离度 → 0-100
        return max(0.0, min(100.0, 50.0 + x * 500.0))

    score = (_pos(close / ma20 - 1.0) + _pos(close / ma60 - 1.0)
             + _pos(mom20)) / 3.0
    return round(score, 2)


def _mainboard(code: str) -> bool:
    """主板口径：沪市 sh60* + 深市 sz00*（排除 30 创业板 / 68 科创板 / 北交所）。"""
    return code.startswith(("sh60", "sz00"))


def _build_dt_chg_table(data_dir: str) -> dict:
    """一次流式扫全部主分片，构建主板 {date: [当日跌幅小数,...]} 表（无前视，前收=同 code 上一行）。"""
    global _DT_CHG_TABLE, _DT_CHG_TABLE_DIR
    if _DT_CHG_TABLE is not None and _DT_CHG_TABLE_DIR == data_dir:
        return _DT_CHG_TABLE
    table: dict = {}
    bulk = os.path.join(data_dir, "bulk")
    for shard in _DT_SCAN_SHARDS:
        path = os.path.join(bulk, f"day_qfq_{shard}.csv")
        try:
            fh = open(path, encoding="utf-8", errors="ignore")
        except (FileNotFoundError, OSError):
            continue
        with fh:
            prev_code, prev_close = None, None
            for line in fh:
                if not line or line[0] == "#":
                    continue
                p = line.split("|")
                if len(p) < 7:
                    prev_code = None
                    continue
                code = p[0]
                if not _mainboard(code):
                    prev_code, prev_close = None, None
                    continue
                try:
                    close = float(p[3])
                except ValueError:
                    continue
                if prev_code == code and prev_close:
                    table.setdefault(p[1], []).append(close / prev_close - 1.0)
                prev_code, prev_close = code, close
    _DT_CHG_TABLE = table
    _DT_CHG_TABLE_DIR = data_dir
    return table


def dt_count_asof(asof: str, data_dir: str = DEFAULT_DATA_DIR) -> Optional[int]:
    """无前视跌停家数：主板（sh60*/sz00*）asof 当日跌幅 ≤ -9.8% 的家数。

    数据源：全市场主分片一次扫描构建的 {date:[chg,...]} 表（_build_dt_chg_table），
    前收=同 code 上一交易日行，无未来函数。asof 非交易日返回 0；表为空返回 None。
    """
    table = _build_dt_chg_table(data_dir)
    if not table:
        return None
    chgs = table.get(asof)
    if not chgs:
        return 0  # 当日无主板交易行 = 非交易日
    return sum(1 for c in chgs if c <= -0.098)


def load_context(data_dir: str = DEFAULT_DATA_DIR,
                 live_dir: Optional[str] = DEFAULT_LIVE_DIR,
                 asof: Optional[str] = None) -> dict:
    """统一加载全部数据源。任何单源失败不崩，记入 _degraded_sources。

    asof=None（默认）：向后兼容，全部读当前快照（含前视偏差，仅实盘当日可用）。
    asof='YYYY-MM-DD'：无前视模式——market_score/dt_count 按 asof 当日重算，
    无历史序列的快照源（regime/ban_prob/limitup_ml/moneyflow/lhb 等）降级为 None。
    """
    context: dict = {"_data_dir": data_dir, "_live_dir": live_dir, "_asof": asof}
    degraded: list = []
    engines = os.path.join(data_dir, "engines")

    _load_json(os.path.join(engines, "market_anomaly_engine.json"),
               "market_anomaly", context, degraded)
    _load_json(os.path.join(engines, "regime_router.json"),
               "regime_router", context, degraded)
    _load_json(os.path.join(data_dir, "engine_fusion.json"),
               "engine_fusion", context, degraded)
    _load_json(os.path.join(data_dir, "engine_v5.json"),
               "engine_v5", context, degraded)
    _load_json(os.path.join(data_dir, "ban_prob.json"),
               "ban_prob", context, degraded)
    _load_json(os.path.join(data_dir, "limitup_ml_results.json"),
               "limitup_ml", context, degraded)

    # stocks-data-live 落地文件（先确认存在性，缺则跳过标注）
    if live_dir and os.path.isdir(live_dir):
        _load_moneyflow(live_dir, context, degraded)
        _load_lhb(live_dir, context, degraded)
        _load_dt_pool(live_dir, context, degraded)
    else:
        context["moneyflow"] = None
        context["lhb"] = None
        context["dt_pool"] = None
        context["dt_count"] = None
        degraded.append("stocks-data-live: 目录不可用，moneyflow/lhb/dt_pool 全部跳过")

    # ---- 无前视 asof 覆盖（前视偏差修复核心）----
    if asof:
        # 1) market_score：当前快照 E 作废，按 asof 从上证指数历史重算
        score = market_score_asof(asof)
        if score is None:
            degraded.append(
                f"market_score: asof={asof} 指数历史不足60根或文件缺失，无法无前视重算，置 None")
        context["market_score_asof"] = score
        context["market_score_source"] = "sh000001_history_asof" if score is not None else None
        context["_asof_market_score"] = score  # 暂存，派生段统一赋 market_score

        # 2) dt_count：当前跌停池快照作废，按 asof 从主分片全市场重算
        dtc = dt_count_asof(asof, data_dir)
        if dtc is None:
            degraded.append(f"dt_count: asof={asof} 主分片缺失，无法无前视统计，置 None")
        context["dt_count"] = dtc
        context["dt_pool_date"] = asof if dtc is not None else None
        context["dt_count_source"] = "bulk_mainboard_scan_asof" if dtc is not None else None

        # 3) 无历史序列的快照源：asof 模式下降级（防前视，不拿当前值冒充历史）
        for k, name in (("regime_router", "regime_router"),
                        ("ban_prob", "ban_prob"),
                        ("limitup_ml", "limitup_ml"),
                        ("moneyflow", "moneyflow"),
                        ("lhb", "lhb")):
            if context.get(k) is not None:
                context[k] = None
                degraded.append(f"{name}: 无历史序列，asof={asof} 模式降级跳过（防前视）")
        context["moneyflow_date"] = None
        context["lhb_file"] = None
        # market_anomaly 同样是当前快照：其事件计数不能用于历史日
        if context.get("market_anomaly") is not None:
            degraded.append(
                f"market_anomaly: 无历史序列（当前快照），asof={asof} 模式事件计数降级跳过")
            context["market_anomaly"] = None
        # engine_fusion/engine_v5 原始 dict 保留（top 榜等静态参考），但 E 不进入
        # market_score（asof 模式下由重算值取代）。

    # ---- 派生摘要字段（策略直接消费，均带 None 兜底）----
    ma = context.get("market_anomaly") or {}
    ec = ma.get("event_counts_in_window") or {}
    context["market_event_counts"] = ec if ec else None
    # 跌停潮：优先精确跌停池（最新文件），否则用 anomaly 引擎窗口事件计数；
    # asof 模式下 dt_count 已被无前视重算覆盖，走第一分支。
    if context.get("dt_count") is not None:
        context["dt_flood_count"] = context["dt_count"]
    elif ec:
        context["dt_flood_count"] = ec.get("dt_flood")
    else:
        context["dt_flood_count"] = None

    rr = context.get("regime_router") or {}
    context["regime"] = rr.get("regime")
    context["phase"] = rr.get("phase")
    context["regime_score"] = rr.get("regime_score")

    if asof:
        # 无前视模式：market_score 取 asof 重算值（或 None）
        context["market_score"] = context.get("_asof_market_score")
        # regime：regime_router.json 为当前快照（无历史序列），asof 模式不伪造历史，
        # 置 None 并记降级；回测的 regime 分段由 bt_driver 用 regime_router.judge_regime
        # 对指数历史逐日无前视判定（不经过本 context['regime']）。
        context["regime"] = None
        context["phase"] = None
        context["regime_score"] = None
        degraded.append(
            f"regime/phase/regime_score: 快照无历史序列，asof={asof} 置 None（防前视）")
        context["_nolookahead"] = True
    else:
        # 兼容模式：market_score 取当前快照 E（无前视保证，仅实盘当日）
        ef = context.get("engine_fusion") or {}
        context["market_score"] = ef.get("E")  # 大盘评分（-100~100 附近），缺失为 None

    context["_degraded_sources"] = degraded
    return context


def moneyflow_for(context: dict, code: str) -> Optional[dict]:
    """按股票代码取资金流记录（含换手率/主力净流入），无则 None。"""
    flows = context.get("moneyflow")
    if not flows:
        return None
    return flows.get(code) or flows.get(code.replace("sh", "").replace("sz", ""))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DEFAULT_DATA_DIR)
    ap.add_argument("--live", default=DEFAULT_LIVE_DIR)
    ap.add_argument("--asof", default=None, help="无前视模式：YYYY-MM-DD")
    args = ap.parse_args()
    ctx = load_context(args.data, args.live, asof=args.asof)
    summary = {k: (None if ctx.get(k) is None else (
        ctx[k] if k in ("regime", "phase", "regime_score", "market_score",
                        "dt_flood_count", "dt_count", "moneyflow_date",
                        "dt_pool_date", "lhb_file") else "loaded"))
               for k in ("market_anomaly", "regime_router", "engine_fusion",
                         "engine_v5", "ban_prob", "limitup_ml", "moneyflow",
                         "lhb", "dt_pool", "regime", "phase", "regime_score",
                         "market_score", "dt_flood_count", "moneyflow_date")}
    summary["moneyflow_codes"] = len(ctx.get("moneyflow") or {})
    summary["asof"] = args.asof
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("degraded:", json.dumps(ctx["_degraded_sources"], ensure_ascii=False, indent=2))
