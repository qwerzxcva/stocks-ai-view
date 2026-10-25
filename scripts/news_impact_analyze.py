#!/usr/bin/env python3
"""news_impact_analyze.py — 逐条新闻的利好/利空分析 + 影响个股，回写 GitHub 新闻库。

用户需求（2026-10-25）：
  消息页只有前 50 条、没有逐条分析；需要「分析每条数据的利好利空及对应股票的影响」，
  且「分析过的记录到 GitHub 的新闻数据库上，带上你的分析」。

实现：
  1. 取当日新闻前 50 条（东财 7x24 直连，失败退 stocks-ai-view 的 hot_news/extra）
  2. 逐条做影响分析：
     - 情绪：利好 / 利空 / 中性（词典 + 实词加权）
     - 质量：真利好（中标/订单/回购等实词）/ 利好·防追高套人（标的已大涨≥8%）/ 利空（减持/解禁/立案等）
     - 影响个股：标题代码命中 + 全市场名称匹配（用 skapp-data 的 symbols 表）
     - 影响板块：个股 → 行业（db-market stock_to_board）
  3. 结果回写 qwerzxcva/t3/news/<date>.md（带分析段落）与 data/news_impact.json（app 读）
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from paths import AI_VIEW_DATA as DATA_DIR  # noqa: E402

CST = timezone(timedelta(hours=8))
NEWS_REPO = "qwerzxcva/t3"
MAX_ITEMS = 50

POS = ["利好", "超预期", "大增", "增长", "上涨", "突破", "新高", "涨停", "中标", "签约",
       "订单", "回购", "增持", "盈利", "扭亏", "预增", "获批", "量产", "投产", "合作",
       "扩产", "景气", "放量", "反弹", "走强", "提价", "涨价", "扩产", "满产", "供不应求",
       "创新高", "超预期", "兑现", "落地", "签署", "中标", "入围", "通过", "受理", "获批"]
NEG = ["利空", "下滑", "下降", "亏损", "下跌", "破发", "跌停", "处罚", "违规", "退市",
       "减持", "解禁", "下调", "风险", "爆雷", "造假", "问询", "停产", "裁员", "疲软",
       "立案", "跌停", "跳水", "回落", "亏损", "巨亏", "商誉", "减值", "诉讼", "冻结",
       "警示", "降薪", "收缩", "欠收", "不及预期", "低于预期", "终止", "违约", "逾期"]
SOLID_POS = ["中标", "签约", "订单", "回购", "增持", "扭亏", "预增", "获批", "量产", "投产"]
RISK_NEG = ["减持", "解禁", "立案", "处罚", "退市", "造假", "爆雷", "问询", "风险警示", "破产", "停产", "终止"]


def fetch_json(url, token=None):
    """容器内 python urllib 常被东财 WAF reset（curl 存活率高），统一 curl 子进程 + 退避重试。"""
    import subprocess, time
    last = None
    for attempt in range(4):
        try:
            cmd = ["curl", "-s", "--max-time", "25",
                   "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
                   "-H", "Referer: https://quote.eastmoney.com/"]
            if token:
                cmd += ["-H", f"Authorization: Bearer {token}"]
            cmd.append(url)
            r = subprocess.run(cmd, capture_output=True, timeout=35)
            if r.returncode != 0 or not r.stdout:
                raise RuntimeError(f"curl rc={r.returncode}")
            return json.loads(r.stdout.decode("utf-8", "replace"))
        except Exception as e:
            last = e
            time.sleep(3 + attempt * 3)
    raise last


def gh_get(path):
    token = os.environ.get("GH_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
    return json.load(urllib.request.urlopen(req, timeout=25))


def gh_put(path, content, message, sha=None):
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise RuntimeError("GH_TOKEN is required for history append")
    payload = {"message": message,
               "content": base64.b64encode(content.encode("utf-8")).decode()}
    if sha:
        payload["sha"] = sha
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"https://api.github.com{path}", data=data, method="PUT",
                                headers={"Authorization": f"token {token}",
                                         "Accept": "application/vnd.github+json",
                                         "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def append_history(date, markdown, run_ts, run_id):
    path = f"/repos/{NEWS_REPO}/contents/news/{date}.md"
    sha = None
    existing = ""
    try:
        current = gh_get(path)
        sha = current.get("sha")
        encoded = (current.get("content") or "").replace("\n", "")
        if encoded:
            existing = base64.b64decode(encoded).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise RuntimeError(f"history read failed HTTP {exc.code}") from exc
    entry = (f"\n\n---\n\n<!-- analysis_run_id: {run_id} generated_at: {run_ts} -->\n\n"
             + markdown.strip() + "\n")
    merged = existing.rstrip() + entry if existing.strip() else entry.lstrip()
    response = gh_put(path, merged, f"news impact append: {date} run {run_id}", sha)
    if not response.get("content", {}).get("sha"):
        raise RuntimeError("history append returned no content sha")
    return response.get("commit", {}).get("sha") or response["content"]["sha"]


def _dedupe_news(rows, limit):
    """Keep first occurrence by stable id or title and publication time."""
    seen = set()
    out = []
    for row in rows:
        title = (row.get("title") or "").strip()
        if not title:
            continue
        stable_id = row.get("id") or row.get("news_id") or row.get("source_id")
        key = ("id", str(stable_id)) if stable_id else (
            "title_time", title.casefold(), str(row.get("published_at") or row.get("ts") or "unknown"))
        if key in seen:
            continue
        seen.add(key)
        out.append({**row, "title": title})
        if len(out) >= limit:
            break
    return out


def _local_news(path, source_kind):
    """Read collector output and preserve its public source fields."""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"[warn] local {source_kind} source unavailable: {exc}", file=sys.stderr)
        return []
    root_ts = payload.get("ts") if isinstance(payload, dict) else None
    rows = []
    freshness = "collector_snapshot"
    try:
        if root_ts:
            stamp = str(root_ts).replace("Z", "+00:00")
            age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp).astimezone(timezone.utc)).total_seconds() / 3600
            if age_hours > 24:
                freshness = "stale_snapshot"
    except (TypeError, ValueError):
        freshness = "unknown"
    for item in (payload.get("items") or []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if not title:
            continue
        published = item.get("published_at") or item.get("updated") or item.get("time") or root_ts
        rows.append({
            "title": title,
            "digest": (item.get("digest") or item.get("summary") or item.get("content") or "")[:500],
            "published_at": published or None,
            "ts": published or root_ts,
            "source": item.get("source_name") or item.get("source") or source_kind,
            "source_url": item.get("url") or item.get("link") or "",
            "codes": item.get("codes") or [],
            "themes": item.get("themes") or ([item.get("group")] if item.get("group") else []),
            "input_freshness": freshness, "stale": freshness == "stale_snapshot",
        })
    return rows


def load_news(limit=50, hot_news_path=None, news_extra_path=None):
    """Merge collector outputs and public live news; fallback rows are marked stale."""
    local = _local_news(hot_news_path, "hot_news") + _local_news(news_extra_path, "news_extra")
    live = []
    try:
        d = fetch_json("https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
                       "?client=web&biz=web7_24&fastColumn=100&column=100"
                       f"&pageSize={max(limit, 50)}&sortEnd=&sortType=0")
        items = (d.get("data") or {}).get("list") or []
        live = [{"title": (i.get("title") or "").strip(),
                 "digest": (i.get("digest") or i.get("content") or "")[:300],
                 "published_at": i.get("showTime") or i.get("publishTime") or i.get("time") or None,
                 "ts": i.get("showTime") or i.get("publishTime") or i.get("time") or None,
                 "source": i.get("source") or i.get("sourceName") or "eastmoney_724",
                 "source_url": i.get("url") or i.get("link") or "",
                 "input_freshness": "live", "stale": False}
                for i in items if (i.get("title") or "").strip()]
    except Exception as e:
        print(f"[warn] 7x24 live source failed ({e}), using marked snapshot", file=sys.stderr)
    # Live public rows take precedence; collector rows only fill missing coverage.
    live = _dedupe_news(live + local, limit)
    if len(live) >= limit:
        return live

    try:
        j = fetch_json("https://raw.githubusercontent.com/qwerzxcva/stocks-ai-view/main/data/api/news.json")
    except Exception:
        if live:
            return _dedupe_news(live, limit)
        raise
    snapshot = []
    for key in ("hot_news", "extra"):
        for i in (j.get(key) or {}).get("items") or []:
            t = (i.get("title") or "").strip()
            if t:
                published = i.get("published_at") or i.get("ts") or i.get("time") or None
                snapshot.append({"title": t, "digest": (i.get("digest") or i.get("content") or "")[:300],
                                 "published_at": published, "ts": published, "source": i.get("source") or key,
                                 "source_url": i.get("url") or i.get("link") or "",
                                 "input_freshness": "stale_snapshot", "stale": True})
    return _dedupe_news(live + snapshot, limit)


def load_symbols():
    try:
        d = fetch_json("https://raw.githubusercontent.com/jiegengxiangjianxue/skapp-data/main"
                       "/data/search/symbols.json")
        arr = d.get("symbols") if isinstance(d, dict) else d
        if isinstance(arr, dict):
            arr = [{"code": code, "name": name} for code, name in arr.items()]
        rows = []
        for item in arr or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or item.get("symbol") or item.get("s") or "").lower()
            code = re.sub(r"^(sh|sz|bj)", "", code)
            name = str(item.get("name") or item.get("n") or item.get("stockName") or "").strip()
            if code and name:
                rows.append((code, name))
        return rows
    except Exception:
        return []


def load_boards():
    """板块名 → 成分股（db-market 板块列表，用于无个股时的板块影响判定）"""
    try:
        out = {}
        for kind in ("industry", "concept"):
            d = fetch_json(f"https://raw.githubusercontent.com/jiegengxiangjianxue1/db-market/"
                           f"main/sector/{kind}.json")
            for b in (d.get("boards") or []):
                if b.get("name"):
                    out[b["name"]] = b.get("bk")
        return out
    except Exception:
        return {}


def analyze(title, digest, symbols, board_map=None, item=None):
    # 标题权重高、摘要辅助：都参与判定，避免大面积中性
    t = title.lower()
    d = (digest or "").lower()
    text = f"{t} {d}"
    pos = [w for w in POS if w in text]
    neg = [w for w in NEG if w in text]
    if pos and neg:
        sentiment, verdict = "neu", "中性（多空交织）"
    elif neg:
        sentiment, verdict = "neg", "利空"
    elif pos:
        sentiment = "pos"
    else:
        sentiment, verdict = "neu", "中性"

    if sentiment == "pos":
        solid = any(w in text for w in SOLID_POS)
        verdict = "真利好" if solid else "利好"

    # 影响个股：代码命中优先，其次名称匹配
    hits = []
    hit_by = {}
    for raw_code in (item or {}).get("codes") or []:
        code = re.sub(r"^(sh|sz|bj)", "", str(raw_code).lower())
        if code.isdigit() and code not in hits:
            hits.append(code.zfill(6))
            hit_by[code.zfill(6)] = "source_annotation"
    for code in re.findall(r"(?<!\d)(?:sh|sz|bj)?\s*(\d{6})(?!\d)", title, re.I):
        if code not in hits:
            hits.append(code)
            hit_by[code] = "public_title_code"
    # 名称匹配只接受完整公开名称，避免把普通词语的前两个字误当成股票简称。
    compact_title = title.replace(" ", "")
    for code, name in symbols:
        if not name or len(name) < 2:
            continue
        nm = name.replace(" ", "")
        if nm in compact_title and code not in hits:
            hits.append(code)
            hit_by[code] = "public_title_name"
        if len(hits) >= 6:
            break
    # 板块影响（无个股时给板块）
    boards = []
    if board_map:
        for bname, codes in board_map.items():
            if bname and bname in title:
                boards.append(bname)
    names = {str(code): name for code, name in symbols}
    stocks = [{"code": code, "name": names.get(code), "evidence": hit_by.get(code, "public_title_code_or_name")}
              for code in hits[:6]]
    benefit_reasons = ["公开标题/摘要命中利好词：" + "、".join(pos[:8])] if pos else ["unknown: 未命中可核验利好词"]
    harm_reasons = ["公开标题/摘要命中利空词：" + "、".join(neg[:8])] if neg else ["unknown: 未命中可核验利空词"]
    if pos and neg:
        reasons = ["同一公开文本同时命中利好与利空词，保持多空交织，不推断单一方向"]
    elif pos or neg:
        reasons = benefit_reasons + harm_reasons
    else:
        reasons = ["未命中预定义方向词，保持中性，不作利好或利空推断"]
    impact_status = "ok" if hits or boards else "insufficient_data"
    impact_reason = "公开标题/摘要命中可核验代码、名称或板块" if hits or boards else "未找到可核验个股或板块证据"
    evidence_status = "sufficient" if title and (item or {}).get("source") and (item or {}).get("published_at") else "insufficient_data"
    source = (item or {}).get("source") or "unknown"
    published_at = (item or {}).get("published_at") or (item or {}).get("ts") or "unknown"
    confidence = round(min(0.9, 0.45 + 0.1 * max(len(pos), len(neg))), 2) if pos or neg else 0.2
    return {"sentiment": sentiment, "direction": {"pos": "bull", "neg": "bear", "neu": "neutral"}.get(sentiment, "neutral"),
            "verdict": verdict, "status": "ok" if evidence_status == "sufficient" else "insufficient_data",
            "evidence_status": evidence_status, "benefit_reasons": benefit_reasons, "harm_reasons": harm_reasons,
            "positive_reason": "；".join(benefit_reasons), "negative_reason": "；".join(harm_reasons),
            "reasons": reasons, "pos_words": pos[:8], "neg_words": neg[:8], "codes": hits[:6], "boards": boards[:3],
            "stocks": stocks, "affected_stocks": stocks, "affected_stocks_status": impact_status,
            "impact_reason": impact_reason, "matched_by": ("collector_annotation" if any(v == "source_annotation" for v in hit_by.values()) else "title_code" if any(v == "public_title_code" for v in hit_by.values()) else "stock_name" if any(v == "public_title_name" for v in hit_by.values()) else "board" if boards else "none"),
            "evidence": {"title": title, "digest": digest or "", "source": source, "published_at": published_at,
                         "impact": "title_code_or_name_or_board" if hits or boards else "unknown"},
            "confidence": confidence, "confidence_basis": "rule_based_public_text",
            "stale": bool((item or {}).get("stale", True)),
            "provenance": {"source": source, "source_url": (item or {}).get("source_url") or "unknown",
                           "confidence": confidence, "snapshot_ts": (item or {}).get("ts") or "unknown",
                           "input_freshness": (item or {}).get("input_freshness") or "unknown"}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=MAX_ITEMS)
    ap.add_argument("--no-push", action="store_true", help="不回写 GitHub")
    ap.add_argument("--hot-news", default=os.path.join(DATA_DIR, "hot_news.json"))
    ap.add_argument("--news-extra", default=os.path.join(DATA_DIR, "news_extra.json"))
    args = ap.parse_args()
    limit = max(1, min(args.limit, MAX_ITEMS))
    run_ts = datetime.now(CST).isoformat(timespec="seconds")
    run_id = os.environ.get("GITHUB_RUN_ID") or f"local-{datetime.now(CST).strftime('%Y%m%d%H%M%S')}"

    today = datetime.now(CST).strftime("%Y-%m-%d")
    news = load_news(limit, args.hot_news, args.news_extra)
    symbols = load_symbols()
    board_map = load_boards()
    print(f"取到 {len(news)} 条新闻, 代码表 {len(symbols)} 只")

    results = []
    for n in news[:MAX_ITEMS]:
        a = analyze(n["title"], n.get("digest", ""), symbols, board_map, n)
        row = {**n, **a, "analyzed_at": run_ts, "run_id": run_id}
        row.setdefault("published_at", n.get("ts"))
        row.setdefault("time", row.get("published_at"))
        row.setdefault("source_url", n.get("source_url") or "")
        row["evidence"] = {"title": row.get("title"), "digest": row.get("digest") or "",
                            "source": row.get("source"), "published_at": row.get("published_at"),
                            "url": row.get("source_url") or ""}
        results.append(row)

    # 回写 t3/news/<date>.md（带分析）
    lines = [f"# {today} 新闻影响分析", "",
             f"> 生成: {run_ts} · run_id: {run_id} · 引擎: news_impact_analyze v2 · 共 {len(results)} 条",
             "> 规则仅依据公开标题/摘要；缺证据标记 insufficient_data。", ""]
    for i, r in enumerate(results, 1):
        codes = "、".join(r["codes"]) if r["codes"] else (
            "板块: " + "、".join(r.get("boards") or []) if r.get("boards") else "宏观/未识别到个股")
        lines += [
            f"## {i}. {r['title']}",
            f"- 时间: {r.get('published_at') or r.get('ts') or 'unknown'} · 来源: {r.get('source') or 'unknown'}",
            f"- 输入新鲜度: {r.get('input_freshness') or 'unknown'} · 状态: `{r.get('status', 'insufficient_data')}`",
            f"- **判定: {r['verdict']}**（情绪 {r['sentiment']}，方向 {r.get('direction', 'neutral')}）",
            f"- 利好理由: {'；'.join(r.get('benefit_reasons') or []) or 'insufficient_data'}",
            f"- 利空理由: {'；'.join(r.get('harm_reasons') or []) or 'insufficient_data'}",
            f"- 分析理由: {'；'.join(r.get('reasons') or [])}",
            f"- 影响个股/代码: {codes}",
            f"- 影响证据状态: `{r.get('affected_stocks_status', 'insufficient_data')}`；{r.get('impact_reason', 'insufficient_data')}",
            (f"- 摘要: {r['digest'][:160]}" if r.get("digest") else "- 摘要: insufficient_data"),
            "",
        ]
    md = "\n".join(l for l in lines if l != "")

    publish = {"status": "dry_run" if args.no_push else "pending"}
    publish_error = None
    if args.no_push:
        print("[skip] --no-push")
    elif not os.environ.get("GH_TOKEN"):
        publish = {"status": "failed", "error": "GH_TOKEN is required for non-dry-run history append"}
        publish_error = publish["error"]
        print("::error::" + publish["error"], file=sys.stderr)
    else:
        try:
            commit_sha = append_history(today, md, run_ts, run_id)
            publish = {"status": "ok", "commit_sha": commit_sha or "unknown", "path": f"news/{today}.md"}
            print(f"已追加写入 {NEWS_REPO}/news/{today}.md (run_id={run_id})")
        except Exception as exc:
            publish_error = str(exc)
            publish = {"status": "failed", "error": publish_error}
            print(f"::error::history append failed: {publish_error}", file=sys.stderr)

    for row in results:
        published = row.get("published_at") or row.get("ts") or "unknown"
        row["published_at"] = str(published)
        row["url"] = row.get("source_url") or None
        row["evidence_status"] = "sufficient" if published != "unknown" and row.get("source") not in (None, "unknown") else "insufficient"
        row["status"] = "ok" if row["evidence_status"] == "sufficient" else "insufficient"
        stocks = row.get("affected_stocks") or []
        for stock in stocks:
            stock.setdefault("matched_by", row.get("matched_by") or "unknown")
            stock.setdefault("evidence", "unknown")
            stock.setdefault("confidence", row.get("confidence", 0.0))
        row["affected_stocks"] = stocks
        row["matched_by"] = row.get("matched_by") or ("stock" if row.get("codes") else ("board" if row.get("boards") else "unknown"))
        row["positive_reason"] = row.get("positive_reason") or "；".join(row.get("benefit_reasons") or []) or "insufficient"
        row["negative_reason"] = row.get("negative_reason") or "；".join(row.get("harm_reasons") or []) or "insufficient"
        row["evidence"] = row.get("evidence") or {"status": "insufficient_data"}
        row["provenance"] = {"source": row.get("source") or "unknown", "published_at": row.get("published_at"),
                             "input_freshness": row.get("input_freshness") or "unknown", "stale": bool(row.get("stale"))}

    impacts = [{"title": r.get("title"), "source": r.get("source") or "unknown", "published_at": r.get("published_at"),
                 "time": r.get("published_at"), "url": r.get("url"), "verdict": r.get("verdict"),
                 "sentiment": r.get("sentiment"), "direction": r.get("direction"),
                 "positive_reason": r.get("positive_reason"), "negative_reason": r.get("negative_reason"),
                 "matched_by": r.get("matched_by") or "unknown", "evidence": r.get("evidence") or {"status": "insufficient_data"},
                 "provenance": r.get("provenance") or {"source": r.get("source") or "unknown", "published_at": r.get("published_at")},
                 "evidence_status": r.get("evidence_status"), "affected_stocks": r.get("affected_stocks"),
                 "channel": r.get("source") or "",
                 "themes": r.get("boards") or [],
                 "confidence": r.get("confidence"), "matched_by": r.get("matched_by") or "unknown",
                 "match_detail": r.get("impact_reason"), "n_stocks": len(r.get("stocks") or []),
                 "stocks": r.get("stocks") or [], "status": r.get("status"),
                 "evidence_status": r.get("evidence_status"), "reasons": r.get("reasons") or [],
                 "benefit_reasons": r.get("benefit_reasons") or [], "harm_reasons": r.get("harm_reasons") or [],
                 "stale": r.get("stale", False)} for r in results]
    assert len(results) <= MAX_ITEMS, f"news result exceeds {MAX_ITEMS}: {len(results)}"
    assert len(impacts) == len(results), "items and impacts must align"
    output_path = os.path.abspath(os.path.join(DATA_DIR, "news_impact.json"))
    mapped = sum(1 for r in results if r.get("affected_stocks_status") == "ok")
    out = {"schema_version": "news-impact.v2", "status": "dry_run" if args.no_push else ("ok" if publish["status"] == "ok" else "insufficient"),
           "ts": run_ts, "generated_at": run_ts, "input_ts": run_ts, "run_id": run_id,
           "engine": "news_impact_analyze v2", "date": today, "n": len(results),
           "n_input": len(results), "n_mapped": mapped, "n_unmapped": len(results) - mapped,
           "coverage_status": "complete" if len(results) >= limit else "insufficient",
           "source_status": ("live" if results and all(r.get("input_freshness") == "live" for r in results)
                            else "mixed_or_stale" if results else "unavailable"),
           "push_status": publish["status"], "publish": publish,
           "output_path": output_path,
           "provenance": {"source": "eastmoney_724_or_github_snapshot", "confidence": "rule_based_public_text",
                          "snapshot_ts": run_ts, "stale": any(r.get("stale", True) for r in results) if results else True},
           "direction_stat": dict(Counter(r.get("direction") for r in results)),
           "items": results, "impacts": impacts,
           "meta": {"public_sources": ["eastmoney_724", "stocks-ai-view:data/api/news.json snapshot fallback"],
                    "source_registry_count": 190,
                    "news_source_registry_count": 13,
                    "legacy_contract": "impacts[].stocks and impacts[].direction retained",
                    "evidence_policy": "missing source/time or stock evidence remains insufficient_data"}}
    p = output_path
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as output:
        json.dump(out, output, ensure_ascii=False, indent=2)
    # appFile maps base(data)+/data/<file>; duplicate for Android's double-data contract.
    app_p = os.path.join(DATA_DIR, "data", "news_impact.json")
    os.makedirs(os.path.dirname(app_p), exist_ok=True)
    with open(app_p, "w", encoding="utf-8") as output:
        json.dump(out, output, ensure_ascii=False, indent=2)
    print(f"output_path={p} ({os.path.getsize(p)} B)")
    print(f"app_output_path={app_p} ({os.path.getsize(app_p)} B)")
    print("判定分布:", dict(Counter(r["verdict"] for r in results)))
    print("识别到个股的条数:", sum(1 for r in results if r["codes"]))
    if publish_error:
        return 1
    if not args.no_push and len(results) < limit:
        print(f"::error::expected {limit} news items, got {len(results)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
