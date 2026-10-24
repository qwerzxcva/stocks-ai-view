# engine 11 数据: 涨停连板次日实证概率表 (吸收自 gitcode dufuwuxin/stock-system ban_prob.json)
# 样本: 5322 票 × 29882 涨停事件 × 腾讯前复权日K 全历史 (2026-10-22 同步)
BAN_PROB = {
    1: {"n": 28468, "zt_next": 3.59,  "zhaban": 2.56,  "ret_next": 0.24, "win": 15.5},
    2: {"n": 1011,  "zt_next": 24.53, "zhaban": 14.24, "ret_next": 1.77, "win": 57.47},
    3: {"n": 248,   "zt_next": 39.52, "zhaban": 15.32, "ret_next": 2.79, "win": 63.71},
    4: {"n": 96,    "zt_next": 38.54, "zhaban": 22.92, "ret_next": 2.86, "win": 67.71},
    5: {"n": 37,    "zt_next": 40.54, "zhaban": 18.92, "ret_next": 3.51, "win": 67.57},
    6: {"n": 22,    "zt_next": 31.82, "zhaban": 36.36, "ret_next": 4.19, "win": 77.27},
}
def lianban_edge(lb):
    """连板高度 → 次日期望收益% 与胜率% (lb=0 首板未涨停参照)"""
    return BAN_PROB.get(lb, BAN_PROB[1])
# 洞察: 首板追入胜率 15.5% (亏钱), 2 板起跳胜率 57%+, 连板越高越强直到 6 板
