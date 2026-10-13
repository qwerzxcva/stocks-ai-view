#!/usr/bin/env python3
"""
data_origin.py — 数据源优先级切换 (GitHub 优先, 本地兜底)

原设计 (index.html):
  本地优先 → 失败才走 GitHub raw 兜底
  问题: 本地 data/ 91MB (bulk 复权 81MB) 要随仓库走, 部署体积大;
        且本地数据可能比 GitHub 旧 (CI 每 10 分钟推一次, 本地可能几天没跑)

新设计 (用户 2026-10-13 要求):
  「本地不需要这些数据, 本地站的数据也是实时查询 GitHub/gitcode 的数据库就好了」
  → GitHub raw 优先 → 本地兜底 (GitHub 不可达时)
  → 本地 data/ 只保留少量必要文件, bulk 复权等大文件一律 .gitignore

实现: 本脚本生成 data_origin_patch.js, 由 index.html 引入,
      把 _ghFallback 的优先级反转为 _ghFirst。
"""
import os

ROOT = "/root/workspace/stocks-ai-view"

PATCH = r"""
// ══════════════════════════════════════════════════════════════════
// data_origin_patch.js — 数据源优先级: GitHub 优先, 本地兜底
// 2026-10-13 用户要求: 本地不存大数据, 网站实时查 GitHub/gitcode 数据库
//
// 原逻辑: 本地 fetch → 失败走 GitHub
// 新逻辑: GitHub fetch → 失败走本地 (404/CORS 时兜底)
// 理由:
//   1. 本地 data/ 曾 91MB (bulk 复权 81MB), 不需要随站点部署
//   2. CI 每 10 分钟推 GitHub, 比本地可能几天没跑的数据更新
//   3. raw.githubusercontent.com CORS 放开 (*), 浏览器可直连
// ══════════════════════════════════════════════════════════════════
(function () {
  if (typeof fetchJSON !== 'function') {
    console.warn('[data_origin] fetchJSON 未定义, 补丁未生效');
    return;
  }
  var _orig = fetchJSON;

  // GitHub raw 主源 + gitcode 镜像 (raw.gitcode.com 作为第二源)
  var GH_RAW = 'https://raw.githubusercontent.com/qwerzxcva/stocks-ai-view/main/';
  var GC_RAW = 'https://raw.gitcode.com/qwerzxcva/stocks-ai-view/raw/main/';

  // 大文件 (bulk 复权/全历史) 只从远端拿, 本地不存
  var REMOTE_ONLY = /(adjusted\/bulk|index_full|all_sources|search_index)\.json$/;

  // 分钟级实时数据 (云服务器常驻采集, 优先级最高)
  var MINUTE_LEVEL = /realtime_minute\.json$/;

  // 热数据 (盘中 30 分钟级) 与温数据 (日级) 分开缓存策略
  var HOT = /(hot_impact|latest|enrich|ai_picks|daily_brief|engine_v5|cycle_deep|ban_prob|ban_pick|flow_divergence|market_now|realtime)\.json$/;

  function _fetchRemote(url, timeoutMs) {
    var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var t = timeoutMs || 12000;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, t) : null;
    return fetch(url, { cache: 'no-store', signal: ctrl ? ctrl.signal : undefined })
      .then(function (r) {
        if (timer) clearTimeout(timer);
        if (!r.ok) throw new Error('remote ' + r.status);
        return r.json();
      })
      .catch(function (e) {
        if (timer) clearTimeout(timer);
        throw e;
      });
  }

  function _remoteFirst(p) {
    // 1) GitHub raw (主源)
    return _fetchRemote(GH_RAW + p)
      .then(function (j) {
        if (window.dataOrigin !== 'github') {
          window.dataOrigin = 'github';
          var el = document.getElementById('dataSourceTag');
          if (el) {
            el.textContent = '⛓️ GitHub源';
            el.title = '数据实时来自 GitHub 仓库 (CI 每 10 分钟更新)';
            el.style.display = 'inline-block';
          }
        }
        return j;
      })
      .catch(function () {
        // 2) gitcode 镜像 (GitHub 不可达时)
        return _fetchRemote(GC_RAW + p).catch(function () { return null; });
      });
  }

  // 覆盖 fetchJSON: 远端优先, 本地兜底
  window.fetchJSON = function (p) {
    // 本地路径也要尝试 (作为兜底), 但优先级低于远端
    return _remoteFirst(p).then(function (j) {
      if (j) return j;
      // 远端全失败 → 本地兜底 (REMOTE_ONLY 的文件本地没有, 直接 null)
      if (REMOTE_ONLY.test(p)) return null;
      return _orig(p);
    });
  };

  // 分钟级数据专用: 远端 + no-store, 失败不兜底本地 (避免读到几分钟前的旧值)
  var _origMinute = window.fetchJSON;
  window.fetchJSONMinute = function (p) {
    return _fetchRemote(GH_RAW + p, 8000)
      .catch(function () { return _fetchRemote(GC_RAW + p, 8000); });
  };

  console.log('[data_origin] 已切换为 GitHub 优先 / 本地兜底');
})();
"""


def main():
    p = os.path.join(ROOT, "data_origin_patch.js")
    with open(p, "w", encoding="utf-8") as f:
        f.write(PATCH.strip() + "\n")
    print(f"[写入] {p} ({os.path.getsize(p)}B)")

    # index.html 引入 (在 realtime.js 之后)
    ip = os.path.join(ROOT, "index.html")
    with open(ip, encoding="utf-8") as f:
        html = f.read()
    if "data_origin_patch.js" in html:
        print("[skip] index.html 已引入 data_origin_patch.js")
        return
    # 找 stock-detail.js 或 realtime.js 的 script 标签, 插到它后面
    import re
    m = re.search(r"(<script[^>]*src=\"(?:realtime|stock-detail)\.js[^>]*></script>)", html)
    if m:
        html = html.replace(m.group(1), m.group(1) + "\n<script src=\"data_origin_patch.js\"></script>")
        with open(ip, "w", encoding="utf-8") as f:
            f.write(html)
        print("[写入] index.html 已引入 data_origin_patch.js")
    else:
        print("[warn] index.html 未找到 realtime/stock-detail script 标签, 需手工加", file=sys.stderr)


if __name__ == "__main__":
    import sys
    main()
