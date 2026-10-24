# engine 10: 吸筹检测 (Wyckoff accumulation + 资金流确认)
# 形态1 价平量增: 20日振幅<8% 且 近5日均量为前15日 1.8x+ (主库验证: 华数传媒 2.7x)
# 形态2 资金流背离: 20日主力净流入 top 而股价 20 日涨幅<5% (钱进价未动)
# 输出 acc_score 0-100
def acc_score(range20, vol_expand, mf_net20, ret20):
    s = 0
    if range20 is not None and range20 < 0.08: s += 30          # 横盘
    if vol_expand is not None:
        if vol_expand > 2.5: s += 40
        elif vol_expand > 1.8: s += 25
    if mf_net20 is not None and mf_net20 > 5e8: s += 20
    if mf_net20 is not None and mf_net20 > 2e9: s += 10          # 特大额
    if ret20 is not None and ret20 < 0.05: s += 10               # 价未兑现
    return s
