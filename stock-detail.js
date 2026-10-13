// stock-detail.js — 个股详情: 真实K线 + 资金流 + 分时 (浏览器直连, 零后端)
//
// 数据源 (全部实测可达, 且 Access-Control-Allow-Origin: * 可跨域直连):
//   日K(qfq前复权)  web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq
//   日K(不复权)     web.ifzq.gtimg.cn/appstock/app/kline/kline?param={code},day,,,{n}
//   分时(1min)      web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}
//   资金流(分档)    push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?secid={mkt}.{bare}&lmt={n}&klt=101
//   实时快照        qt.gtimg.cn/q={code}
//
// 资金流字段 (实测 2026-10-13, 贵州茅台):
//   date, main_net, small, medium, large, xlarge,
//   main_pct, small_pct, medium_pct, large_pct, xlarge_pct, close, ...
// 单位: 元 (负数为流出)

(function () {
  'use strict';

  var UP = '#f85149', DN = '#3fb950', AMBER = '#d29922', ACCENT = '#8957e5', BLUE = '#58a6ff';

  function prefixed(code) {
    var c = String(code).replace(/\D/g, '').padStart(6, '0');
    if (/^(60|68|90|11|13)/.test(c)) return 'sh' + c;
    if (/^(43|83|87|88|92)/.test(c)) return 'bj' + c;
    return 'sz' + c;
  }

  function secid(code) {
    var p = prefixed(code);
    return (p.slice(0, 2) === 'sh' ? '1.' : '0.') + p.slice(2);
  }

  function jget(url) {
    return fetch(url).then(function (r) { return r.json(); }).catch(function () { return null; });
  }

  // ─── 数据获取 ───────────────────────────────────────────────
  // 周期: day/week/month/year/year → 腾讯 fqkline 参数
  function fetchKline(code, n, period) {
    var p = prefixed(code);
    var klt = period || 'day';
    var cnt = n || 60;
    if (klt !== 'day') cnt = Math.min(cnt, 200);
    return jget('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param='
                + p + ',' + klt + ',,' + (klt === 'day' ? ',' : ',,') + cnt + ',qfq')
      .then(function (d) {
        var node = (d && d.data && d.data[p]) || {};
        var key = klt === 'day' ? 'qfqday' : 'qfq' + klt;
        var arr = node[key] || node.qfqday || node.day || [];
        return arr.map(function (r) {
          return { date: r[0], open: +r[1], close: +r[2], high: +r[3], low: +r[4], vol: +r[5] };
        });
      });
  }

  function fetchFundFlow(code, n) {
    return jget('https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
                + '?secid=' + secid(code) + '&lmt=' + (n || 30) + '&klt=101'
                + '&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65')
      .then(function (d) {
        var klines = ((d || {}).data || {}).klines || [];
        return klines.map(function (row) {
          var f = row.split(',');
          return {
            date: f[0],
            main_net: +f[1],      // 主力净流入 (元)
            small: +f[2],         // 小单净流入
            medium: +f[3],        // 中单净流入
            large: +f[4],         // 大单净流入
            xlarge: +f[5],        // 超大单净流入
            main_pct: +f[6],      // 主力净占比 %
            close: +f.length > 11 ? +f[11] : 0,
          };
        });
      });
  }

  function fetchMinute(code) {
    var p = prefixed(code);
    return jget('https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=' + p)
      .then(function (d) {
        var node = (d && d.data && d.data[p]) || {};
        // 实测结构: data[p].data.data = ['0930 1287.00 201 25868700.00', ...]
        // 兼容: data[p].data 直接是数组的情况
        var arr = [];
        if (Array.isArray(node.data)) {
          arr = node.data;
        } else if (node.data && Array.isArray(node.data.data)) {
          arr = node.data.data;
        } else if (Array.isArray(node.data)) {
          arr = node.data;
        }
        return arr.map(function (row) {
          var f = String(row).split(' ');
          return { t: f[0], price: +f[1], vol: +f[2], amt: +f[3] };
        }).filter(function (x) { return x.price > 0; });
      });
  }

  // 腾讯行情是 GBK 编码, fetch().json() 按 UTF-8 解析会失败, 需手动解码
  function fetchQuote(code) {
    var p = prefixed(code);
    return fetch('https://qt.gtimg.cn/q=' + p)
      .then(function (r) { return r.arrayBuffer(); })
      .then(function (buf) {
        var txt;
        try { txt = new TextDecoder('gbk').decode(buf); } catch (e) { txt = ''; }
        var m = txt.match(/v_([a-z0-9]+)="([^"]*)"/);
        if (!m) return null;
        var f = m[2].split('~');
        if (f.length < 40) return null;
        return {
          code: code, name: f[1], price: +f[3], prev_close: +f[4], open: +f[5],
          high: +f[33], low: +f[34], chg_pct: +f[32], turnover: +f[38],
          pe: +f[39], mktcap: (+f[44] || 0) * 1e8, pb: +f[46], time: f[30]
        };
      })
      .catch(function () { return null; });
  }

  // ─── 渲染: K线图 (SVG 蜡烛) ──────────────────────────────────
  function renderKlineChart(bars) {
    if (!bars || bars.length < 5) return '<div class="fl">K线数据不足</div>';
    var W = 460, H = 190, PL = 6, PR = 52, PT = 8, PB = 18;
    var cw = W - PL - PR, ch = H - PT - PB;
    var hi = -1e18, lo = 1e18;
    bars.forEach(function (b) { hi = Math.max(hi, b.high); lo = Math.min(lo, b.low); });
    var rng = (hi - lo) || 1;
    var bw = cw / bars.length;
    var out = '';
    bars.forEach(function (b, i) {
      var x = PL + i * bw;
      var up = b.close >= b.open;
      var col = up ? UP : DN;
      var yO = PT + (hi - b.open) / rng * ch;
      var yC = PT + (hi - b.close) / rng * ch;
      var yH = PT + (hi - b.high) / rng * ch;
      var yL = PT + (hi - b.low) / rng * ch;
      var bodyTop = Math.min(yO, yC), bodyH = Math.max(1, Math.abs(yC - yO));
      out += '<line x1="' + (x + bw / 2).toFixed(1) + '" y1="' + yH.toFixed(1) + '" x2="' + (x + bw / 2).toFixed(1) + '" y2="' + yL.toFixed(1) + '" stroke="' + col + '" stroke-width="1"/>';
      out += '<rect x="' + (x + bw * 0.15).toFixed(1) + '" y="' + bodyTop.toFixed(1) + '" width="' + (bw * 0.7).toFixed(1) + '" height="' + bodyH.toFixed(1) + '" fill="' + col + '"/>';
    });
    // 右侧价格轴
    for (var g = 0; g <= 4; g++) {
      var v = hi - rng * g / 4;
      var y = PT + ch * g / 4;
      out += '<line x1="' + PL + '" y1="' + y.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + y.toFixed(1) + '" stroke="#1e2738" stroke-width="0.5"/>';
      out += '<text x="' + (W - PR + 4) + '" y="' + (y + 3).toFixed(1) + '" fill="#586069" font-size="8">' + v.toFixed(1) + '</text>';
    }
    // 日期轴
    out += '<text x="' + PL + '" y="' + (H - 5) + '" fill="#586069" font-size="8">' + bars[0].date.slice(5) + '</text>';
    out += '<text x="' + (W - PR - 30) + '" y="' + (H - 5) + '" fill="#586069" font-size="8">' + bars[bars.length - 1].date.slice(5) + '</text>';
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:190px;display:block">' + out + '</svg>';
  }

  // ─── 渲染: 资金流图 (SVG 柱) ────────────────────────────────
  function renderFundChart(flows) {
    if (!flows || !flows.length) return '<div class="fl">资金流数据不可用</div>';
    var W = 460, H = 140, PL = 6, PR = 52, PT = 8, PB = 18;
    var cw = W - PL - PR, ch = H - PT - PB;
    var mx = 0;
    flows.forEach(function (f) { mx = Math.max(mx, Math.abs(f.main_net)); });
    mx = mx || 1;
    var bw = cw / flows.length;
    var out = '';
    // 零轴
    var y0 = PT + ch / 2;
    out += '<line x1="' + PL + '" y1="' + y0.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + y0.toFixed(1) + '" stroke="#263044" stroke-width="1"/>';
    flows.forEach(function (f, i) {
      var x = PL + i * bw;
      var h = Math.abs(f.main_net) / mx * (ch / 2);
      var up = f.main_net >= 0;
      var y = up ? y0 - h : y0;
      out += '<rect x="' + (x + bw * 0.15).toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + (bw * 0.7).toFixed(1) + '" height="' + Math.max(1, h).toFixed(1) + '" fill="' + (up ? UP : DN) + '" opacity="0.85"/>';
    });
    // 量程标注
    out += '<text x="' + (W - PR + 4) + '" y="' + (PT + 8) + '" fill="#586069" font-size="8">+' + (mx / 1e8).toFixed(1) + '亿</text>';
    out += '<text x="' + (W - PR + 4) + '" y="' + (H - PB) + '" fill="#586069" font-size="8">-' + (mx / 1e8).toFixed(1) + '亿</text>';
    out += '<text x="' + PL + '" y="' + (H - 5) + '" fill="#586069" font-size="8">' + flows[0].date.slice(5) + '</text>';
    out += '<text x="' + (W - PR - 30) + '" y="' + (H - 5) + '" fill="#586069" font-size="8">' + flows[flows.length - 1].date.slice(5) + '</text>';
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:140px;display:block">' + out + '</svg>';
  }

  // ─── 渲染: 分时图 ───────────────────────────────────────────
  function renderMinuteChart(mins) {
    if (!mins || mins.length < 5) return '<div class="fl">分时数据不足</div>';
    var W = 460, H = 130, PL = 6, PR = 52, PT = 8, PB = 18;
    var cw = W - PL - PR, ch = H - PT - PB;
    var base = mins[0].price;
    var hi = -1e18, lo = 1e18;
    mins.forEach(function (m) { hi = Math.max(hi, m.price); lo = Math.min(lo, m.price); });
    var rng = (hi - lo) || 1;
    var bw = cw / mins.length;
    var pts = [];
    mins.forEach(function (m, i) {
      pts.push((PL + i * bw + bw / 2).toFixed(1) + ',' + (PT + (hi - m.price) / rng * ch).toFixed(1));
    });
    var yBase = PT + (hi - base) / rng * ch;
    var out = '<line x1="' + PL + '" y1="' + yBase.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + yBase.toFixed(1) + '" stroke="#263044" stroke-width="1" stroke-dasharray="3 2"/>';
    out += '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' + BLUE + '" stroke-width="1.2"/>';
    out += '<text x="' + (W - PR + 4) + '" y="' + (PT + 8) + '" fill="#586069" font-size="8">' + hi.toFixed(2) + '</text>';
    out += '<text x="' + (W - PR + 4) + '" y="' + (H - PB) + '" fill="#586069" font-size="8">' + lo.toFixed(2) + '</text>';
    out += '<text x="' + (W - PR + 4) + '" y="' + (yBase + 3).toFixed(1) + '" fill="#586069" font-size="8">' + base.toFixed(2) + '</text>';
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:130px;display:block">' + out + '</svg>';
  }

  function fmtYi(v) {
    if (v == null) return '—';
    var a = Math.abs(v);
    if (a >= 1e8) return (v / 1e8).toFixed(2) + '亿';
    if (a >= 1e4) return (v / 1e4).toFixed(0) + '万';
    return v.toFixed(0);
  }

  var PERIODS = [{ k: 'day', label: '日K' }, { k: 'week', label: '周K' },
                 { k: 'month', label: '月K' }, { k: 'year', label: '年K' }];
  var curPeriod = 'day';

  // ─── 主渲染 ─────────────────────────────────────────────────
  function render(code) {
    var det = document.getElementById('symDetail');
    if (!det) return;
    det.style.display = 'block';
    curPeriod = 'day';
    det.innerHTML = '<b style="font-size:13px">' + code + '</b> ' +
      '<span onclick="document.getElementById(\'symDetail\').style.display=\'none\'" style="float:right;cursor:pointer;color:var(--tx3)">✕</span>' +
      '<br><span class="fl">加载中… (腾讯K线 + 东财资金流)</span>';

    Promise.all([
      fetchQuote(code), fetchKline(code, 60, curPeriod), fetchFundFlow(code, 30), fetchMinute(code)
    ]).then(function (res) {
      var q = res[0], bars = res[1], flows = res[2], mins = res[3];
      var h = '';
      if (q) {
        h += '<b style="font-size:14px">' + q.name + ' (' + q.code + ')</b> ' +
          '<span class="fl" style="font-size:9px">' + (q.time || '') + '</span> ' +
          '<span onclick="document.getElementById(\'symDetail\').style.display=\'none\'" style="float:right;cursor:pointer;color:var(--tx3)">✕</span><br>';
        var col = q.chg_pct >= 0 ? 'var(--up)' : 'var(--dn)';
        h += '<span style="font-size:18px;font-weight:700;color:' + col + '">' + q.price.toFixed(2) + '</span> ' +
          '<span style="color:' + col + '">' + (q.chg_pct >= 0 ? '+' : '') + q.chg_pct.toFixed(2) + '%</span> ';
        h += '<span class="fl" style="font-size:10px">换手' + q.turnover.toFixed(2) + '% ' +
          'PE' + (q.pe > 0 ? q.pe.toFixed(1) : '亏') + ' ' +
          '市值' + (q.mktcap / 1e8).toFixed(0) + '亿 PB' + (q.pb > 0 ? q.pb.toFixed(1) : '—') + '</span><br>';
        h += '<span class="fl" style="font-size:9px">数据源: 腾讯 qt.gtimg.cn 实时 · 东财资金流</span><hr style="border:0;border-top:1px solid var(--border);margin:6px 0">';
      }
      // 分时
      h += '<div class="fl" style="font-size:9px;margin-bottom:2px">分时 (1分钟, ' + (mins ? mins.length : 0) + '点)</div>';
      h += renderMinuteChart(mins);
      // K线 + 周期切换
      h += '<div style="display:flex;align-items:center;gap:4px;margin:6px 0 2px">' +
           '<span class="fl" style="font-size:9px">K线 (前复权, ' + (bars ? bars.length : 0) + '根)</span>';
      PERIODS.forEach(function (p) {
        var on = p.k === curPeriod;
        h += '<button data-klt="' + p.k + '" style="font-size:9px;padding:1px 6px;border-radius:8px;cursor:pointer;' +
             'border:1px solid ' + (on ? 'var(--accent)' : 'var(--border)') + ';' +
             'background:' + (on ? 'rgba(137,87,229,.2)' : 'transparent') + ';' +
             'color:' + (on ? 'var(--accent)' : 'var(--tx3)') + ';font-weight:700">' + p.label + '</button>';
      });
      h += '</div>';
      h += '<div id="klineChartBox">' + renderKlineChart(bars) + '</div>';
      // 资金流
      h += '<div class="fl" style="font-size:9px;margin:6px 0 2px">主力资金流 (近' + (flows ? flows.length : 0) + '日, 净额) · 数据源: 东财 push2his</div>';
      h += '<div id="flowChartBox">' + renderFundChart(flows) + '</div>';
      // 净流入/净流出汇总
      if (flows && flows.length) {
        var inflow = flows.filter(function (f) { return f.main_net > 0; })
                          .reduce(function (s, f) { return s + f.main_net; }, 0);
        var outflow = flows.filter(function (f) { return f.main_net < 0; })
                           .reduce(function (s, f) { return s + f.main_net; }, 0);
        h += '<div style="display:flex;gap:10px;font-size:10px;margin:4px 0">' +
             '<span>主力净流入 <b class="up">' + fmtYi(inflow) + '</b></span>' +
             '<span>主力净流出 <b class="dn">' + fmtYi(outflow) + '</b></span>' +
             '<span>净额 <b class="' + (inflow + outflow >= 0 ? 'up' : 'dn') + '">' + fmtYi(inflow + outflow) + '</b></span></div>';
        // 资金流明细表
        h += '<table class="pool-table" style="margin-top:6px"><thead><tr><th>日期</th><th>主力净额</th><th>占比%</th><th>超大单</th><th>大单</th><th>中单</th><th>小单</th><th>收盘</th></tr></thead><tbody>';
        flows.slice(0, 12).forEach(function (f) {
          var c = f.main_net >= 0 ? 'up' : 'dn';
          h += '<tr><td>' + f.date.slice(5) + '</td>' +
            '<td class="' + c + '">' + fmtYi(f.main_net) + '</td>' +
            '<td class="' + c + '">' + f.main_pct.toFixed(2) + '</td>' +
            '<td class="' + (f.xlarge >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.xlarge) + '</td>' +
            '<td class="' + (f.large >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.large) + '</td>' +
            '<td class="' + (f.medium >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.medium) + '</td>' +
            '<td class="' + (f.small >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.small) + '</td>' +
            '<td>' + (f.close ? f.close.toFixed(2) : '—') + '</td></tr>';
        });
        h += '</tbody></table>';
        // 5日累计
        var sum5 = flows.slice(0, 5).reduce(function (s, f) { return s + f.main_net; }, 0);
        h += '<div class="fl" style="font-size:9px;margin-top:4px">近5日主力净额: <b class="' + (sum5 >= 0 ? 'up' : 'dn') + '">' + fmtYi(sum5) + '</b> · 仅供研究参考, 不构成投资建议</div>';
      }
      det.innerHTML = h;
      // 周期切换按钮事件
      Array.prototype.forEach.call(det.querySelectorAll('button[data-klt]'), function (btn) {
        btn.onclick = function () {
          curPeriod = btn.dataset.klt;
          var box = document.getElementById('klineChartBox');
          if (box) box.innerHTML = '<span class="fl" style="font-size:9px">加载中…</span>';
          fetchKline(code, 60, curPeriod).then(function (nb) {
            if (box) box.innerHTML = renderKlineChart(nb);
            var lab = det.querySelector('#klineCount');
            if (lab) lab.textContent = nb.length + '根';
            // 刷新按钮态
            Array.prototype.forEach.call(det.querySelectorAll('button[data-klt]'), function (b2) {
              var on2 = b2.dataset.klt === curPeriod;
              b2.style.border = '1px solid ' + (on2 ? 'var(--accent)' : 'var(--border)');
              b2.style.background = on2 ? 'rgba(137,87,229,.2)' : 'transparent';
              b2.style.color = on2 ? 'var(--accent)' : 'var(--tx3)';
            });
          });
        };
      });
    });
  }

  window.StockDetail = {
    render: render,
    fetchKline: fetchKline,
    fetchFundFlow: fetchFundFlow,
    fetchMinute: fetchMinute,
    fetchQuote: fetchQuote
  };
})();
