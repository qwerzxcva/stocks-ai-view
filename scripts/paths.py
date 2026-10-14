#!/usr/bin/env python3
"""
paths.py — 统一路径解析 (本地 + GitHub Actions 双环境兼容)

问题: 多个脚本硬编码 /root/workspace/..., 在 GitHub runner 上该路径不存在
      (runner 用 /home/runner/work/<repo>/<repo>/), 导致 PermissionError。

方案: 统一从这里取路径, 按优先级:
  1. 环境变量 (STOCKS_KLINE_BULK / STOCKS_LIVE_MARKET / STOCKS_AI_VIEW_DATA ...)
  2. CI 布局探测 (../stocks-data-kline/data/bulk 等相对当前仓的位置)
  3. 本地默认 (/root/workspace/...)

CI 工作流里的目录布局 (actions/checkout with path:):
  <workspace>/
    stocks-ai-view/        <- 当前仓
    kline-repo/            <- checkout path: kline-repo
    ../stocks-data-kline/  <- 工作流里 mkdir + cp 造出来的

用法:
  from paths import BULK, LIVE_MARKET, AI_VIEW_DATA, INDUSTRY_MAP
  from paths import resolve(kind)     # 更细粒度
"""
import os

# ─── 仓库根 ───────────────────────────────────────────────────────
def _repo_root():
    """当前脚本所在的 stocks-ai-view 根目录。"""
    # paths.py 在 <repo>/scripts/ 下
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


REPO_ROOT = _repo_root()
WORKSPACE = os.path.dirname(REPO_ROOT)          # <workspace>/stocks-ai-view 的上一级


def _first_env(*names):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _first_existing(*paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


# ─── K线 bulk ────────────────────────────────────────────────────
def kline_bulk():
    v = _first_env("STOCKS_KLINE_BULK")
    if v:
        return v
    p = _first_existing(
        os.path.join(WORKSPACE, "stocks-data-kline", "data", "bulk"),
        os.path.join(REPO_ROOT, "..", "stocks-data-kline", "data", "bulk"),
        os.path.join(WORKSPACE, "kline-repo", "data", "bulk"),
        "/root/workspace/stocks-data-kline/data/bulk",
    )
    return p or os.path.join(WORKSPACE, "stocks-data-kline", "data", "bulk")


# ─── 实时数据目录 ────────────────────────────────────────────────
def live_market():
    v = _first_env("STOCKS_LIVE_MARKET")
    if v:
        return v
    p = _first_existing(
        os.path.join(WORKSPACE, "stocks-data-live", "data", "market"),
        os.path.join(REPO_ROOT, "..", "stocks-data-live", "data", "market"),
        "/root/workspace/stocks-data-live/data/market",
    )
    return p or os.path.join(WORKSPACE, "stocks-data-live", "data", "market")


def live_root():
    v = _first_env("STOCKS_LIVE_ROOT")
    if v:
        return v
    p = _first_existing(
        os.path.join(WORKSPACE, "stocks-data-live", "data"),
        os.path.join(REPO_ROOT, "..", "stocks-data-live", "data"),
        "/root/workspace/stocks-data-live/data",
    )
    return p or os.path.join(WORKSPACE, "stocks-data-live", "data")


# ─── ai-view data 目录 ──────────────────────────────────────────
def ai_view_data():
    v = _first_env("STOCKS_AI_VIEW_DATA")
    if v:
        return v
    return os.path.join(REPO_ROOT, "data")


# ─── 派生路径 ───────────────────────────────────────────────────
BULK = kline_bulk()
LIVE_MARKET = live_market()
LIVE_ROOT = live_root()
AI_VIEW_DATA = ai_view_data()
INDUSTRY_MAP = os.path.join(LIVE_ROOT, "meta", "industry_map_tv.json")
INDUSTRY_MAP_EM = os.path.join(LIVE_ROOT, "meta", "industry_map.json")
SNAPSHOT_GLOB = os.path.join(LIVE_MARKET, "snapshot_*.json")
DUCKDB_PATH = os.environ.get("STOCKS_DB") or os.path.join(LIVE_ROOT, "stocks.duckdb")


# ─── bulk kline 文件清单 ─────────────────────────────────────────
# 大分片在 git 里以 .csv.gz 存放 (54.6MB -> 15.7MB), 而 28 个引擎脚本都
# glob 'day_qfq_*.csv'。让 glob 同时看到 .csv 与解压后的 .csv.gz。

def bulk_kline_files(bulk_dir=None):
    """返回 day_qfq_* 的全部可读路径: 原生 .csv 优先, 否则解压 .csv.gz。

    解压产物写到系统临时目录, 不污染仓库工作区。同名 .csv 已存在时
    直接用它, 避免重复解压。
    """
    import glob
    import gzip
    import shutil
    import tempfile

    d = bulk_dir or BULK
    out = []
    seen = set()
    cache = os.path.join(tempfile.gettempdir(), "stocks_bulk_gz")
    for p in sorted(glob.glob(os.path.join(d, "day_qfq_*.csv"))):
        if p not in seen:
            seen.add(p)
            out.append(p)
    for gz in sorted(glob.glob(os.path.join(d, "day_qfq_*.csv.gz"))):
        base = os.path.basename(gz)[:-3]
        plain = os.path.join(d, base)
        if plain in seen:
            continue
        if os.path.exists(plain):
            seen.add(plain)
            out.append(plain)
            continue
        os.makedirs(cache, exist_ok=True)
        target = os.path.join(cache, base)
        if not os.path.exists(target):
            with gzip.open(gz, "rb") as fin, open(target, "wb") as fout:
                shutil.copyfileobj(fin, fout, length=1 << 20)
        seen.add(target)
        out.append(target)
    return out


def ensure_bulk_uncompressed(bulk_dir=None):
    """CI 里跑引擎前调用一次: 把所有 .csv.gz 解压到 bulk 目录旁边。

    返回解压出的文件数。工作区已有同名 .csv 时跳过。
    """
    import glob
    import gzip
    import shutil

    d = bulk_dir or BULK
    n = 0
    for gz in sorted(glob.glob(os.path.join(d, "day_qfq_*.csv.gz"))):
        plain = os.path.join(d, os.path.basename(gz)[:-3])
        if os.path.exists(plain):
            continue
        with gzip.open(gz, "rb") as fin, open(plain, "wb") as fout:
            shutil.copyfileobj(fin, fout, length=1 << 20)
        n += 1
    return n



def safe_load_json(path, default=None):
    """安全读 JSON — 多个 workflow 并发写 data/ 时, 裸 json.load 会撞上
    「读到写了一半的文件」而抛 JSONDecodeError (2026-10-14 实测:
    portfolio_optimizer 读 decision.json 时该文件正好被另一个 run 覆写)。

    读失败时返回 default, 让调用方按「数据缺失」处理而不是整个引擎崩掉。
    """
    import json
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        return default


def describe():
    """打印路径解析结果 (调试用)。"""
    print("paths:")
    print(f"  REPO_ROOT      = {REPO_ROOT}")
    print(f"  WORKSPACE      = {WORKSPACE}")
    print(f"  BULK           = {BULK}  exists={os.path.exists(BULK)}")
    print(f"  LIVE_MARKET    = {LIVE_MARKET}  exists={os.path.exists(LIVE_MARKET)}")
    print(f"  AI_VIEW_DATA   = {AI_VIEW_DATA}  exists={os.path.exists(AI_VIEW_DATA)}")
    print(f"  INDUSTRY_MAP   = {INDUSTRY_MAP}  exists={os.path.exists(INDUSTRY_MAP)}")


if __name__ == "__main__":
    describe()
