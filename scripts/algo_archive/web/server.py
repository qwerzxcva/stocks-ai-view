#!/usr/bin/env python3
"""
股票仪表盘 server (2026-10-12 UI重构: 多Tab专业布局, 参考主流选股站)
  GET /        仪表盘(6 Tab: 总览/周期池/板块池/低位池/AI推荐/24h热点)
  GET /api/*   JSON数据(pools/hot/news/analysis)
端口 8600
"""
import json, glob, os, datetime, duckdb, urllib.request, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
from urllib.parse import parse_qs, quote as _urlquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ---- 实时价覆盖层: qt批量拉, 60s缓存 ----
_RT_CACHE = {"ts": 0, "data": {}}
def rt_quotes(symbols):
    import time as _t, urllib.request
    now = _t.time()
    if now - _RT_CACHE["ts"] < 60 and _RT_CACHE["data"]:
        return _RT_CACHE["data"]
    syms = list(set(symbols))[:60]
    if not syms:
        return {}
    try:
        body = urllib.request.urlopen(urllib.request.Request(
            "https://qt.gtimg.cn/q=" + ",".join(syms),
            headers={"Referer": "https://gu.qq.com"}), timeout=5).read().decode("gbk", "ignore")
        out = {}
        for line in body.strip().split(";"):
            p = line.split("~")
            if len(p) > 49:
                c6 = p[2]
                pre = "sh" if c6.startswith("6") else "sz"
                try:
                    out[pre + c6] = {"price": float(p[3]), "pct": float(p[32]), "rt_time": p[30]}
                except (ValueError, IndexError):
                    pass
        _RT_CACHE["ts"] = now
        _RT_CACHE["data"] = out
        return out
    except Exception:
        return _RT_CACHE["data"]

def apply_rt(doc):
    """把实时价覆盖进 pools/ai_picks 文档的每个记录"""
    syms = []
    for key in ("cycle", "sector", "lowpos"):
        for sub in doc.get(key, {}).values() if isinstance(doc.get(key), dict) else []:
            pass
    def collect(o):
        if isinstance(o, dict):
            if "symbol" in o and isinstance(o.get("symbol"), str) and o["symbol"][:2] in ("sh", "sz"):
                syms.append(o["symbol"])
            for v in o.values(): collect(v)
        elif isinstance(o, list):
            for v in o: collect(v)
    collect(doc)
    rt = rt_quotes(syms)
    def patch(o):
        if isinstance(o, dict):
            if o.get("symbol") in rt:
                o["price_rt"] = rt[o["symbol"]]["price"]
                o["pct_rt"] = rt[o["symbol"]]["pct"]
                o["rt_time"] = rt[o["symbol"]]["rt_time"]
            for v in o.values(): patch(v)
        elif isinstance(o, list):
            for v in o: patch(v)
    patch(doc)
    doc["rt_asof"] = rt.get(next(iter(rt)), {}).get("rt_time") if rt else None
    return doc
# ---- END 实时层 ----

BASE = "/home/agentuser/stockdb"


def load_pools():
    try: return json.load(open(f"{BASE}/data/pools.json"))
    except Exception: return {}

def load_hot():
    files = sorted(glob.glob(f"{BASE}/data/hot/*.json"))
    if not files: return {}
    return json.load(open(files[-1]))

def load_news():
    files = sorted(f for f in glob.glob(f"{BASE}/data/news_map/*.json") if "_stocks" not in f and "llm" not in f)
    if not files: return {}
    return json.load(open(files[-1]))

