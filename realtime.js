// realtime.js — 浏览器直连腾讯行情的分钟级实时刷新模块
// 挂在 window 上, 由 index.html 按需引入。
//
// 为什么这样做:
//   GitHub Actions cron 最小粒度 5 分钟, 无法做到分钟级。
//   腾讯 qt.gtimg.cn 返回 Access-Control-Allow-Origin: * (实测),
//   浏览器可跨域直连 → 由客户端轮询, 粒度可到 30s~60s, 且不消耗 Actions 额度。
//
// 数据口径:
//   - 腾讯快照字段已实测核对: p[3]=现价 p[4]=昨收 p[32]=涨跌幅% p[38]=换手率%
//     p[39]=PE(TTM) p[44]=总市值(亿) p[45]=流通市值(亿) p[46]=PB p[30]=时间
//   - 分时数据: minute/query 返回 ["0930 3806.98 5231446 6745515551.20", ...]
//     (时间 价格 成交量手 成交额元)

(function () {
  'use strict';

  var UP = '#f85149', DN = '#3fb950', FLAT = '#8b949e';

  var RT = {
    // 指数清单 (腾讯代码 → 显示名)
    indices: [
      ['sh000001', '上证指数'], ['sz399001', '深证成指'], ['sh000300', '沪深300'],
      ['sz399006', '创业板指'], ['sh000688', '科创50'], ['bj899050', '北证50']
    ],
    pollMs: 60000,          // 轮询间隔 (60s; 交易时段内)
    offHoursPollMs: 300000, // 非交易时段 (5min, 省请求)
    timer: null,
    lastOk: 0,
    failCount: 0,
    // 交易时段判断 (CST, 含盘前/盘后缓冲)
    inSession: function () {
      var now = new Date();
      // 转 CST
      var cst = new Date(now.getTime() + (now.getTimezoneOffset() + 480) * 60000);
      var day = cst.getDay();          // 0=周日
      if (day === 0 || day === 6) return false;
      var h = cst.getHours(), m = cst.getMinutes();
      var mins = h * 60 + m;
      // 09:15 - 15:05 (含开盘集合竞价与收盘后缓冲)
      return mins >= 555 && mins <= 905;
    }
  };

  // ─── 拉取腾讯实时快照 ─────────────────────────────────────────
  function fetchQuotes(codes) {
    var url = 'https://qt.gtimg.cn/q=' + codes.join(',');
    return fetch(url).then(function (r) { return r.text(); }).then(function (txt) {
      var out = {};
      // 腾讯返回 GBK 编码, fetch 按 UTF-8 解码会乱码 → 手动转码
      var bytes = new Uint8Array(txt.length);
      for (var i = 0; i < txt.length; i++) bytes[i] = txt.charCodeAt(i) & 0xff;
      var gbkText;
      try {
        gbkText = new TextDecoder('gbk').decode(bytes);
      } catch (e) {
        gbkText = txt;   // 解码器不可用时退化为原文本 (名称可能乱码, 数值不受影响)
      }
      var re = /v_([a-z0-9]+)="([^"]*)"/gi, m;
      while ((m = re.exec(gbkText)) !== null) {
        var p = m[2].split('~');
        if (p.length < 40) continue;
        out[m[1]] = {
          code: m[1],
          name: p[1],
          price: parseFloat(p[3]) || 0,
          prev: parseFloat(p[4]) || 0,
          open: parseFloat(p[5]) || 0,
          high: parseFloat(p[33]) || 0,
          low: parseFloat(p[34]) || 0,
          chg_pct: parseFloat(p[32]) || 0,
          turnover: parseFloat(p[38]) || 0,
          vol_ratio: 1.0,               // 量比无独立字段, 见 ai_screener 注释
          mktcap: (parseFloat(p[44]) || 0) * 1e8,   // 亿元 → 元
          pe: parseFloat(p[39]) || 0,
          pb: parseFloat(p[46]) || 0,
          time: p[30] || ''
        };
      }
      return out;
    }).catch(function (e) {
      console.warn('[rt] quote fetch failed:', e);
      return null;
    });
  }

  // ─── 拉取分时数据 (1min 粒度) ────────────────────────────────
  function fetchMinute(code) {
    var url = 'https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=' + code;
    return fetch(url).then(function (r) { return r.json(); }).then(function (d) {
      var node = (d && d.data && d.data[code] && d.data[code].data) || {};
      var arr = node.data || node['data'] || [];
      return arr.map(function (row) {
        var f = String(row).split(' ');
        return { t: f[0], price: parseFloat(f[1]) || 0, vol: parseFloat(f[2]) || 0 };
      }).filter(function (x) { return x.price > 0; });
    }).catch(function (e) {
      console.warn('[rt] minute fetch failed:', e);
      return [];
    });
  }

  // ─── 渲染: 更新 KPI 条里的指数 ───────────────────────────────
  function renderIndices(quotes) {
    var box = document.getElementById('kpis');
    if (!box) return;
    var html = '';
    RT.indices.forEach(function (pair) {
      var q = quotes[pair[0]];
      if (!q) return;
      var cls = q.chg_pct > 0 ? 'up' : q.chg_pct < 0 ? 'dn' : 'fl';
      var col = q.chg_pct > 0 ? UP : q.chg_pct < 0 ? DN : FLAT;
      html += '<div class="kpi"><div class="n">' + pair[1] + '</div>' +
              '<div class="v ' + cls + '">' + q.price.toFixed(2) + '</div>' +
              '<div class="c ' + cls + '" style="color:' + col + '">' +
              (q.chg_pct >= 0 ? '+' : '') + q.chg_pct.toFixed(2) + '%</div></div>';
    });
    if (html) {
      // 保留原有后 4 个 KPI (涨停/跌停/炸板/封板率)
      var keep = box.querySelectorAll('.kpi');
      for (var i = 4; i < keep.length; i++) html += keep[i].outerHTML;
      box.innerHTML = html;
    }
  }

  // ─── 渲染: 实时状态角标 ──────────────────────────────────────
  function renderStatus(ok, ts) {
    var el = document.getElementById('rtStatus');
    if (!el) return;
    if (ok) {
      el.textContent = '实时 ' + (ts || '').slice(8, 10) + ':' + (ts || '').slice(10, 12);
      el.style.color = UP;
      el.title = '浏览器直连腾讯行情, ' + RT.pollMs / 1000 + 's 刷新';
    } else {
      el.textContent = '实时离线';
      el.style.color = FLAT;
    }
  }

  // ─── 主轮询 ─────────────────────────────────────────────────
  function poll() {
    var codes = RT.indices.map(function (x) { return x[0]; });
    fetchQuotes(codes).then(function (quotes) {
      if (!quotes || !Object.keys(quotes).length) {
        RT.failCount++;
        renderStatus(false);
        return;
      }
      RT.failCount = 0;
      RT.lastOk = Date.now();
      renderIndices(quotes);
      var any = quotes[codes[0]] || quotes[codes[1]];
      renderStatus(true, any && any.time);
      // 供其他模块复用
      window._rtQuotes = quotes;
      document.dispatchEvent(new CustomEvent('rt:quotes', { detail: quotes }));
    });
  }

  function schedule() {
    if (RT.timer) clearTimeout(RT.timer);
    // 失败退避: 最多退到 5min
    var delay = RT.failCount > 0
      ? Math.min(RT.pollMs * Math.pow(1.5, RT.failCount), 300000)
      : (RT.inSession() ? RT.pollMs : RT.offHoursPollMs);
    RT.timer = setTimeout(function () { poll(); schedule(); }, delay);
  }

  RT.start = function () {
    if (RT.timer) return;
    console.log('[rt] start, session=' + RT.inSession() + ', poll=' + RT.pollMs + 'ms');
    poll();
    schedule();
  };
  RT.stop = function () {
    if (RT.timer) clearTimeout(RT.timer);
    RT.timer = null;
  };
  RT.fetchMinute = fetchMinute;
  RT.fetchQuotes = fetchQuotes;

  window.StocksRealtime = RT;

  // 页面加载后自动启动
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', RT.start);
  } else {
    RT.start();
  }
})();
