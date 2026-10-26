#!/usr/bin/env python3
"""fetchers_batch3.py — 注册表标注可用但此前没有执行器的源（2026-10-26 逐个实测）。

实测结论：
  财联社   cls_collect.py 已有签名逆向实现（sign=md5(sha1(含 category 的排序串))），
           直接复用 → cls_telegraph / cls_telegraph_v1 可接；此前判“需签名”是误判。
  ths_kzz_all      https://data.10jqka.com.cn/ipo/kzz/   JSON，969 只
  jisilu           https://www.jisilu.cn/data/cbnew/cb_list_new/  JSON rows[].cell
  tencent_board    proxy.finance.qq.com .../getBoardRankList  JSON rank_list
  sina_moneyflow   vip.stock.finance.sina.com.cn MoneyFlow.ssi_ssfx_flzjtj  四档资金
  sina_hq_node     Market_Center.getHQNodeStockCount  裸字符串（如 "5576"）
  wscn_lives       api-one.wallstcn.com apiv1/content/lives  JSON items

返回口径与 collect_all_sources 一致: (records, meta)，record 带 _key。
"""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fetchers_f10 import UA, target_codes  # noqa

UA_JSON = dict(UA)
UA_JSON["Referer"] = "https://finance.sina.com.cn/"


def _get(url, timeout=20, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or UA_JSON),
                                timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ── 财联社（复用已验证的签名实现）──────────────────────
def _cls_records(category):
    import cls_collect
    rows = cls_collect.fetch(category=category, rn=50)
    recs = []
    for row in rows:
        rid = row.get("id")
        recs.append({
            "_key": "cls-%s-%s" % (category or "all", rid),
            "id": rid,
            "title": (row.get("title") or row.get("brief") or "")[:120],
            "content": (row.get("content") or "")[:400],
            "level": row.get("level"),
            "ctime": row.get("ctime"),
            "category": category or "all",
            "stocks": [s.get("name") for s in (row.get("stock_list") or []) if s.get("name")],
            "src": "cls_telegraph",
        })
    return recs


def f_cls_telegraph(ctx=None):
    recs = _cls_records("")
    return recs, {"source": "cls_telegraph", "n": len(recs)}


def f_cls_telegraph_v1(ctx=None):
    recs = _cls_records("announcement")
    return recs, {"source": "cls_telegraph_v1", "n": len(recs), "category": "announcement"}


# ── 同花顺可转债列表 ─────────────────────────────────
def f_ths_kzz_all(ctx=None):
    data = json.loads(_get("https://data.10jqka.com.cn/ipo/kzz/",
                           headers={**UA, "Referer": "https://data.10jqka.com.cn/"}))
    rows = data.get("list") or []
    recs = []
    for row in rows:
        code = row.get("bond_code") or row.get("code")
        recs.append({
            "_key": "ths-kzz-%s" % code,
            "bond_code": code, "name": row.get("bond_name"),
            "sub_date": row.get("sub_date"), "stock_code": row.get("stock_code"),
            "src": "ths_kzz_all",
        })
    return recs, {"source": "ths_kzz_all", "n": len(recs)}


# ── 集思录可转债实时 ────────────────────────────────
def f_jisilu_cb_realtime(ctx=None):
    data = json.loads(_get("https://www.jisilu.cn/data/cbnew/cb_list_new/?___jsl=LST___t=1",
                           headers={**UA, "Referer": "https://www.jisilu.cn/data/cbnew/"}))
    rows = data.get("rows") or []
    recs = []
    for row in rows:
        cell = row.get("cell") or {}
        code = cell.get("bond_id")
        recs.append({
            "_key": "jisilu-%s" % code,
            "bond_code": code, "name": cell.get("bond_nm"),
            "price": cell.get("price"), "premium": cell.get("premium_rt"),
            "ytm": cell.get("ytm_rt"), "rating": cell.get("rating_cd"),
            "src": "jisilu_cb_realtime",
        })
    return recs, {"source": "jisilu_cb_realtime", "n": len(recs)}


