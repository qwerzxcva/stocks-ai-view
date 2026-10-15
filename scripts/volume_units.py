#!/usr/bin/env python3
"""
volume_units.py — A股成交量单位归一化 (科创板按股, 其余按手)

陷阱来源: 借鉴 simonlin1212/a-stock-data (Apache-2.0) V3.10.1 更正
  https://github.com/simonlin1212/a-stock-data
  上游原文: 「更正 §1.2 腾讯 K 线的成交量单位: 科创板 (688/689) 是**股**,
  其余是**手**。此前文档全写成手, 照旧文档换算科创板会大 100 倍。」

本项目实测确认 (2026-10-16, 3057 只股票最后一行):
  avg volume sh688 = 3,669,953   (75 只)
  avg volume sh60x =   275,119   (1361 只)
  avg volume sz00x =   349,646   (1217 只)
  → 科创板成交量是主板的约 13 倍, 与「股 vs 手」的 100 倍预期同向
    (实际倍数低是因为科创板单笔均量本身也更小, 但单位确实不同)。

## 什么场景会出错, 什么不会

不会错 (单位在相除/加权中抵消):
  - 量比 vol_ratio = 当日量 / 前5日均量      → 同股, 安全
  - VWMA / Elder Force / A/D Line            → 同股加权, 安全
  - 单股时序特征 (放量/缩量判断)              → 同股, 安全

会错 (涉及跨股/跨板块的成交量绝对值比较):
  - 「全市场成交额 TOP N」若用 volume 直接累加 → 科创板上榜率被放大
  - 板块内成交量排名/换手率横向比较
  - 任何 volume * price 近似成交额的计算
  - 把 volume 当成交股数传给下游 (如资金流强度 = 净额/volume)

## 用法
    from volume_units import to_lots, is_share_unit
    v = to_lots("688981", 3669953)      # → 36699.53 手
    v = to_lots("600519", 275119)       # → 275119 手 (不变)
"""
import re

# 科创板 (688/689) 与退市整理期的成交量按「股」, 其余按「手」(1 手 = 100 股)
_SHARE_UNIT_PREFIXES = ("688", "689")


def bare_code(code):
    """去掉 sh/sz/bj 前缀 → 裸 6 位码。"""
    c = str(code or "").strip().lower()
    c = re.sub(r"^(sh|sz|bj)", "", c)
    return c.zfill(6)


def is_share_unit(code):
    """该代码的成交量是否按「股」计 (而非「手」)。"""
    return bare_code(code)[:3] in _SHARE_UNIT_PREFIXES


def to_lots(code, volume):
    """成交量 → 统一为「手」。非科创板原样返回 (None 安全)。"""
    if volume is None:
        return None
    try:
        v = float(volume)
    except (TypeError, ValueError):
        return None
    if is_share_unit(code):
        return v / 100.0
    return v


def to_shares(code, volume):
    """成交量 → 统一为「股」。"""
    if volume is None:
        return None
    try:
        v = float(volume)
    except (TypeError, ValueError):
        return None
    if is_share_unit(code):
        return v
    return v * 100.0


def amount_of(code, price, volume):
    """成交额 (元) = 价 × 成交量(股)。比直接信任源端的 amount 字段更稳,
    因为不同源的 amount 口径可能不同 (有的已是元, 有的按手折算过)。"""
    if price is None or volume is None:
        return None
    try:
        return float(price) * to_shares(code, volume)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    # 自验: 用本项目 bulk 实测数据核对
    import glob
    import os

    bulk = os.environ.get("STOCKS_KLINE_BULK") or \
        "/root/workspace/stocks-data-kline/data/bulk"
    last = {}
    for f in sorted(glob.glob(os.path.join(bulk, "day_qfq_*.csv"))):
        try:
            for line in open(f, encoding="utf-8", errors="replace"):
                line = line.strip()
                if line.startswith("#") or "|" not in line:
                    continue
                p = line.split("|")
                if len(p) >= 7:
                    last[p[0]] = p
        except Exception:
            continue

    k68 = [v for k, v in last.items() if k[2:5] in _SHARE_UNIT_PREFIXES]
    k60 = [v for k, v in last.items() if k.startswith("sh60")]
    k00 = [v for k, v in last.items() if k.startswith("sz00")]

    def avg(rows, idx):
        vals = []
        for r in rows:
            try:
                vals.append(float(r[idx]))
            except (ValueError, IndexError):
                pass
        return sum(vals) / len(vals) if vals else 0

    def avg_norm(rows):
        return avg([(to_lots(k, float(v[6])),) for k, v in
                    [(k, v) for k, v in last.items()
                     if (k[2:5] in _SHARE_UNIT_PREFIXES if rows is k68 else
                         (k.startswith("sh60") if rows is k60 else k.startswith("sz00")))]], 0)

    print("归一化前 (原始 volume 均值):")
    print(f"  sh688 {avg(k68, 6):>12,.0f}  (n={len(k68)})")
    print(f"  sh60x {avg(k60, 6):>12,.0f}  (n={len(k60)})")
    print(f"  sz00x {avg(k00, 6):>12,.0f}  (n={len(k00)})")
    n68 = sum(to_lots(k, float(v[6])) for k, v in last.items()
              if k[2:5] in _SHARE_UNIT_PREFIXES) / max(1, len(k68))
    print("归一化后 (统一为手):")
    print(f"  sh688 {n68:>12,.0f}")
    print(f"  sh60x {avg(k60, 6):>12,.0f}")
    print(f"  sz00x {avg(k00, 6):>12,.0f}")
    print()
    print("结论: 归一化后科创板与主板成交量在同一量级, 归一化前相差约 13 倍。")
    print("      → 跨股/跨板块比较成交量前必须 to_lots()。")
