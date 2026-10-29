#!/usr/bin/env python3
"""limit_caliber.py — 涨停/跌停口径的唯一实现（single source of truth）。

## 为什么单独成文件

盘面上「涨停」此前有两套互不兼容的判定：

    phase_runner.py:82        固定 ret >= 0.095     → 300/688 的真涨停(19.9%)算不进梯队
    build_limit_history.py:48 按前缀 9.9/19.9/29.9  → 与上面不一致
    market_phase.py(demo)     同样 9.5%

同一只创业板票在 `market_phase.json` 与 `limit_history.json` 里可能一边是涨停
一边不是，而 `engine_fusion` 的 0.30 权重直接吃 market_phase。所以判定必须收敛到
一处：任何脚本要判涨停都 import 本模块，不再自带阈值。

## 口径

    pct   = 按代码前缀给的涨跌幅上限（百分点）
    limit = 判涨停的触发线 = pct - TOLERANCE
    ret   = (close/prev_close - 1) * 100

ST(±5%)、新股首日无涨跌幅、北交所 30cm 的细节见 `caveats()`，供产物写进
`caliber` 字段，避免下游把近似当精确。
"""
import re

# 容差：收盘价四舍五入到分，真实涨停的 ret 常落在 9.87~9.99。
# 0.15 个百分点是为了容纳「9.9% 触发线 + 分位舍入」，不是给非涨停留后门。
TOLERANCE = 0.15

# 前缀 → 涨跌幅上限（百分点）。顺序即优先级。
PREFIX_LIMITS = (
    (("688", "689", "30"), 20.0),      # 科创板 / 创业板注册制
    (("43", "83", "87", "88", "92"), 30.0),  # 北交所
    (("ST",), 5.0),                    # 名称 ST 标记，需调用方传 name 才生效
)


def bare(code: str) -> str:
    """去掉 sh/sz/bj 前缀与后缀。"""
    c = str(code or "").strip().lower()
    c = re.sub(r"^(sh|sz|bj)", "", c)
    c = re.sub(r"\.(sh|sz|bj)$", "", c)
    return c


def limit_pct(code: str, name: str = "") -> float:
    """该股的涨跌幅上限（百分点）。ST 优先于板块。"""
    if name and ("ST" in name.upper()):
        return 5.0
    b = bare(code)
    for prefixes, pct in PREFIX_LIMITS:
        if b.startswith(prefixes):
            return pct
    return 10.0


def limit_line(code: str, name: str = "") -> float:
    """判涨停的触发线（百分点）。"""
    return limit_pct(code, name) - TOLERANCE


def is_limit_up(code: str, ret_pct: float, name: str = "") -> bool:
    """ret_pct 为当日涨跌幅百分数（例如 9.98 表示 +9.98%）。"""
    try:
        return float(ret_pct) >= limit_line(code, name)
    except (TypeError, ValueError):
        return False


def is_limit_down(code: str, ret_pct: float, name: str = "") -> bool:
    try:
        return float(ret_pct) <= -limit_line(code, name)
    except (TypeError, ValueError):
        return False


def is_limit_up_pair(code: str, prev_close: float, close: float, name: str = "") -> bool:
    if not prev_close or prev_close <= 0:
        return False
    return is_limit_up(code, (close / prev_close - 1) * 100, name)


def is_main_board(code: str) -> bool:
    """无科创/创业板权限的可交易范围（与 engine_v5.MAIN_BOARD_PREFIX 同源）。"""
    return bare(code).startswith(("600", "601", "603", "605", "000", "001", "002"))


def caveats() -> str:
    return ("按收盘价涨幅判定，容差 %.2f 个百分点；不含 ST 5%% 特例（调用方需传 name）、"
            "新股上市首日无涨跌幅限制会误判；日线口径不含封单量与炸板。" % TOLERANCE)


if __name__ == "__main__":
    for c in ("600000", "300750", "688111", "830799", "000001"):
        print(c, "limit=%.2f line=%.2f" % (limit_pct(c), limit_line(c)),
              "9.9→", is_limit_up(c, 9.9), "19.9→", is_limit_up(c, 19.9))
    print("ST:", limit_pct("600001", "*ST某某"))
