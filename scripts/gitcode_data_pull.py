#!/usr/bin/env python3
"""
gitcode_data_pull.py — 从 GitCode stock-data 仓拉指定数据集到本地/CI 目录

数据仓: dufuwuxin/stock-data (公开可读, 需 PAT)
  wh/minute1/YYYYMM.parquet       1 分钟 K
  wh/daily/YYYY.parquet           日 K (前复权, 无复权因子列)
  wh/moneyflow_hist/YYYY.parquet  资金流
  wh/fq/<symbol>.parquet          逐票前复权全史

实现只走 subprocess + git (与 scripts/sync_gitcode.py 同风格), 不依赖 requests:
  1. 维护一个裸缓存仓 (默认 ~/.cache/gitcode-stock-data, 可用 GITCODE_CACHE 覆盖),
     `git clone --no-checkout --depth 1`; 已存在则 `git fetch --depth 1`。
  2. `git ls-tree -r -l <ref> -- wh/<dataset>` 拿远端清单 + 每个 blob 的字节数。
  3. 本地已存在且大小一致的跳过; 其余用 `git cat-file blob <sha>` 流式写出到
     `<dest>.part` 再 os.replace, 中断不会留下半个 parquet。

落盘布局 (--out 默认为 ~/workspace/stocks-data-kline/data/minute/):
  <out>/minute1/YYYYMM.parquet
  <out>/daily/YYYY.parquet
  <out>/moneyflow_hist/YYYY.parquet
  <out>/fq/<symbol>.parquet

--report 输出校验 JSON: 每个数据集的 文件数/总行数/日期范围/股票数/
每票每日根数中位数/缺失率; minute1 另给逐日 bars_per_sym。
统计需要本机有 pyarrow(+pandas); 缺库时报告对应字段写 null 并置
status=insufficient, 进程退出码 3 (不伪装成通过)。

退出码:
  0 正常; 2 参数/仓库缺失或 --out 落在代码仓内; 3 报告统计不可用(pyarrow 缺失);
  4 缓存刷新失败但用了旧缓存继续拉取; 5 拉取过程中有文件失败。

凭据: 只从 ~/.config/stocks-db/tokens.json 的 dufuwuxin_gitcode 读,
      或环境变量 GITCODE_DATA_TOKEN; 任何日志/报告里都脱敏为 ***。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
CST = timezone(timedelta(hours=8))

GC_HOST = "gitcode.com"
GC_OWNER_REPO = "dufuwuxin/stock-data"
GC_BRANCH = "main"
TOKEN_FILE = os.path.expanduser("~/.config/stocks-db/tokens.json")
TOKEN_KEY = "dufuwuxin_gitcode"
TOKEN_ENV = "GITCODE_DATA_TOKEN"

DEFAULT_OUT = os.path.expanduser("~/workspace/stocks-data-kline/data/minute")
DEFAULT_CACHE = os.path.expanduser("~/.cache/gitcode-stock-data")

# 数据集 -> 仓内目录
DATASETS = {
    "minute1": "wh/minute1",
    "daily": "wh/daily",
    "moneyflow_hist": "wh/moneyflow_hist",
    "fq": "wh/fq",
}
# minute1 完整交易日的每票根数 (09:31~11:30 + 13:01~15:00 = 240 分钟,
# 实测完整日 234~238, 取中位数 236 作为缺失率分母基准)
EXPECTED_BARS_FULL = 236


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def load_token() -> str:
    tok = os.environ.get(TOKEN_ENV, "")
    if tok:
        return tok
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        die(f"✗ 读不到凭据文件 {TOKEN_FILE}: {exc}")
    tok = str(data.get(TOKEN_KEY) or "")
    if not tok:
        die(f"✗ {TOKEN_FILE} 缺键 {TOKEN_KEY}, 请先配置 GitCode PAT")
    return tok


def repo_url() -> str:
    """无凭据的仓库 URL; PAT 只经 http.extraHeader 注入, 不落进 remote URL / .git/config。"""
    return f"https://{GC_HOST}/{GC_OWNER_REPO}.git"


def basic_header(token: str) -> str:
    """Basic 认证头取值; base64 在进程内算, 不落盘。"""
    raw = f"oauth2:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def auth_args(token: str) -> list[str]:
    """git 全局选项: 用 -c http.extraHeader 传 PAT, 不走 URL userinfo。"""
    return ["-c", f"http.extraHeader=Authorization: {basic_header(token)}"]


def redact(text: str, token: str) -> str:
    """任何 print / 异常信息写出前, 把 PAT 及其 Basic 头统一脱敏。"""
    if not token:
        return text
    return text.replace(token, "***").replace(basic_header(token), "***")


def run_git(args: list[str], cwd: str | None = None, token: str = "",
            check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    cmd = ["git", *(auth_args(token) if token else []), *args]
    print(f"  $ {redact(' '.join(cmd), token)}")
    p = subprocess.run(cmd, cwd=cwd, env=env, text=True,
                       stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.STDOUT if capture else None)
    if check and p.returncode != 0:
        out = redact((p.stdout or "").strip(), token)
        die(f"✗ git {' '.join(args[:3])} 失败 (rc={p.returncode}):\n{out}")
    return p


def config_has_credential(cache: str) -> bool:
    """缓存仓 .git/config 里是否还残留带凭据的 remote URL (只看布尔, 不打印内容)。"""
    try:
        with open(os.path.join(cache, ".git", "config"), encoding="utf-8",
                  errors="replace") as fh:
            return "oauth2:" in fh.read()
    except OSError:
        return False


def ensure_cache(cache: str, token: str, no_filter: bool) -> tuple[str, bool]:
    """确保缓存仓存在并尽量刷新。返回 (ls-tree 用的 ref, 是否刷新成功)。"""
    url = repo_url()
    git_dir = os.path.join(cache, ".git")
    if not os.path.isdir(git_dir):
        # 用户把 --cache 指到了已有内容的普通目录: 直接报错退出, 绝不动既有路径
        if os.path.isdir(cache) and os.listdir(cache):
            die(f"✗ {cache} 已存在且非空, 但不是 git 缓存仓; "
                "拒绝删除既有目录, 请换 --cache 或自行清理")
        created = not os.path.exists(cache)
        os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
        base = ["clone", "--no-checkout", "--depth", "1",
                "--single-branch", "--branch", GC_BRANCH]
        attempts = [(["--filter=blob:none"] if not no_filter else [])]
        if not no_filter:
            attempts.append([])  # 服务端不支持 partial clone 时退回全 blob
        for flt in attempts:
            p = run_git([*base, *flt, url, cache], token=token, check=False)
            if p.returncode == 0:
                break
            if created:  # 只回滚本次新建的目录
                shutil.rmtree(cache, ignore_errors=True)
        else:
            die("✗ clone 失败: 检查网络与 PAT (需 read_repository 权限)")
        return "HEAD", True

    if config_has_credential(cache):
        # 旧版 clone 把 PAT 拼进 URL 写进了 .git/config, 这里清成无凭据 URL
        run_git(["-C", cache, "remote", "set-url", "origin", url], check=False)
        print("  ! 缓存仓 config 含凭据, 已用 remote set-url 清成无凭据 URL")

    p = run_git(["-C", cache, "fetch", "--depth", "1", "origin", GC_BRANCH],
                token=token, check=False)
    if p.returncode != 0:
        print("  ! 缓存刷新失败, 继续使用已有缓存 (数据可能过期)")
        return "HEAD", False
    return "FETCH_HEAD", True


def list_blobs(cache: str, ref: str, path: str) -> list[dict[str, Any]]:
    p = run_git(["-C", cache, "ls-tree", "-r", "-l", ref, "--", path], token="")
    out: list[dict[str, Any]] = []
    for line in (p.stdout or "").splitlines():
        if "\t" not in line:
            continue
        meta, fpath = line.split("\t", 1)
        parts = meta.split()
        if len(parts) < 4 or parts[1] != "blob" or not fpath.endswith(".parquet"):
            continue
        try:
            size = int(parts[3])
        except ValueError:
            size = -1  # partial clone 可能拿不到 size
        out.append({"path": fpath, "sha": parts[2], "size": size})
    out.sort(key=lambda b: b["path"])
    return out


def select_blobs(blobs: Iterable[dict[str, Any]], only_latest: bool) -> list[dict[str, Any]]:
    items = list(blobs)
    if not only_latest or not items:
        return items
    return [max(items, key=lambda b: os.path.basename(b["path"]))]


def fetch_blob(cache: str, sha: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    with open(tmp, "wb") as fh:
        p = subprocess.Popen(["git", "-C", cache, "cat-file", "blob", sha],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        assert p.stdout is not None
        shutil.copyfileobj(p.stdout, fh, length=1 << 20)
        err = p.stderr.read().decode("utf-8", "replace") if p.stderr else ""
        rc = p.wait()
    if rc != 0:
        os.unlink(tmp)
        raise RuntimeError(f"git cat-file 失败 (sha={sha[:8]}): {err.strip()}")
    os.replace(tmp, dest)


def pull_dataset(cache: str, ref: str, dataset: str, out: str,
                 only_latest: bool, dry_run: bool) -> tuple[list[str], list[str]]:
    """返回 (本地文件路径列表, 本次下载的路径列表)。"""
    path = DATASETS[dataset]
    sub = out if dataset == "minute1" else os.path.join(out, dataset)
    blobs = select_blobs(list_blobs(cache, ref, path), only_latest)
    if not blobs:
        print(f"  ! {dataset}: 远端 {path}/ 下没有 parquet")
        return [], []
    local, pulled = [], []
    for b in blobs:
        dest = os.path.join(out, dataset, os.path.basename(b["path"])) \
            if dataset == "minute1" else os.path.join(sub, os.path.basename(b["path"]))
        local.append(dest)
        size = b["size"]
        if size >= 0 and os.path.isfile(dest) and os.path.getsize(dest) == size:
            print(f"  = 跳过(大小一致) {os.path.relpath(dest, out)}  {size:,} B")
            continue
        print(f"  ↓ {b['path']} -> {os.path.relpath(dest, out)}  {size:,} B")
        if dry_run:
            pulled.append(dest)
            continue
        try:
            fetch_blob(cache, b["sha"], dest)
        except RuntimeError as exc:
            print(f"  ✗ {exc}")
            local.pop()
            continue
        pulled.append(dest)
    return local, pulled


def _read_parquet(paths: list[str], columns: list[str]) -> Any:
    import pyarrow.parquet as pq  # 运行期依赖, 缺库由调用方转成 insufficient

    frames = [pq.read_table(p, columns=columns).to_pandas() for p in paths]
    import pandas as pd

    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=columns)


def dataset_stats(dataset: str, files: list[str]) -> dict[str, Any]:
    """校验统计。pyarrow/pandas 缺失时抛 ImportError, 由调用方标注 insufficient。"""
    if not files:
        return {"status": "missing", "files": 0, "total_rows": 0}
    measure = ["symbol", "date"] if dataset in ("minute1", "daily") else None
    if measure is None:
        measure = ["symbol", "date"]
    df = _read_parquet(files, measure)
    stats: dict[str, Any] = {
        "status": "ok",
        "files": len(files),
        "total_rows": int(len(df)),
        "date_min": str(df["date"].min()) if len(df) else None,
        "date_max": str(df["date"].max()) if len(df) else None,
        "n_symbols": int(df["symbol"].nunique()) if len(df) else 0,
    }
    if not len(df):
        stats["status"] = "empty"
        return stats

    per_sym_day = df.groupby(["date", "symbol"], sort=False).size()
    stats["symbol_days"] = int(len(per_sym_day))
    stats["bars_per_sym_day_median"] = float(per_sym_day.median())
    expected_bars = EXPECTED_BARS_FULL if dataset == "minute1" else 1
    expected = len(per_sym_day) * expected_bars
    stats["expected_rows"] = int(expected)
    stats["missing_rate"] = round(1 - len(df) / expected, 6) if expected else None
    stats["bars_expected_per_sym_day"] = expected_bars

    daily = df.groupby("date", sort=True).agg(rows=("symbol", "size"),
                                              symbols=("symbol", "nunique"))
    bars = per_sym_day.groupby(level=0).agg(["median", "min", "max"])
    per_day = []
    for date, row in daily.iterrows():
        b = bars.loc[date]
        per_day.append({
            "date": str(date),
            "rows": int(row["rows"]),
            "symbols": int(row["symbols"]),
            "bars_per_sym_median": float(b["median"]),
            "bars_per_sym_min": int(b["min"]),
            "bars_per_sym_max": int(b["max"]),
        })
    stats["per_day"] = per_day
    return stats


def resolve_out(out: str) -> str:
    resolved = os.path.abspath(os.path.expanduser(out))
    repo = os.path.abspath(REPO_ROOT)
    if resolved == repo or resolved.startswith(repo + os.sep):
        die(f"✗ --out {resolved} 在代码仓 stocks-ai-view 内; "
            "数据副本不进仓库, 请指到仓外目录", 2)
    return resolved


def parse_datasets(raw: str) -> list[str]:
    names = [x.strip() for x in raw.split(",") if x.strip()]
    bad = [x for x in names if x not in DATASETS]
    if bad:
        die(f"✗ 未知数据集 {bad}; 可选 {sorted(DATASETS)}")
    return names or ["minute1"]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="从 GitCode stock-data 拉数据集 (minute1/daily/moneyflow_hist/fq)")
    ap.add_argument("--dataset", default="minute1",
                    help="逗号分隔: minute1,daily,moneyflow_hist,fq (默认 minute1)")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"落地根目录 (默认 {DEFAULT_OUT})")
    ap.add_argument("--cache", default=os.environ.get("GITCODE_CACHE", DEFAULT_CACHE),
                    help="git 缓存仓目录")
    ap.add_argument("--only-latest", action="store_true",
                    help="每个数据集只拉文件名最大的那份 (最新月份/年份)")
    ap.add_argument("--dry-run", action="store_true", help="只列将要下载的文件")
    ap.add_argument("--report", default="", help="校验结果 JSON 输出路径")
    ap.add_argument("--no-filter", action="store_true",
                    help="不做 partial clone (服务端不支持 blob filter 时用)")
    args = ap.parse_args()

    datasets = parse_datasets(args.dataset)
    out = resolve_out(args.out)
    token = load_token()
    print(f"仓库 {GC_OWNER_REPO} · 数据集 {datasets} · 落地 {out}")

    ref, refreshed = ensure_cache(args.cache, token, args.no_filter)
    commit = run_git(["-C", args.cache, "rev-parse", ref], token="").stdout.strip()

    local_by_ds: dict[str, list[str]] = {}
    failed = 0
    for ds in datasets:
        local, pulled = pull_dataset(args.cache, ref, ds, out, args.only_latest,
                                     args.dry_run)
        local_by_ds[ds] = local
        if not local:
            failed += 1
        print(f"  {ds}: 本地 {len(local)} 份, 本次下载 {len(pulled)} 份")

    report: dict[str, Any] = {
        "ts": datetime.now(CST).isoformat(timespec="seconds"),
        "source": {
            "repo": GC_OWNER_REPO,
            "branch": GC_BRANCH,
            "commit": commit,
            "cache_refreshed": refreshed,
            "datasets": datasets,
        },
        "out": out,
        "dry_run": args.dry_run,
        "datasets": {},
    }
    insufficient: list[str] = []
    if not args.dry_run:
        for ds in datasets:
            try:
                report["datasets"][ds] = dataset_stats(ds, local_by_ds.get(ds, []))
            except ImportError as exc:
                insufficient.append(ds)
                report["datasets"][ds] = {
                    "status": "insufficient",
                    "reason": f"缺少 pyarrow/pandas: {exc}",
                    "files": len(local_by_ds.get(ds, [])),
                    "total_rows": None,
                    "date_min": None,
                    "date_max": None,
                    "n_symbols": None,
                    "bars_per_sym_day_median": None,
                    "missing_rate": None,
                    "per_day": None,
                }
            except (OSError, ValueError) as exc:
                insufficient.append(ds)
                report["datasets"][ds] = {"status": "error", "reason": str(exc)}
    report["insufficient_datasets"] = insufficient

    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".",
                    exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"→ 报告 {args.report}")

    for ds, st in report["datasets"].items():
        if st.get("status") != "ok":
            print(f"  ! {ds}: {st.get('status')} {st.get('reason', '')}")
            continue
        print(f"  {ds}: {st['total_rows']:,} 行, {st['n_symbols']} 只, "
              f"{st['date_min']}~{st['date_max']}, "
              f"每票每日中位 {st['bars_per_sym_day_median']:.0f} 根, "
              f"缺失率 {st['missing_rate']:.4%}")

    if insufficient and args.report:
        return 3
    if not refreshed:
        return 4
    return 5 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
