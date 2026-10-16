/**
 * gh_sources.js — 多 GitHub 账号仓配置 + 降级读取
 *
 * 背景: 单个仓的 Actions schedule cron 最短间隔是 5 分钟 (GitHub 官方硬限制)。
 *       三个账号的仓各自错峰跑 (mod 5 分别取 0/2/4), 前端按「取最新 ts」
 *       依次读三个 raw, 就能拿到 2 分钟粒度的数据。
 *
 * 附带收益:
 *   - 单点故障容错: A 仓 workflow 挂了, B/C 继续供数
 *   - 防 schedule 自动禁用: GitHub 会禁用公开仓 60 天无活动的 schedule,
 *     三仓都有真实提交活动, 且互相作为对方的热源
 *
 * 账号 B / C 的仓建好后把 owner 填进来即可, 空数组会自动跳过。
 */
(function (global) {
  "use strict";

  // A 是唯一站点与唯一事实源 (main 分支), B/C 是采集镜像节点 (见 scripts/deploy_mirror.py)。
  // 三仓 cron 错峰 (A=mod5=2, B=mod5=4, C=mod5=0) → 任意 2 分钟窗口内
  // 必有一个仓刚跑完, 前端按「取 ts 最新」读取即可突破单仓 5 分钟下限。
  // C 仓 (dufuwuxin/stocks-ai-view-c) 只采集不同源, 不建站点。
  var REPOS = [
    { owner: "qwerzxcva", repo: "stocks-ai-view", branch: "main", label: "A" },
    { owner: "jiegengxiangjianxue", repo: "stocks-ai-view-b", branch: "main", label: "B" },
    { owner: "dufuwuxin", repo: "stocks-ai-view-c", branch: "main", label: "C" },
  ].filter(function (r) { return r.owner && r.repo; });

  var RAW = "https://raw.githubusercontent.com";

  function rawUrl(repo, path) {
    return RAW + "/" + repo.owner + "/" + repo.repo + "/" + repo.branch + "/" + path;
  }

  /** 记录每个仓最近一次成功的时间, 用于前端展示数据新鲜度 */
  var health = {};

  /**
   * 多仓降级读取: 依次尝试, 第一个成功且 ts 最新的返回。
   * @param {string} path  相对路径, 如 "data/market_now.json"
   * @param {object} opts  { freshest: true=取 ts 最新的(默认), false=取第一个成功 }
   * @returns {Promise<{data:*, source:string, ts:string}>}
   */
  function fetchMulti(path, opts) {
    opts = opts || {};
    var freshest = opts.freshest !== false;
    if (REPOS.length === 0) return Promise.reject(new Error("no repos configured"));

    var attempts = REPOS.map(function (repo) {
      var url = rawUrl(repo, path);
      return fetch(url, { cache: "no-store" })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then(function (d) {
          health[repo.label] = { ok: true, at: Date.now() };
          return { data: d, source: repo.label, ts: (d && d.ts) || "" };
        })
        .catch(function (e) {
          health[repo.label] = { ok: false, at: Date.now(), err: String(e && e.message) };
          return null;   // 降级: 这个仓失败不阻断其他仓
        });
    });

    return Promise.all(attempts).then(function (results) {
      var ok = results.filter(Boolean);
      if (ok.length === 0) throw new Error("all repos failed for " + path);
      if (freshest) {
        ok.sort(function (a, b) {
          // ts 缺失 (daily 档文件只有 date) 时按 source 固定顺序, 保证 A 优先 ——
          // 它是唯一事实源, B/C 只是错峰镜像, 不能因排序把镜像顶到前面。
          if (!a.ts && !b.ts) return a.source.localeCompare(b.source);
          if (!a.ts) return 1;
          if (!b.ts) return -1;
          return b.ts.localeCompare(a.ts);
        });
      }
      return ok[0];
    });
  }

  /** 前端展示用: 各仓健康状态 */
  function getHealth() {
    return REPOS.map(function (r) {
      return { label: r.label, repo: r.owner + "/" + r.repo,
               status: health[r.label] || { ok: null } };
    });
  }

  global.GHSources = {
    REPOS: REPOS,
    fetchMulti: fetchMulti,
    rawUrl: rawUrl,
    getHealth: getHealth,
  };
})(window);