class H(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json; charset=utf-8"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
    def do_GET(self):
        m = self.path.split("?")
        p = m[0]; qs = m[1] if len(m) > 1 else ""
        q = parse_qs(qs)
        if p == "/api/moneyflow":
            try: return self._send(json.dumps(json.load(open(f"{BASE}/data/moneyflow/latest.json")), ensure_ascii=False).encode())
            except Exception: return self._send(b"{}")
        if p == "/api/fund":
            try: return self._send(json.dumps(json.load(open(f"{BASE}/data/fund/latest.json")), ensure_ascii=False).encode())
            except Exception: return self._send(b"{}")
        if p == "/api/fundnav":
            # 个基净值: /api/fundnav?code=001594 → 实时估值+东财历史+蛋卷(直接import fund_scan)
            code = (q.get("code", [""])[0] or "").strip()
            if not code: return self._send(b"{}")
            out = {}
            try:
                if "_fund_scan" not in globals():
                    globals()["_fund_scan"] = __import__("fund_scan", fromlist=["em_nav_history", "danjuan_detail", "ttjj_valuation"])
                fs = globals()["_fund_scan"]
                out = {"nav": fs.em_nav_history(code, 60), "dj": fs.danjuan_detail(code), "rt": fs.ttjj_valuation([code])}
            except Exception as e:
                out = {"error": str(e)[:120]}
            return self._send(json.dumps(out, ensure_ascii=False).encode())
        if p == "/api/search":
            # 股票搜索: 腾讯smartbox建议接口(中文名/拼音首字母/代码全支持) → 再qt补实时价
            term = (q.get("q", [""])[0] or "").strip()
            if not term: return self._send(b"[]")
            results = []
            try:
                u = f"https://smartbox.gtimg.cn/s3/?v=2&q={_urlquote(term)}&t=all"
                txt = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=6).read().decode("gbk", "ignore")
                body = txt.split('="', 1)[1].rstrip('"\n') if '="' in txt else ""
                import codecs as _cd
                for item in body.split("^"):
                    f = item.split("~")
                    if len(f) >= 5 and f[4] in ("GP-A", "GP", "GP-A-KCB", "GP-A-CYB") and f[1][:2] in ("60", "00"):
                        name = _cd.decode(f[2], "unicode_escape").encode("latin1", "ignore").decode("gbk", "ignore")
                        results.append({"code": f[1], "name": name})
            except Exception:
                pass
            if results:
                full = ",".join(("sh" if r["code"].startswith(("60", "68")) else "sz") + r["code"] for r in results[:20])
                try:
                    txt = urllib.request.urlopen(urllib.request.Request(f"https://qt.gtimg.cn/q={full}", headers={"User-Agent": "Mozilla/5.0"}), timeout=6).read().decode("gbk", "ignore")
                    for line, r0 in zip(txt.split(";"), results):
                        line = line.strip()
                        if "~" not in line: continue
                        f = line.split("~")
                        r0["price"], r0["pct"] = f[3], f[32]
                        if len(f) > 2 and f[1]: r0["name"] = f[1]
                except Exception: pass
            return self._send(json.dumps(results[:20], ensure_ascii=False).encode())
        if p == "/api/stock":
            # 个股详情: qt实时 + 日K(duckdb) + 5分钟K(duckdb minute)
            code = (q.get("code", [""])[0] or "").strip()  # 6位
            if not code: return self._send(b"{}")
            mkt = "sh" if code.startswith(("60", "68")) else "sz"
            sym = mkt + code
            out = {"code": code, "sym": sym}
            try:
                txt = urllib.request.urlopen(urllib.request.Request(
                    f"https://qt.gtimg.cn/q={sym}", headers={"User-Agent": "Mozilla/5.0"}), timeout=5).read().decode("gbk", "ignore")
                f = txt.split("~")
                out.update({"name": f[1], "price": f[3], "pct": f[32], "open": f[5], "high": f[33],
                            "low": f[34], "volume": f[36], "amount": f[37], "turnover": f[38],
                            "pe": f[39], "high52": f[41] if len(f) > 41 else "", "rt_time": f[30]})
            except Exception as e:
                out["rt_error"] = str(e)
            try:
                con = duckdb.connect(f"{BASE}/stock.duckdb", read_only=True)
                dk = con.execute("SELECT date, open, close, high, low, volume FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 120", [sym]).fetchall()
                mk = con.execute("SELECT hhmm, price, volume FROM minute WHERE symbol=? AND date=(SELECT MAX(date) FROM minute) ORDER BY hhmm", [sym]).fetchall()
                con.close()
                out["daily_k"] = [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in reversed(dk)]
                out["minute_k"] = [[r[0], r[1], r[2]] for r in mk]
                out["minute_date"] = mk[-1][0] if mk else ""
            except Exception as e:
                out["k_error"] = str(e)
            return self._send(json.dumps(out, ensure_ascii=False).encode())
        if p == "/api/orchestrator":
            import os, subprocess, time
            rp = "/tmp/orchestrator_report.json"
            # 缓存 30 分钟, 过期重跑
            if not os.path.exists(rp) or time.time()-os.path.getmtime(rp) > 1800:
                try:
                    subprocess.run([sys.executable, "/home/agentuser/stockdb/t1/orchestrator.py"],
                                   capture_output=True, timeout=600)
                except Exception as e:
                    return self._send(json.dumps({"error": str(e)}).encode())
            if os.path.exists(rp):
                return self._send(open(rp, "rb").read())
            return self._send(b'{"error":"orchestrator unavailable"}')
        if p == "/api/pools": return self._send(json.dumps(apply_rt(load_pools()), ensure_ascii=False).encode())
        if p == "/api/hot": return self._send(json.dumps(load_hot(), ensure_ascii=False).encode())
        if p == "/api/news": return self._send(json.dumps(load_news(), ensure_ascii=False).encode())
        if p == "/api/aipicks":
            try: return self._send(json.dumps(apply_rt(json.load(open(f"{BASE}/data/ai_picks.json"))), ensure_ascii=False).encode())
            except Exception: return self._send(b"{}")
        if p == "/api/social":
            try: return self._send(json.dumps(json.load(open(f"{BASE}/data/social/latest.json")), ensure_ascii=False).encode())
            except Exception: return self._send(b"{}")
        if p == "/api/global":
            try: return self._send(json.dumps(json.load(open(f"{BASE}/data/global/latest.json")), ensure_ascii=False).encode())
            except Exception: return self._send(b"{}")
        if p == "/api/snapshot":
            try: return self._send(json.dumps(json.load(open(f"{BASE}/data/market_snapshot/latest.json")), ensure_ascii=False).encode())
            except Exception: return self._send(b"{}")
        if p == "/api/review":
            try:
                con = duckdb.connect(f"{BASE}/stock.duckdb", read_only=True)
                rows = con.execute("SELECT id, trade_date, kind, symbol, payload FROM analysis WHERE kind IN ('pick','skip','review2','drift','brief') ORDER BY id DESC LIMIT 60").fetchall()
                con.close()
                return self._send(json.dumps([{"id":r[0],"date":r[1],"kind":r[2],"symbol":r[3],"payload":json.loads(r[4]) if r[4] else {}} for r in rows], ensure_ascii=False).encode())
            except Exception as e:
                return self._send(json.dumps({"error": str(e)}).encode())
        if p == "/":
            html = open(os.path.join(BASE, "web", "index.html"), encoding="utf-8").read()
            return self._send(html.encode(), "text/html; charset=utf-8")
        self.send_response(404); self.end_headers()

if __name__ == "__main__":
    print("dashboard http://0.0.0.0:8600")
    ThreadingHTTPServer(("0.0.0.0", 8600), H).serve_forever()
