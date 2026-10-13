#!/usr/bin/env python3
"""Build data/enrich.json — 独立文件, 不被远端 local realtime sync 覆盖。

背景: 远端每10分钟推送 local realtime sync 覆盖 data/latest.json,
     手动合并永远追不上 (07:34 合并 → 07:40 又被冲掉)。
方案: 把 强势股/龙虎榜/大宗交易 三个块写成独立 JSON,
     前端 render() 优先读 enrich.json, 缺失才回退 latest.json。
     远端 sync 只动 latest.json, 不碰 enrich.json。
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect_market_extras import collect_all
from paths import AI_VIEW_DATA

OUT = os.path.join(AI_VIEW_DATA, "enrich.json")
now_iso = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")

extras = collect_all()
extras.pop("_pools_detail", None)

payload = {
    "ts": now_iso,
    "ths_strong": extras.get("ths_strong", []),
    "lhb": extras.get("lhb", []),
    "blocktrade": extras.get("blocktrade", []),
    "enrich_meta": extras.get("enrich_meta", {}),
    "meta": {
        "why_independent": "远端 local realtime sync 每10分钟覆盖 latest.json, "
                           "本文件独立存放以免被覆盖",
        "source": "同花顺强势股 + 腾讯行情 + 东财 datacenter (龙虎榜/大宗交易)",
        "fields": {
            "ths_strong": "code/name/reason 来自同花顺; price/chg_pct/turnover/"
                          "mktcap/pe/pb/score/rank 由腾讯行情补全并计算",
            "score": "综合分 = 动量30 + 换手20 + 市值15 + 估值15 + 题材20 (满分100)",
            "lhb": "东财 RPT_DAILYBILLBOARD_DETAILS, 含席位解读与历史成功率",
            "blocktrade": "东财 RPT_DATA_BLOCKTRADE, 溢价率=(成交价-收盘价)/收盘价",
        },
        "date_fallback": "龙虎榜/大宗交易收盘后才出, 当日无数据自动回退最近交易日",
        "freq": "rebuild-pools 工作流每次运行刷新 (交易日 4 次/天)",
    },
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(payload, f, ensure_ascii=False)

print(f"[out] {OUT}")
print(f"  ths_strong {len(payload['ths_strong'])} 只 "
      f"(有价格 {sum(1 for x in payload['ths_strong'] if x.get('price') is not None)})")
print(f"  lhb {len(payload['lhb'])} 条 | blocktrade {len(payload['blocktrade'])} 笔")
print(f"  enrich_meta {json.dumps(payload['enrich_meta'], ensure_ascii=False)}")
