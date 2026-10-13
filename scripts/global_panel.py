#!/usr/bin/env python3
# ═══ 来源: GitCode dufuwuxin/stock-system · engines/global_panel.py ═══
# 上游 commit: cf5091f9953c · 拉取时间: 2026-10-13 22:11 · 工具: scripts/sync_gitcode.py --pull-engines
# 该文件由 GitCode 侧维护 (云容器/上游引擎), GitHub 仅作镜像; 如需改动请同步回上游。
# 适配: 2026-10-13 路径规范 (输出 stockdb/data/global → data/global/; global_kline 读 data/; --out-dir)
"""全球市场面板 v1: 海外数据 → 大A影响分析
源: 腾讯qt(美股指数/ETF/黄金/原油/港股) + 新浪(A50期货/汇率)
输出: data/global/latest.json + 关键传导指标 → 网站/复盘用
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA as DATA_DIR
except Exception:
    DATA_DIR = os.path.join(os.path.dirname(HERE), "data")

DEFAULT_OUT = os.path.join(DATA_DIR, "global")
os.makedirs(DEFAULT_OUT, exist_ok=True)
GLOBAL_KLINE = os.path.join(DATA_DIR, "global_kline.json")
QT_HDR = {"Referer": "https://gu.qq.com"}
SINA_HDR = {"Referer": "https://finance.sina.com.cn"}


def _get_text(url, headers, timeout=8, retries=3):
    """腾讯/新浪行情偶发连接重置, 重试; 全失败返回空串由调用方按缺失处理。"""
    last = None
    for _ in range(retries):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                          timeout=timeout).read().decode("gbk", "ignore")
        except Exception as e:
            last = e
    print(f"[global_panel] 请求失败(重试{retries}次): {url[:60]} {last}", file=sys.stderr)
    return ""

# ---- 腾讯全球: (代码, 名称, 分组) ----
QT_CODES = [
    ("usDJI", "道琼斯", "美股指数"), ("usIXIC", "纳斯达克", "美股指数"),
    ("usINX", "标普500", "美股指数"), ("usVIX", "VIX恐慌指数", "美股指数"),
    ("usSOXX", "费城半导体ETF", "中国关联"), ("usKWEB", "中概互联网ETF", "中国关联"),
    ("usFXI", "中国大盘ETF", "中国关联"), ("usMCHI", "中国ETF-MSCI", "中国关联"),
    ("usEEM", "新兴市场ETF", "中国关联"), ("usQQQ", "纳指100ETF", "美股指数"),
    ("hkHSI", "恒生指数", "港股"), ("hkHSTECH", "恒生科技", "港股"),
    ("hf_XAU", "伦敦金", "商品"), ("hf_CL", "WTI原油", "商品"),
]

# ---- 新浪: hf/fx 特殊解析 ----
SINA_CODES = [
    ("hf_CHA50CFD", "富时A50期货", "中国关联"),
    ("fx_susdcnh", "离岸人民币", "汇率"),
]


def qt_batch(codes):
    out = {}
    u = "https://qt.gtimg.cn/q=" + ",".join(c[0] for c in codes)
    b = _get_text(u, QT_HDR)
    lines = b.strip().split(";")
    for (code, name, grp) in codes:
        for line in lines:
            line = line.strip()
            if line.startswith(f'v_{code}="') and len(line) > 30:
                body = line.split('"')[1]
                try:
                    if code.startswith("hf_"):  # 期货: 逗号分隔 p0=现价 p1=涨跌% p6=时间
                        p = body.split(",")
                        price, pct, t = float(p[0]), float(p[1]), p[6]
                    else:  # 指数/股票: ~分隔 p3=价 p32=涨跌% p30=时间
                        p = body.split("~")
                        price, pct = float(p[3]), float(p[32])
                        t = p[30][:14] if len(p) > 30 else ""
                    out[code] = {"code": code, "name": name, "group": grp,
                                 "price": price, "pct": pct, "time": t}
                except (ValueError, IndexError):
                    pass
                break
    return out


def sina_batch(codes):
    out = {}
    u = "https://hq.sinajs.cn/list=" + ",".join(c[0] for c in codes)
    b = _get_text(u, SINA_HDR)
    for (code, name, grp) in codes:
        for line in b.strip().split("\n"):
            if f'hq_str_{code}="' in line:
                p = line.split('"')[1].split(",")
                try:
                    if code.startswith("hf_"):
                        # A50: p[0]=价 p[2]=昨结? p[3]=买 p[4]=卖... 用 p[0]与p[8](昨结)算涨跌
                        price = float(p[0]); prev = float(p[8])
                        pct = (price / prev - 1) * 100 if prev else 0
                        t = p[6]
                    else:  # fx: p[1]=卖 p[2]=买 p[3]=昨收
                        price = float(p[1]); prev = float(p[3])
                        pct = (price / prev - 1) * 100 if prev else 0
                        t = p[0]
                    out[code] = {"code": code, "name": name, "group": grp,
                                 "price": price, "pct": round(pct, 3), "time": t}
                except (ValueError, IndexError):
                    pass
                break
    return out


def analyze(items):
    """全球→大A 传导信号"""
    sig = {}
    def get(code):
        return items.get(code, {})
    a50 = get("hf_CHA50CFD"); kweb = get("usKWEB"); vix = get("usVIX")
    cny = get("fx_susdcnh"); sox = get("usSOXX"); nasdq = get("usIXIC")
    signals = []
    if a50:
        signals.append(f"A50期货 {a50['pct']:+.2f}% — 夜盘/早盘外资对大A的最直接定价")
        sig["a50_pct"] = a50["pct"]
    if kweb:
        signals.append(f"中概KWEB {kweb['pct']:+.2f}% — 隔夜中概情绪")
        sig["kweb_pct"] = kweb["pct"]
    if vix:
        lvl = vix["price"]
        mood = "恐慌" if lvl > 25 else ("警惕" if lvl > 20 else "平稳")
        signals.append(f"VIX {lvl} ({mood}) — {'风险偏好收缩, 大A承压' if lvl>25 else '风险偏好正常' if lvl<20 else '中性偏谨慎'}")
        sig["vix"] = lvl
    if cny:
        d = cny["pct"]
        signals.append(f"离岸人民币 {cny['price']} ({d:+.2f}%) — {'升值利好外资流入' if d<0 else '贬值压力, 外资流出风险'}")
        sig["cny_pct"] = d
    if sox:
        signals.append(f"费半ETF {sox['pct']:+.2f}% — A股半导体链情绪锚")
        sig["sox_pct"] = sox["pct"]
    # 综合评分: -100~+100
    score = 0
    score += max(-30, min(30, sig.get("a50_pct", 0) * 10))
    score += max(-20, min(20, sig.get("kweb_pct", 0) * 5))
    score -= max(0, sig.get("vix", 20) - 20) * 3
    score += max(-15, min(15, -sig.get("cny_pct", 0) * 20))
    score += max(-15, min(15, sig.get("sox_pct", 0) * 5))
    # 全球指数真实K线动量(腾讯usfqkline, global_kline.py产出)
    try:
        import json as _json
        _gk = _json.load(open(GLOBAL_KLINE, encoding="utf-8"))
        mom = {}
        for s, v in _gk.get("symbols", {}).items():
            closes = [b[2] for b in v.get("bars", []) if len(b) >= 3]
            if len(closes) >= 21:
                m5 = (closes[-1] / closes[-6] - 1) * 100
                m20 = (closes[-1] / closes[-21] - 1) * 100
                mom[s] = {"m5": round(m5, 2), "m20": round(m20, 2)}
        if mom:
            sig["global_mom"] = mom
            # 隔夜美股(道指+纳指)5日动量计入评分
            for k in ("usDJI", "usIXIC"):
                if k in mom:
                    score += max(-15, min(15, mom[k]["m5"] * 2))
            # 恒生20日动量(更代表外资对中资态度)
            if "hkHSI" in mom:
                score += max(-10, min(10, mom["hkHSI"]["m20"] * 0.5))
            signals.append("★ 全球K线动量: " + " | ".join(f"{s}:{v['m5']:+.1f}%/5d" for s, v in mom.items()))
    except Exception as _e:
        pass
    sig["global_score"] = round(score, 1)
    mood = "偏多" if score > 15 else ("偏空" if score < -15 else "中性")
    sig["global_mood"] = mood
    signals.append(f"★ 全球情绪综合分: {score} ({mood}) → 大A开盘预期")
    return {"signals": signals, "metrics": sig}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="全球市场面板 (海外数据 → 大A影响分析)")
    ap.add_argument("--out-dir", default=DEFAULT_OUT, help="输出目录 (默认 data/global)")
    args = ap.parse_args()
    OUT = args.out_dir

    items = {}
    items.update(qt_batch(QT_CODES))
    items.update(sina_batch(SINA_CODES))
    an = analyze(items)
    doc = {"ts": datetime.datetime.now().isoformat(), "date": datetime.date.today().isoformat(),
           "n": len(items), "items": items, **an}
    os.makedirs(OUT, exist_ok=True)
    with open(f"{OUT}/latest.json", "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    with open(f"{OUT}/{doc['date']}_{datetime.datetime.now():%H%M}.json", "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    print(f"全球面板 {len(items)} 品种")
    for s in an["signals"]:
        print(" ", s)
