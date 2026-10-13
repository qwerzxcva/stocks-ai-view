#!/usr/bin/env python3
"""
zsxq_collector.py — 知识星球行业情报采集 (消息面/政策面/板块面因子源)

数据源: 知识星球 MCP (https://mcp.zsxq.com/topic/mcp?api_key=<key>)
目标星球: 行业咨询智库 (group_id=51111514812824)
价值标签: #行业报告#(4630) #券商研报#(75) #金融保险#(961) #宏观经济#
          + 各行业标签 (人工智能/生物医药/能源矿产/汽车/光伏/储能...)

⚠️ 前置条件: 星球需开通 Skill 权限, 否则所有主题读取返回
   \"该星球暂未开通 Skill 权限\" (联系星主开通)

输出: data/zsxq_digest.json (与 news_digest 同结构, 可被 hot_impact/multidim 消费)

用法:
  ZSXQ_API_KEY=<key> python3 zsxq_collector.py --check     # 只验权限
  ZSXQ_API_KEY=<key> python3 zsxq_collector.py --collect   # 采集标签主题
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    from paths import AI_VIEW_DATA
except Exception:
    AI_VIEW_DATA = os.path.join(os.path.dirname(HERE), "data")

API_KEY = os.environ.get("ZSXQ_API_KEY", "")
MCP_URL = f"https://mcp.zsxq.com/topic/mcp?api_key={API_KEY}"
GROUP_ID = "51111514812824"

# 金融/股票相关标签 (hashtag_id → 名称), 来自 2026-10-13 实测标签列表
FINANCE_TAGS = {
    "48851855585158": "行业报告",
    "48851111515458": "券商研报",
    "48851828541428": "金融保险",
    "48851145144848": "经济",
    "51158584524554": "宏观经济",
    "15512582254242": "投融资",
    "51158814284844": "投资",
    "51142241218544": "资产管理",
    "51158555585454": "银行",
    "28821848522541": "人工智能",
    "88821848251512": "生物医药",
    "51158185885454": "能源矿产",
    "15548444885842": "汽车",
    "15548581228582": "光伏",
    "48851812444428": "储能",
    "15548515244542": "新能源",
    "15548448822112": "电子游戏",
    "88821815452242": "化工材料",
    "15548482481412": "航空航天",
    "48851812445418": "电力",
    "15548584884822": "节能环保（ESG）",
}

# 主题关键词 → 情绪/题材 (与 ai_screener.theme_of 思路一致)
BULL_WORDS = ["增长", "突破", "新高", "利好", "上调", "超预期", "扩产", "订单",
              "涨价", "复苏", "回暖", "盈利", "涨停", "龙头", "景气"]
BEAR_WORDS = ["下滑", "下跌", "亏损", "下调", "低于预期", "减产", "取消",
              "降价", "衰退", "萎缩", "跌停", "风险", "处罚", "退市"]
POLICY_WORDS = ["政策", "规划", "纲要", "意见", "通知", "条例", "监管",
                "补贴", "税收", "改革", "试点", "战略", "部委", "国务院"]


def mcp_call(method, params, timeout=90, retries=2):
    """调 MCP 工具, 返回解析后的 content text (SSE 格式)。"""
    body = json.dumps({"jsonrpc": "2.0", "id": int(time.time()),
                       "method": "tools/call",
                       "params": {"name": method, "arguments": params}}).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(MCP_URL, data=body, method="POST", headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
            for line in raw.split("\n"):
                if line.startswith("data: "):
                    d = json.loads(line[6:])
                    for c in (d.get("result") or {}).get("content") or []:
                        if c.get("type") == "text":
                            return c.get("text", "")
            return ""
        except Exception as e:
            if attempt == retries - 1:
                print(f"[zsxq] {method} 失败: {e}", file=sys.stderr)
                return ""
            time.sleep(2)
    return ""


def cmd_check():
    """验证 API key 与 Skill 权限。"""
    if not API_KEY:
        print("[zsxq] 未设置 ZSXQ_API_KEY 环境变量")
        print("  用法: ZSXQ_API_KEY=<key> python3 zsxq_collector.py --check")
        return 1
    txt = mcp_call("get_self_info", {})
    print(f"[zsxq] get_self_info: {txt[:200]}")
    txt2 = mcp_call("get_group_hashtags", {"group_id": GROUP_ID})
    try:
        tags = json.loads(txt2).get("hashtags") or []
        print(f"[zsxq] ✅ 标签列表可用: {len(tags)} 个标签")
    except Exception:
        print(f"[zsxq] 标签列表不可用: {txt2[:150]}")
    txt3 = mcp_call("get_hashtag_topics",
                    {"group_id": GROUP_ID, "hashtag_id": "48851855585158", "count": 3})
    if "暂未开通 Skill 权限" in txt3:
        print("\n[zsxq] ❌ 星球未开通 Skill 权限 — 主题内容读不了")
        print("       解法: 联系星主「行业咨询智库」开通 Skill 权限")
        print("       或换一个已开通权限的星球")
        return 2
    print(f"[zsxq] ✅ 主题可读: {txt3[:200]}")
    return 0


def tag_of(title):
    """简单关键词打标 (情绪 + 是否政策)。"""
    t = str(title)
    bull = sum(1 for w in BULL_WORDS if w in t)
    bear = sum(1 for w in BEAR_WORDS if w in t)
    sent = "pos" if bull > bear else ("neg" if bear > bull else "neu")
    policy = any(w in t for w in POLICY_WORDS)
    return sent, policy


def cmd_collect(per_tag=20):
    if not API_KEY:
        print("[zsxq] 未设置 ZSXQ_API_KEY")
        return 1
    items = []
    n_denied = 0
    for hid, name in FINANCE_TAGS.items():
        txt = mcp_call("get_hashtag_topics",
                       {"group_id": GROUP_ID, "hashtag_id": hid, "count": per_tag})
        if "暂未开通 Skill 权限" in txt:
            n_denied += 1
            continue
        try:
            d = json.loads(txt)
        except Exception:
            continue
        topics = d.get("topics") or d.get("topics_brief") or []
        for t in topics:
            title = (t.get("title") or "").strip()
            if not title:
                continue
            sent, policy = tag_of(title)
            items.append({
                "topic_id": str(t.get("topic_id", "")),
                "title": title[:200],
                "tag": name,
                "ts": (t.get("create_time") or "")[:19],
                "sentiment": sent,
                "policy": policy,
                "src": "zsxq",
                "digest": (t.get("digest") or "")[:300],
            })
        print(f"[zsxq] #{name}: {len(topics)} 篇")
        time.sleep(0.3)
    if n_denied and not items:
        print(f"\n[zsxq] ❌ 全部 {n_denied} 个标签都被拒 (Skill 权限未开通)")
        print("       联系星主开通后重试; 已记录的标签清单见 docs/MCP_ZSXQ.md")
        return 2
    # 按 topic_id 去重 + 时间倒序
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: str(x.get("ts", "")), reverse=True):
        k = it["topic_id"] or it["title"]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(it)
    out = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "date": datetime.now(CST).strftime("%Y-%m-%d"),
        "source": "知识星球 MCP (行业咨询智库)",
        "group_id": GROUP_ID,
        "total": len(uniq),
        "n_tags": len(FINANCE_TAGS) - n_denied,
        "n_denied": n_denied,
        "sentiment": {"pos": sum(1 for i in uniq if i["sentiment"] == "pos"),
                      "neg": sum(1 for i in uniq if i["sentiment"] == "neg"),
                      "neu": sum(1 for i in uniq if i["sentiment"] == "neu")},
        "policy_count": sum(1 for i in uniq if i["policy"]),
        "items": uniq[:200],
        "meta": {"note": "与 news_digest 同结构, 可被 hot_impact/multidim 消息面维度消费",
                 "caveat": "星球内容为第三方观点, 不构成投资建议"},
    }
    p = os.path.join(AI_VIEW_DATA, "zsxq_digest.json")
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[zsxq] ✅ {len(uniq)} 条 (政策 {out['policy_count']}, "
          f"情绪 {out['sentiment']}) → {p}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="验证 API key 与 Skill 权限")
    ap.add_argument("--collect", action="store_true", help="采集金融标签主题")
    ap.add_argument("--per-tag", type=int, default=20)
    args = ap.parse_args()
    if args.collect:
        return cmd_collect(args.per_tag)
    return cmd_check()


if __name__ == "__main__":
    sys.exit(main())