# ── 腾讯板块榜 ─────────────────────────────────────
def f_tencent_board_rank(ctx=None):
    url = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
           "?_appver=11.17.0&board_code=aStock&sort_type=price&direct=down&offset=0&count=60")
    data = json.loads(_get(url, headers={**UA, "Referer": "https://gu.qq.com/"}))
    rows = ((data.get("data") or {}).get("rank_list")) or []
    recs = []
    for row in rows:
        code = row.get("code")
        recs.append({
            "_key": "tencent-board-%s" % code,
            "code": code, "name": row.get("name"),
            "chg_pct": row.get("zdf"), "turnover": row.get("hsl"),
            "src": "tencent_board_rank",
        })
    return recs, {"source": "tencent_board_rank", "n": len(recs)}


# ── 新浪四档资金流 ───────────────────────────────────
def f_sina_moneyflow_lscjfb(ctx=None):
    recs, err = [], []
    for code in target_codes(8):
        try:
            prefix = "sh" if code[:3] in ("600", "601", "603", "605", "688") else "sz"
            d = json.loads(_get("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
                                "json_v2.php/MoneyFlow.ssi_ssfx_flzjtj?daima=%s%s" % (prefix, code)))
            if not isinstance(d, dict) or not d.get("r0_in"):
                err.append("%s:no_data" % code)
                time.sleep(0.3)
                continue
            recs.append({
                "_key": "sina-mf-%s" % code, "code": code,
                "super_in": d.get("r0_in"), "super_out": d.get("r0_out"),
                "big_in": d.get("r1_in"), "big_out": d.get("r1_out"),
                "mid_in": d.get("r2_in"), "small_in": d.get("r3_in"),
                "src": "sina_moneyflow_lscjfb",
            })
        except Exception as e:
            err.append("%s:%s" % (code, type(e).__name__))
        time.sleep(0.3)
    return recs, {"source": "sina_moneyflow_lscjfb", "errors": err[:3]}


# ── 新浪节点股票数（裸字符串）────────────────────────
def f_sina_hq_node_count(ctx=None):
    text = _get("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                "Market_Center.getHQNodeStockCount?node=hs_a")
    count = text.strip().strip('"')
    return ([{"_key": "sina-node-count-hs_a", "node": "hs_a", "count": count,
              "src": "sina_hq_node_count"}],
            {"source": "sina_hq_node_count", "count": count})


# ── 华尔街见闻快讯 ──────────────────────────────────
def f_wscn_lives(ctx=None):
    data = json.loads(_get("https://api-one.wallstcn.com/apiv1/content/lives"
                           "?channel=a-stock-channel&limit=30",
                           headers={**UA, "Referer": "https://wallstreetcn.com/"}))
    items = ((data.get("data") or {}).get("items")) or []
    recs = []
    for it in items:
        recs.append({
            "_key": "wscn-%s" % it.get("id"),
            "id": it.get("id"),
            "title": (it.get("title") or "")[:120],
            "content": (it.get("content_text") or it.get("content") or "")[:400],
            "ts": it.get("display_time"),
            "src": "wscn_lives",
        })
    return recs, {"source": "wscn_lives", "n": len(recs)}


if __name__ == "__main__":
    for n in ("f_cls_telegraph", "f_cls_telegraph_v1", "f_ths_kzz_all",
              "f_jisilu_cb_realtime", "f_tencent_board_rank",
              "f_sina_moneyflow_lscjfb", "f_sina_hq_node_count", "f_wscn_lives"):
        try:
            recs, meta = globals()[n]()
            print(n, "records=", len(recs), json.dumps(meta, ensure_ascii=False)[:120])
        except Exception as e:
            print(n, "FAIL", type(e).__name__, str(e)[:90])
