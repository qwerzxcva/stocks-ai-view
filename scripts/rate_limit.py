#!/usr/bin/env python3
"""
rate_limit.py — 统一限流门 (令牌桶), 所有采集脚本共用

借鉴: waditu/tushare 的积分制限流 + shidenggui/easyquotation 的分批上限。

问题 (2026-10-13):
  各 collect_*.py 各写 time.sleep, 频率策略不一致; AGENTS.md 要求 1-2 QPS
  但无自动拦截机制, 超频只能靠人看。WAF 风险实际存在 (东财 push2 已被封)。

设计:
  - RateLimiter(per_minute): 令牌桶, 每 60s 补 per_minute 个令牌
  - @rate_limited("tencent_quote") 装饰器: 按源限流, 令牌不足则阻塞等待
  - 每源上限声明在 sources_registry.py 的 rate_limit 字段 (req/min)
  - 默认上限: 东财 10/min, 腾讯 50/min, 新浪 10/min, 集思录 10/min
    (与 registry notes 一致)

用法:
  from rate_limit import rate_limited, limiter_for

  @rate_limited("tencent_quote")
  def fetch():
      return _http(url)

  # 或手动
  lim = limiter_for("eastmoney_datacenter")
  lim.acquire()
  do_request()
"""
import functools
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 每源每分钟请求上限 (与 sources_registry notes 对齐)
DEFAULT_LIMITS = {
    "tencent_quote": 50, "tencent_kline": 50, "tencent_index_history": 50,
    "sina_hq": 10, "sina_news": 10, "sina_kline_ma": 10, "sina_vip_kline": 10,
    "sina_kline_daily": 10, "sina_etf_hist": 10,
    "eastmoney_datacenter": 10, "eastmoney_zt_pool": 10, "eastmoney_zdfenbu": 10,
    "em_push2his_clist": 10, "em_push2his_etf_onsite": 10,
    "em_fund_pingzhong": 10, "em_fund_lsjz": 10, "em_lcpd_report": 10,
    "em_fund_etf_hist_min": 10, "em_ann_np": 10,
    "ths_strong_stocks": 10, "ths_industry_list": 8, "ths_concept_list": 8,
    "ths_financial_abstract": 10, "ths_financial_abstract_v2": 10,
    "tv_screener": 10, "douyin_hot_search": 2, "toutiao_hot": 10,
    "jisilu": 10, "xueqiu": 10, "remote_news": 10,
    "coingecko": 10, "default": 6,
}

# 从 registry 读 rate_limit 字段 (若有)
def _load_registry_limits():
    try:
        import sources_registry as sr
        out = {}
        for s in sr.SOURCES:
            sid = s["id"]
            note = (s.get("notes") or "") + " " + (s.get("rate_limit") or "")
            # 从 notes 里解析 "10 req/min" / "30s/次" 等
            import re
            m = re.search(r"(\d+)\s*req/min", note)
            if m:
                out[sid] = int(m.group(1))
                continue
            m = re.search(r"(\d+)\s*s/次", note)
            if m:
                sec = int(m.group(1))
                if sec > 0:
                    out[sid] = max(1, 60 // sec)
                continue
        return out
    except Exception:
        return {}


class RateLimiter:
    """线程安全令牌桶。"""

    def __init__(self, per_minute):
        self.capacity = max(1, float(per_minute))
        self.rate = self.capacity / 60.0     # tokens per second
        self.tokens = self.capacity
        self.last = time.monotonic()
        self._lock = threading.Lock()
        self.wait_total = 0.0
        self.n_calls = 0

    def acquire(self, n=1):
        """取 n 个令牌, 不足则阻塞。"""
        with self._lock:
            self.n_calls += n
            now = time.monotonic()
            # 补令牌
            self.tokens = min(self.capacity,
                              self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= n:
                self.tokens -= n
                return 0.0
            need = (n - self.tokens) / self.rate
            self.tokens = 0.0
        if need > 0:
            time.sleep(need)
            self.wait_total += need
            with self._lock:
                self.tokens = 0.0
                self.last = time.monotonic()
        return need

    def stats(self):
        return {"per_minute": self.capacity, "calls": self.n_calls,
                "wait_total_s": round(self.wait_total, 2)}


_LIMITERS = {}
_LOCK = threading.Lock()


def _limits():
    merged = dict(DEFAULT_LIMITS)
    merged.update(_load_registry_limits())
    return merged


def limiter_for(source_id, per_minute=None):
    """取 (或建) 某源的限流器。"""
    key = source_id or "default"
    with _LOCK:
        if key not in _LIMITERS:
            rpm = per_minute or _limits().get(key, _limits()["default"])
            _LIMITERS[key] = RateLimiter(rpm)
        return _LIMITERS[key]


def rate_limited(source_id, per_minute=None):
    """装饰器: 函数调用前先取令牌。"""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            limiter_for(source_id, per_minute).acquire()
            return fn(*a, **kw)
        return wrapper
    return deco


def stats_all():
    return {k: v.stats() for k, v in _LIMITERS.items()}


if __name__ == "__main__":
    import json as _json
    print(_json.dumps(_limits(), indent=2, ensure_ascii=False))
