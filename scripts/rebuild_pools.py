#!/usr/bin/env python3
"""
rebuild_pools.py — 用可达数据源重建网站的池子数据 (带完整来源标注)

背景 (2026-10-12 审计发现):
  data/sector_pool.json / cycle.json / lowpool.json 三个文件 MD5 完全相同,
  内容都是 cycle_v2 格式、日期 2026-10-09 (上周五) —— 远端系统写入错位。
  且原 sector_pool 因东财 push2 被封只剩 2 个板块, lowpool 0 只,
  multi_v5 因缺「个股→行业」映射出 0 买入。

本脚本:
  1. sector_pool.json  ← 腾讯 mktHs 行业板块排行 (含涨跌/5日/20日/领涨股)
  2. cycle.json        ← 腾讯指数日K 计算的周期信号 (MA/MACD/RSI/量比)
  3. 每个文件都带 meta: {source, ts, date, trading_day, caveat}

用法:
  python3 rebuild_pools.py --out-dir /path/to/ai-view/data
  python3 rebuild_pools.py --out-dir ... --date 20261012
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
      "Referer": "https://gu.qq.com/"}

# 指数清单: (腾讯代码, 显示名)
INDICES = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sh000300", "沪深300"),
    ("sz399006", "创业板指"),
    ("sh000688", "科创50"),
    ("bj899050", "北证50"),
]


def _http(url, timeout=15, retries=2, headers=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    for i in range(retries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                          timeout=timeout).read()
        except Exception:
            if i < retries - 1:
                import time
                time.sleep(0.4)
    return b""


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── 1. 腾讯行业板块排行 ────────────────────────────────────────────
def build_sector_pool():
    """腾讯行业板块 → 分级板块池。

    分级口径 (基于板块当日涨幅 + 5/20日区间涨幅):
      tier1 (强势): 当日涨幅 ≥ 2% 且 5日涨幅 > 0
      tier2 (活跃): 当日涨幅 ≥ 1% 或 5日涨幅 ≥ 3%
      tier3 (弱势): 其余
      tier_down:     当日涨幅 ≤ -2%
    """
    url = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/mktHs/rank"
           "?l=100&p=1&t=01/averatio")
    body = _http(url, timeout=15)
    if not body:
        return None
    try:
        d = json.loads(body.decode("utf-8"))
    except Exception as e:
        print(f"[sector] parse err: {e}", file=sys.stderr)
        return None

    rows = d.get("data") or []
    if not rows:
        return None

    now = datetime.now(CST)
    sectors = []
    for r in rows:
        name = str(r.get("bd_name", "") or "").strip()
        if not name:
            continue
        chg = _f(r.get("bd_zdf"))
        chg5 = _f(r.get("bd_zdf5"))
        chg20 = _f(r.get("bd_zdf20"))
        if chg is None:
            continue
        sectors.append({
            "sector": name,
            "code": str(r.get("bd_code", "") or ""),
            "score": round(chg, 2),                 # 以当日涨幅为强度分
            "avg_chg_1d": round(chg, 2),
            "chg_5d": round(chg5, 2) if chg5 is not None else None,
            "chg_20d": round(chg20, 2) if chg20 is not None else None,
            "leader": str(r.get("nzg_name", "") or ""),
            "leader_pct": _f(r.get("nzg_zdf")),
            "level": round(_f(r.get("bd_zs")) or 0, 2),   # 板块指数点位
        })

    tier1, tier2, tier3, tdown = [], [], [], []
    for s in sectors:
        c5 = s["chg_5d"] or 0
        if s["avg_chg_1d"] >= 2 and c5 > 0:
            tier1.append(s)
        elif s["avg_chg_1d"] >= 1 or c5 >= 3:
            tier2.append(s)
        elif s["avg_chg_1d"] <= -2:
            tdown.append(s)
        else:
            tier3.append(s)

    for lst in (tier1, tier2, tier3, tdown):
        lst.sort(key=lambda x: -x["score"])

    return {
        "date": now.strftime("%Y-%m-%d"),
        "market_phase": None,          # 由 cycle.json 提供, 避免两处口径不一
        "total_sectors": len(sectors),
        "tier1_count": len(tier1),
        "tier2_count": len(tier2),
        "tier3_count": len(tier3),
        "tier_down_count": len(tdown),
        "tier1": tier1[:20],
        "tier2": tier2[:30],
        "tier3": tier3[:40],
        "tier_down": tdown[:20],
        # ── 元数据: 数据来源 / 时间 / 口径 (用户要求标注) ──
        "meta": {
            "source": "腾讯证券 proxy.finance.qq.com mktHs/rank",
            "ts": now.isoformat(),
            "freq": "实时 (盘中随行情更新, 收盘后定格)",
            "grading": "tier1=当日涨幅≥2%且5日>0; tier2=当日≥1%或5日≥3%; "
                       "tier_down=当日≤-2%; 其余 tier3",
            "fields": {
                "avg_chg_1d": "板块当日涨跌幅 %",
                "chg_5d": "板块近5日涨跌幅 %",
                "chg_20d": "板块近20日涨跌幅 %",
                "leader": "板块内领涨股",
                "leader_pct": "领涨股涨跌幅 %",
            },
            "caveat": "腾讯行业分类口径与申万/东财行业不同, 板块数量约100个; "
                      "不含主力资金净流入字段 (该源不提供)",
            "rebuilt": "rebuild_pools.py (原 sector_pool.json 因远端写入错位, "
                       "内容为 cycle_v2/2026-10-09, 已重建)",
        },
    }


# ─── 2. 周期池 (腾讯指数日K) ───────────────────────────────────────
def _ma(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _ema(closes, n):
    if len(closes) < n:
        return None
    k = 2 / (n + 1)
    e = closes[0]
    for v in closes[1:]:
        e = e + k * (v - e)
    return e


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0
        l = -d if d < 0 else 0
        ag = (ag * (period - 1) + g) / period
        al = (al * (period - 1) + l) / period
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)


def build_cycle_pool(days=120):
    """腾讯指数日K → 周期信号 (MA/MACD/RSI/量比)。"""
    now = datetime.now(CST)
    macro = {}
    klines = {}

    for code, name in INDICES:
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               f"?param={code},day,,,{days},qfq")
        body = _http(url, timeout=15)
        if not body:
            continue
        try:
            d = json.loads(body.decode("utf-8"))
            node = (d.get("data") or {}).get(code) or {}
            arr = node.get("qfqday") or node.get("day") or []
        except Exception:
            continue
        if len(arr) < 30:
            continue

        closes = [float(r[2]) for r in arr if len(r) > 2 and float(r[2]) > 0]
        vols = [float(r[5]) for r in arr if len(r) > 5 and float(r[5]) > 0]
        if len(closes) < 30:
            continue

        ma5, ma20, ma60 = _ma(closes, 5), _ma(closes, 20), _ma(closes, 60)
        ema12, ema26 = _ema(closes[-60:], 12), _ema(closes[-60:], 26)
        macd_hist = None
        if ema12 is not None and ema26 is not None:
            macd_hist = round(ema12 - ema26, 2)
        rsi = _rsi(closes)
        vr = round(vols[-1] / (sum(vols[-6:-1]) / 5), 2) if len(vols) >= 6 else None

        # 周期评分: 均线多头 + MACD 正 + RSI 中性偏强 = 高分
        score = 0
        score += 3 if (ma5 and ma20 and ma5 > ma20) else 0
        score += 2 if (ma20 and ma60 and ma20 > ma60) else 0
        score += 2 if (macd_hist or 0) > 0 else 0
        score += 1 if (rsi or 50) >= 50 else 0
        score -= 2 if (rsi or 50) < 30 else 0
        score -= 3 if (ma5 and ma20 and ma5 < ma20 and (macd_hist or 0) < 0) else 0

        if score >= 5:
            phase = "uptrend"
        elif score >= 2:
            phase = "recovery"
        elif score >= 0:
            phase = "neutral"
        elif score >= -2:
            phase = "decline"
        else:
            phase = "downtrend"

        macro[code] = {
            "name": name,
            "phase": phase,
            "score": score,
            "close": round(closes[-1], 2),
            "ma5": round(ma5, 2) if ma5 else None,
            "ma20": round(ma20, 2) if ma20 else None,
            "ma60": round(ma60, 2) if ma60 else None,
            "ma_bull": bool(ma5 and ma20 and ma5 > ma20),
            "macd_bull": bool((macd_hist or 0) > 0),
            "macd_hist": macd_hist,
            "rsi": rsi,
            "vr": vr,
            "vol_regime": ("high" if (vr or 1) >= 1.3
                           else "low" if (vr or 1) <= 0.7 else "normal"),
        }
        klines[code] = [{"date": r[0], "close": float(r[2]),
                         "vol": float(r[5]) if len(r) > 5 else 0} for r in arr]

    if not macro:
        return None, None

    # 市场总周期: 以上证为主, 其余取均值参考
    avg = sum(v["score"] for v in macro.values()) / len(macro)
    sh = macro.get("sh000001", {})
    mkt = sh.get("phase") or ("uptrend" if avg >= 4 else
                              "recovery" if avg >= 1 else
                              "decline" if avg >= -2 else "downtrend")

    return {
        "date": now.strftime("%Y-%m-%d"),
        "market_phase": mkt,
        "macro_signals": macro,
        "market_score": round(avg, 1),
        "meta": {
            "source": "腾讯证券 web.ifzq.gtimg.cn 指数日K (qfq 前复权)",
            "ts": now.isoformat(),
            "window": f"近 {days} 个交易日",
            "phase_rules": "uptrend≥5 / recovery≥2 / neutral≥0 / decline≥-2 / downtrend<-2",
            "score_rules": "MA5>MA20 +3; MA20>MA60 +2; MACD>0 +2; RSI≥50 +1; "
                           "RSI<30 -2; MA5<MA20且MACD<0 -3",
            "fields": {
                "vr": "当日量 / 前5日均量 (量比, >1.3 放量, <0.7 缩量)",
                "rsi": "14日 RSI",
                "macd_hist": "EMA12-EMA26 (简化口径, 未含 DEA)",
            },
            "caveat": "MACD 为简化口径 (未算 DEA 信号线); 周期判定为技术面单一口径, "
                      "不含基本面/资金面",
            "rebuilt": "rebuild_pools.py (原 cycle.json 因远端写入错位已重建)",
        },
    }, klines


# ─── main ─────────────────────────────────────────────────────────
def _prev_date(out_dir, name):
    path = os.path.join(out_dir, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("date")
    except Exception:
        return None


def _write_stage_status(out_dir, stage, status, reason, prev_date):
    """阶段产物状态：失败时显式落盘，避免下游把旧文件当当日数据。"""
    payload = {
        "stage": stage,
        "status": status,
        "reason": reason,
        "at": datetime.now(CST).isoformat(timespec="seconds"),
        "prev_file_date": prev_date,
    }
    path = os.path.join(out_dir, f"{stage}_status.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[{stage}] status={status} reason={reason} -> {path}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True, help="ai-view/data 目录")
    ap.add_argument("--date", help="YYYYMMDD, 仅用于日志")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    now = datetime.now(CST)
    print(f"=== rebuild_pools {now:%Y-%m-%d %H:%M CST} ===", flush=True)

    # 1. 板块池
    sp = build_sector_pool()
    if sp:
        p = os.path.join(args.out_dir, "sector_pool.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(sp, f, ensure_ascii=False, indent=2)
        print(f"[sector_pool] {sp['total_sectors']} 板块 -> "
              f"tier1={sp['tier1_count']} tier2={sp['tier2_count']} "
              f"tier3={sp['tier3_count']} down={sp['tier_down_count']}")
        _write_stage_status(args.out_dir, "sector_pool", "ok", None, sp["date"])
        for s in sp["tier1"][:5]:
            print(f"    {s['sector']:12s} 当日{s['avg_chg_1d']:+.2f}% "
                  f"5日{s['chg_5d']:+.2f}% 领涨 {s['leader']}")
    else:
        print("[sector_pool] ❌ 腾讯行业接口不可用")
        _write_stage_status(args.out_dir, "sector_pool", "failed",
                            "腾讯行业接口不可用",
                            _prev_date(args.out_dir, "sector_pool.json"))

    # 2. 周期池
    # 回看窗口: INDEX_KLINE_DAYS > index_history.json 里最短的可用指数长度 > 500
    _days = int(os.environ.get("INDEX_KLINE_DAYS") or 0)
    if not _days:
        try:
            with open(os.path.join(args.out_dir, "index_history.json"),
                      encoding="utf-8") as f:
                _ih = json.load(f)
            _lens = [len(v) for v in (_ih.get("indices") or {}).values()
                     if isinstance(v, list)]
            _lens = [n for n in _lens if n > 0]
            _days = min(_lens) if _lens else 500
        except Exception:
            _days = 500
    print(f"[cycle] 回看窗口 {_days} 根 (INDEX_KLINE_DAYS={os.environ.get('INDEX_KLINE_DAYS') or '-'})")
    cp, klines = build_cycle_pool(_days)
    if cp:
        p = os.path.join(args.out_dir, "cycle.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cp, f, ensure_ascii=False, indent=2)
        print(f"[cycle] market_phase={cp['market_phase']} score={cp['market_score']}")
        for code, v in cp["macro_signals"].items():
            print(f"    {v['name']:8s} {v['phase']:9s} score={v['score']:+d} "
                  f"RSI={v['rsi']} vr={v['vr']}")

        # 顺带产出指数K线 (给成交量线图用)
        if klines and "sh000001" in klines:
            kp = os.path.join(args.out_dir, "index_kline.json")
            with open(kp, "w", encoding="utf-8") as f:
                json.dump({"date": cp["date"], "ts": cp["meta"]["ts"],
                           "indices": klines,
                           "meta": {"source": cp["meta"]["source"],
                                    "freq": "日K (收盘后定格)"}},
                          f, ensure_ascii=False)
            print(f"[index_kline] 上证 {len(klines['sh000001'])} 根 -> {kp}")
    else:
        print("[cycle] ❌ 腾讯指数K线不可用")
        _write_stage_status(args.out_dir, "cycle", "failed", "腾讯指数K线不可用", None)

    print("\nDONE")
    return 0 if (sp and cp) else 1


if __name__ == "__main__":
    sys.exit(main())
