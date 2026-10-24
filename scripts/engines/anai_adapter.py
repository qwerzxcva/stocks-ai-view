#!/usr/bin/env python3
"""
anai_adapter.py — anai.pro 双模式接入适配器 (新增引擎, 不改任何既有文件)

合规声明 (与 reports/anai_api_recon.md 一致):
  - 目标站 robots.txt: User-agent:* Allow:/, 但附 Cloudflare Content-Signal
    search=yes, ai-train=no, use=reference —— 本适配器仅作「引用级」读取,
    输出供决策参考, 严禁将抓取内容用于任何 AI 训练/微调。
  - robots 同时 Disallow GPTBot/ClaudeBot/KimiBot 等主流 AI 爬虫; 本脚本以自有 UA
    按 User-agent:* 组评估, --respect-robots (默认开) 启动时核对目标路径未被禁。
  - 红线: 不破解签名/验证码/反爬, 不伪造鉴权, 不访问登录墙后数据;
    若登录遇验证码只记录并提示人工处理。

双模式:
  模式 A (凭据模式): 仅当环境变量 ANAI_COOKIE 存在时启用; cookie 只从环境变量读,
    绝不打印值/不落盘/不入 git/不写日志。refresh 钩子 = 调 /api/auth/get-session
    重新校验会话 (better-auth cookie 会话, 无独立 refresh token, 见侦察报告 §2)。
  模式 B (匿名模式, 默认): 只用 GET /s/{shortId} 与 GET /api/stock/search。
  两模式对上层暴露统一接口 (search / fetch_share), 输出 JSON 的 mode 字段标识实际模式。

功能:
  search(keyword)        -> [{code, name, type}] 代码映射
  fetch_share(short_id)  -> 解析 SSR HTML/RSC 内嵌行情快照 JSON + AI 报告 markdown ->
    {code, name, snapshot{price,prev_close,open,high,low,volume,amount,mktcap,
     turnover,pe,pb,...}, report_md, model, fetched_at, mode, data_asof}

限流: 请求间隔默认 >=2s (--min-interval 可配, 硬下限 1s, 拒绝更低)。
缓存: /tmp/anai_cache/ (不写仓库 data/), TTL 默认 4h (--cache-ttl 秒, --no-cache 关闭)。

已知降级:
  - RSC payload 用正则级解析 (非完整 RSC parser), Next.js 升级可能失效 -> 解析失败
    时 snapshot/report_md 置 null 且 degraded 字段说明, 不抛断主流程。
  - 匿名模式无 shortId 列表入口: search 不回传 shortId, --code 只能做代码映射验证,
    fetch_share 必须显式给 shortId。
  - 快照数值为中文单位字符串 ("35.91亿"/"3.90%"), 保留原样并附 *_raw, 由上层归一。

CLI:
  python3 anai_adapter.py --search 中天
  python3 anai_adapter.py --short-id 0d5772a8
  python3 anai_adapter.py --code 600522            # 仅代码映射 (匿名无 shortId 反查)
  python3 anai_adapter.py --short-id 0d5772a8 --out /tmp/out.json --no-cache
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.abspath(os.path.join(HERE, "..", "..", "data", "engines", "anai_adapter.json"))


BASE = "https://anai.pro"
UA = "stocks-ai-view-anai-adapter/1.0 (+reference-use; contact: local-research)"
ENGINE = "anai_adapter"

CACHE_DIR = "/tmp/anai_cache"
DEFAULT_TTL = 4 * 3600
DEFAULT_INTERVAL = 2.0
MIN_INTERVAL_FLOOR = 1.0

ROBOTS_URL = BASE + "/robots.txt"
CHECKED_PATHS = ["/s/", "/api/stock/search"]  # 匿名模式实际访问的路径前缀


# ---------------------------------------------------------------- 凭据 (红线2)

def _load_cookie() -> str | None:
    """仅从环境变量 ANAI_COOKIE 读凭据。绝不打印/落盘/入日志。"""
    v = os.environ.get("ANAI_COOKIE", "").strip()
    return v or None


def _redact(text: str, cookie: str | None) -> str:
    """异常信息脱敏: 若错误文本中混入 cookie 片段则打码 (纵深防御)。"""
    if cookie and len(cookie) >= 8:
        text = text.replace(cookie, "***REDACTED***")
    return text


# ---------------------------------------------------------------- HTTP 层

class PoliteClient:
    def __init__(self, min_interval: float = DEFAULT_INTERVAL, timeout: float = 20.0,
                 cookie: str | None = None):
        self.min_interval = max(min_interval, MIN_INTERVAL_FLOOR)
        self.timeout = timeout
        self.cookie = cookie
        self._last_req = 0.0

    def _throttle(self):
        delta = time.monotonic() - self._last_req
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)

    def get(self, url: str) -> tuple[int, str, dict]:
        self._throttle()
        headers = {"User-Agent": UA, "Accept": "*/*"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                self._last_req = time.monotonic()
                return resp.status, body, dict(resp.headers)
        except urllib.error.HTTPError as e:
            self._last_req = time.monotonic()
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise AdapterError(
                _redact(f"HTTP {e.code} for {url}: {body[:200]}", self.cookie)) from e
        except urllib.error.URLError as e:
            raise AdapterError(_redact(f"network error for {url}: {e.reason}", self.cookie)) from e


class AdapterError(Exception):
    pass


# ---------------------------------------------------------------- robots 合规

def robots_allows(client: PoliteClient, paths: list[str]) -> tuple[bool, str]:
    """抓 robots.txt, 解析 User-agent:* 组的 Disallow, 核对 paths 是否被禁。
    解析失败时保守放行 (robots 缺失=允许), 但记录说明。"""
    try:
        status, body, _ = client.get(ROBOTS_URL)
        if status != 200:
            return True, f"robots.txt status={status}, 按缺失放行"
    except AdapterError as e:
        return True, f"robots.txt 获取失败 ({e}), 保守放行"
    # 提取 User-agent: * 组
    disallows: list[str] = []
    in_star = False
    for raw in body.splitlines():
        line = raw.strip()
        low = line.lower()
        if low.startswith("user-agent:"):
            in_star = (low.split(":", 1)[1].strip() == "*")
        elif in_star and low.startswith("disallow:"):
            disallows.append(line.split(":", 1)[1].strip())
        elif low and not low.startswith("#") and in_star and low.startswith("user-agent"):
            in_star = False
    blocked = [p for p in paths if any(d and p.startswith(d) for d in disallows)]
    note = "robots User-agent:* Disallow=" + (json.dumps(disallows) if disallows else "[]")
    if blocked:
        return False, f"{note}; 被禁路径: {blocked}"
    return True, note


# ---------------------------------------------------------------- 缓存

def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:24] + ".json"


def cache_get(url: str, ttl: int) -> dict | None:
    path = os.path.join(CACHE_DIR, _cache_key(url))
    try:
        st = os.stat(path)
        if time.time() - st.st_mtime > ttl:
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def cache_put(url: str, payload: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, _cache_key(url))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 解析

def _unescape_rsc(s: str) -> str:
    """RSC push 内的字符串是 JS 字符串字面量转义; 用 json 反转义最稳。"""
    try:
        return json.loads('"' + s.replace('"', '\\"') + '"')
    except json.JSONDecodeError:
        # 降级: 手工处理常见转义
        return (s.replace("\\n", "\n").replace("\\t", "\t")
                 .replace('\\"', '"').replace("\\u003e", ">")
                 .replace("\\u003c", "<").replace("\\\\", "\\"))


_STOCK_RE = re.compile(r'\\+"stock\\+"\s*:\s*(\{.*?\})\s*[,}]', re.S)
_MODEL_RE = re.compile(r'\\+"model\\+"\s*:\s*\\+"([^"\\]+)\\+"')


def parse_share_html(html: str) -> dict:
    """从 /s/{shortId} SSR HTML 提取快照 JSON + AI 报告 md + model + data_asof。"""
    out = {"snapshot_raw": None, "report_md": None, "model": None, "degraded": []}

    m = _STOCK_RE.search(html)
    if m:
        blob = _unescape_rsc(m.group(1))
        try:
            out["snapshot_raw"] = json.loads(blob)
        except json.JSONDecodeError:
            out["degraded"].append("snapshot_json_parse_failed")
    else:
        out["degraded"].append("snapshot_block_not_found")

    mm = _MODEL_RE.search(html)
    if mm:
        out["model"] = mm.group(1)

    # 报告 md: 找 push 中最长的、含 markdown 标题的转义字符串
    best = ""
    for pm in re.finditer(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html, re.S):
        body = _unescape_rsc(pm.group(1))
        if len(body) > len(best) and re.search(r"^#{1,3}\s", body, re.M) and len(body) > 200:
            best = body
    if best:
        out["report_md"] = best
    else:
        out["degraded"].append("report_md_not_found")
    return out


_NUM_FIELDS = {
    "price": "price", "previous_close": "prev_close", "open": "open",
    "high": "high", "low": "low", "volume": "volume", "amount": "amount",
    "market_value": "mktcap", "turnover_rate": "turnover", "pe": "pe", "pb": "pb",
}


def normalize_snapshot(raw: dict | None) -> dict:
    """12 字段统一结构; 数值保留原字符串 (带中文单位), 由上层归一。"""
    snap = {v: None for v in _NUM_FIELDS.values()}
    snap.update({"change": None, "change_rate": None,
                 "circulating_value": None, "vr": None})
    if not raw:
        return snap
    for src, dst in _NUM_FIELDS.items():
        snap[dst] = raw.get(src)
    for extra in ("change", "change_rate", "circulating_value", "vr"):
        snap[extra] = raw.get(extra)
    return snap


# ---------------------------------------------------------------- 公开接口

class AnaiAdapter:
    """双模式适配器。mode 自动判定: ANAI_COOKIE 存在 -> credential, 否则 anonymous。"""

    def __init__(self, min_interval: float = DEFAULT_INTERVAL,
                 cache_ttl: int = DEFAULT_TTL, use_cache: bool = True,
                 respect_robots: bool = True):
        if min_interval < MIN_INTERVAL_FLOOR:
            raise AdapterError(f"min_interval 不允许 < {MIN_INTERVAL_FLOOR}s (礼貌抓取红线)")
        self.cookie = _load_cookie()
        self.mode = "credential" if self.cookie else "anonymous"
        self.cache_ttl = cache_ttl
        self.use_cache = use_cache
        self.client = PoliteClient(min_interval=min_interval, cookie=self.cookie)
        self.robots_note = "disabled"
        if respect_robots:
            ok, note = robots_allows(self.client, CHECKED_PATHS)
            self.robots_note = note
            if not ok:
                raise AdapterError("robots.txt 禁止访问目标路径, 拒绝继续: " + note)
        if self.mode == "credential":
            self._check_session()

    # -- 凭据模式: refresh 钩子 (better-auth cookie 会话, 无独立 refresh token)
    def _check_session(self) -> None:
        """refresh 钩子: 调 /api/auth/get-session 校验 cookie 有效性。
        会话失效则降级为匿名模式并记录 (不打印 cookie 值)。"""
        try:
            _, body, _ = self.client.get(BASE + "/api/auth/get-session")
            if body.strip() in ("null", ""):
                self.mode = "anonymous"
                self.client.cookie = None
                self.robots_note += "; credential_invalid_fallback_anonymous"
        except AdapterError as e:
            if "captcha" in str(e).lower() or "challenge" in str(e).lower():
                raise AdapterError("检测到验证码/挑战机制, 按红线转人工处理, 不实施破解") from e
            self.mode = "anonymous"
            self.client.cookie = None
            self.robots_note += "; session_check_failed_fallback_anonymous"

    def refresh(self) -> dict:
        """显式刷新/校验凭据 (模式 A)。返回 {mode, valid}。无凭据时直接报匿名。"""
        if not self.cookie:
            return {"mode": self.mode, "valid": False, "note": "no ANAI_COOKIE"}
        _, body, _ = self.client.get(BASE + "/api/auth/get-session")
        valid = body.strip() not in ("null", "")
        return {"mode": self.mode, "valid": valid}

    # -- 业务接口 (两模式统一, 上层不感知)
    def search(self, keyword: str) -> dict:
        url = BASE + "/api/stock/search?keyword=" + urllib.parse.quote(keyword)
        cached = self.use_cache and cache_get(url, self.cache_ttl)
        if cached is not None:
            results = cached["results"]
        else:
            _, body, _ = self.client.get(url)
            try:
                results = json.loads(body)
            except json.JSONDecodeError as e:
                raise AdapterError(f"search 返回非 JSON: {body[:200]}") from e
            if self.use_cache:
                cache_put(url, {"results": results})
        return self._envelope({
            "keyword": keyword,
            "results": results,
            "data_asof": None,
        })

    def fetch_share(self, short_id: str) -> dict:
        url = f"{BASE}/s/{short_id}"
        cached = self.use_cache and cache_get(url, self.cache_ttl)
        if cached is not None:
            parsed = cached["parsed"]
        else:
            _, html, _ = self.client.get(url)
            parsed = parse_share_html(html)
            if self.use_cache:
                cache_put(url, {"parsed": parsed})
        raw = parsed.get("snapshot_raw") or {}
        snap = normalize_snapshot(parsed.get("snapshot_raw"))
        data_asof = None
        if isinstance(raw.get("time"), (int, float)):
            data_asof = datetime.fromtimestamp(raw["time"] / 1000, timezone.utc).isoformat()
        return self._envelope({
            "short_id": short_id,
            "code": raw.get("code"),
            "name": raw.get("name"),
            "symbol": raw.get("symbol"),
            "snapshot": snap,
            "report_md": parsed.get("report_md"),
            "model": parsed.get("model"),
            "data_asof": data_asof,
            "degraded": parsed.get("degraded") or [],
        })

    def resolve_code(self, code_or_name: str) -> dict:
        """按代码或名称经 search 做映射。匿名模式无 shortId 反查, 仅返回候选列表。"""
        res = self.search(code_or_name)
        hits = res["data"]["results"]
        exact = [h for h in hits
                 if h.get("code", "").endswith(code_or_name) or h.get("name") == code_or_name]
        res["data"]["best"] = (exact or hits or [None])[0]
        res["data"]["note"] = ("匿名模式 search 不回传 shortId, 无法按代码自动取最新分享; "
                               "fetch_share 需显式 shortId")
        return res

    def _envelope(self, data: dict) -> dict:
        return {
            "ts": datetime.now(timezone.utc).isoformat(),
            "engine": ENGINE,
            "mode": self.mode,
            "robots_note": self.robots_note,
            "data": data,
        }


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="anai.pro 双模式接入适配器 (引用级, 合规见 docstring)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--code", help="按代码/名称做映射, 如 600522 或 中天科技")
    g.add_argument("--short-id", help="分享页 shortId, 如 0d5772a8")
    g.add_argument("--search", help="关键字搜索, 如 中天")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="输出 JSON 文件路径 (默认 DEFAULT_OUT) ('--out -' 输出 stdout)")
    ap.add_argument("--no-cache", action="store_true", help="禁用本地缓存")
    ap.add_argument("--cache-ttl", type=int, default=DEFAULT_TTL, help="缓存 TTL 秒 (默认 14400)")
    ap.add_argument("--min-interval", type=float, default=DEFAULT_INTERVAL,
                    help="请求间隔秒 (默认 2, 硬下限 1)")
    ap.add_argument("--respect-robots", dest="respect_robots", action="store_true",
                    default=True, help="启动时核对 robots.txt (默认开)")
    ap.add_argument("--no-respect-robots", dest="respect_robots", action="store_false",
                    help="跳过 robots 核对 (不推荐)")
    args = ap.parse_args(argv)

    try:
        adapter = AnaiAdapter(min_interval=args.min_interval, cache_ttl=args.cache_ttl,
                              use_cache=not args.no_cache,
                              respect_robots=args.respect_robots)
        if args.search:
            out = adapter.search(args.search)
        elif args.code:
            out = adapter.resolve_code(args.code)
        else:
            out = adapter.fetch_share(args.short_id)
    except AdapterError as e:
        out = {"ts": datetime.now(timezone.utc).isoformat(), "engine": ENGINE,
               "error": str(e)}
        text = json.dumps(out, ensure_ascii=False, indent=2)
        print(text, file=sys.stderr)
        return 1

    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out and args.out != "-":
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
