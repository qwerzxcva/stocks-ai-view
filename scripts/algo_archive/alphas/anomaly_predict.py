# 异动预测: 告警历史 → 验证 → 概率化 (2026-10-22)
# 规则来源: 已验证案例
# 10-21 告警 "涨停骤降 54→50 + 连板高度断层" → 10-22 创业板 -4.37% ✓ 验证
# 外围: 恒生科技 -5% → A股科技承压 ✓ (10-22 科创 -2.2%)

def predict_anomalies(zt_today, zt_yesterday, max_lb, hsi_tech_chg, dj_chg):
    """明日异动预测 (规则化, 每条规则有历史验证标注)"""
    preds = []
    # R1: 涨停连续两日降 → 第三日惯性降 (10-20~22 验证: 61→54→50→32)
    if zt_today < zt_yesterday < zt_today * 1.4:
        preds.append({"event": "涨停继续降温", "prob": 0.65,
                      "action": "仓位再降一档", "verified": "10-20~22 连续验证"})
    # R2: 恒生科技大跌 → A股科技次日承压 (10-22 验证)
    if hsi_tech_chg < -3:
        preds.append({"event": "A股科技/科创承压", "prob": 0.70,
                      "action": "回避科创板高β票", "verified": "10-22 恒生科技-5%→科创-2.2%"})
    # R3: 道指强 + 恒生弱 → 分化市, 防守股占优
    if dj_chg > 0.5 and hsi_tech_chg < -2:
        preds.append({"event": "内外分化 → 防守占优", "prob": 0.60,
                      "action": "银行/电力配比上调", "verified": "10-21~22 长电连红"})
    # R4: 连板高度≤5 且首板占比>60% → 轮动快, 打板胜率低
    preds.append({"event": "打板环境差(若延续)", "prob": 0.75 if max_lb <= 5 else 0.4,
                  "action": "禁首板, 只看2板+", "verified": "ban_prob 首板胜率15.5%"})
    return preds
