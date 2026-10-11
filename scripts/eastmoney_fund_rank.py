#!/usr/bin/env python3
"""
eastmoney_fund_rank: 东财基金排行接口 (纯 HTTP, 免登录, 带日期区间)
接口: https://fund.eastmoney.com/data/rankhandler.aspx
参数: op=ph&dt=kf&ft=all&sc=1nzf&st=desc&sd=起始日&ed=截止日
字段含义 (CSV): [fundcode,fundname,fcode,,,,近1Y收益率,,,,总规模,...成立日期,,,评级,...]
产出: data/{date}_fundrank.json + data/fundrank/latest.json
用法: python3 eastmoney_fund_rank.py [--days 90] [--top 50] [--out /path/latest.json]
"""
import argparse, datetime, json, re, sys, urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120",
      "Referer": "https://fund.eastmoney.com/"}

def fetch(days=90, top=50, ft="hh"):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    sd = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    ed = now.strftime("%Y-%m-%d")
    url = (f"https://fund.eastmoney.com/data/rankhandler.aspx"
           f"?op=ph&dt=kf&ft={ft}&rs=&gs=0&sc=1nzf&st=desc&sd={sd}&ed={ed}"
           f"&qdii=&tabType=new&pi=1&pn={top}&dx=")
    body = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15).read().decode("utf-8", "replace")
    # 响应是 JS: var rankData = {datas:[...],allRecords:N,...};
    # key 无引号 (datas/allRecords 等), 先把裸 key 加引号再 JSON 解析
    idx = body.find("var rankData = ")
    if idx < 0:
        return []
    s = body[idx + 15:]
    # JS 对象 key 无引号 (datas/allRecords...), 但 datas 的数组内容是标准 JSON;
    # 直接提取 [ ... ] 数组再解析, 避免全局改写引号破坏字符串内容
    m = re.search(r"datas:\s*(\[.*?\])", s, re.DOTALL)
    if not m:
        return []
    rows = json.loads(m.group(1))
    out = []
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for raw in rows:
        if not raw or len(raw) < 4:
            continue
        p = raw.split(",")
        code = p[0].strip()
        if not code or code == "---":
            continue
        # 东财排行 CSV: 0 code,1 name,2 fcode, 然后 [nav_date, 净值, 累计净值, 日涨跌, 1周, 1月, 3月, 6月, 1年, 2年, 3年, 5年, ...]
        # 但各类型基金前置空字段数不同 → 用首个「日期」字段做锚点定位
        anchor = -1
        for i in range(3, min(9, len(p))):
            if date_re.match(p[i].strip()):
                anchor = i
                break
        if anchor < 0:
            continue  # 找不到日期锚点 (非基金类型), 跳过

        def f(off, cap=None):
            """取锚点后第 off 列 (0=日期本身): 1=净值 2=累计 3=日涨跌 4=1周 5=1月 6=3月 7=6月 8=1年 9=2年 10=3年 11=5年 12=今年来 13=成立来"""
            j = anchor + off
            if j >= len(p):
                return 0.0
            try:
                v = p[j].strip()
                return float(v) if v not in ("", "---") else 0.0
            except ValueError:
                return 0.0

        # 数值合理性: 收益率字段若 >1000 说明是「累计净值/规模」而非收益率 (债券基金错列), 置 0
        def guarded(off, cap=1000.0):
            v = f(off)
            return v if abs(v) < cap else 0.0

        out.append({
            "code": code,
            "name": p[1].strip(),
            "short_name": p[2].strip(),
            "nav": f(1),
            "daily_chg_pct": guarded(3, cap=100.0),
            "ret_1w": guarded(4),
            "ret_1m": guarded(5),
            "ret_3m": guarded(6),
            "ret_6m": guarded(7),
            "ret_1y": guarded(8),
            "ret_2y": guarded(9),
            "ret_3y": guarded(10),
            "ret_5y": guarded(11),
        })
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--ft", default="hh", help="基金类型: all/zq/hh/jj(股票型)/zqjj(债券) 等")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    rows = fetch(days=args.days, top=args.top, ft=args.ft)
    report = {
        "ts": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "source": "eastmoney-fundrank",
        "algo": "eastmoney-rank-v1",
        "fund_type": args.ft,
        "rows": rows,
        "meta": {"zero_cred": True,
                  "note": "近1年收益排行 = 资金抱团方向代理指标; ft=hh 混合基金 KOL 多仓"},
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"written {args.out} ({len(rows)} funds)", flush=True)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2)[:800])
    if rows:
        print(f"top3: {[(r['code'], r['name'][:10], r['ret_1y']) for r in rows[:3]]}", flush=True)


if __name__ == "__main__":
    main()
