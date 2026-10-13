// stock-detail.js — Per-stock detail panel: K-line, fund flow, minute line.
//
// Data priority (cloud-first, local copy removed 2026-10-14):
//   1. Cloud warehouse via window.DB  (jiegengxiangjianxue*/db-stocks-*)
//   2. Direct provider calls          (tencent kline, eastmoney fflow)
// The warehouse gives full history + five-tier flow; the direct calls give
// the freshest snapshot. Both are CORS-open.
(function () {
  'use strict';

  var UP = '#f85149', DN = '#3fb950', BLUE = '#58a6ff';

  function prefixed(code) {
    var c = String(code).replace(/\D/g, '').padStart(6, '0');
    if (/^(60|68|90|11|13)/.test(c)) return 'sh' + c;
    if (/^(43|83|87|88|92)/.test(c)) return 'bj' + c;
    return 'sz' + c;
  }

  function bare(code) {
    var p = prefixed(code);
    return p.slice(2);
  }

  function jget(url) {
    return fetch(url).then(function (r) { return r.json(); })
      .catch(function () { return null; });
  }

  // ─── data sources ───────────────────────────────────────────
  function fetchKline(code, n, period) {
    var p = prefixed(code);
    var klt = period || 'day';
    var cnt = n || 60;
    var url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param='
      + p + ',' + klt + ',,' + (klt === 'day' ? ',' : ',,') + cnt + ',qfq';
    return jget(url).then(function (d) {
      var node = (d && d.data && d.data[p]) || {};
      var key = klt === 'day' ? 'qfqday' : (klt === 'year' ? 'year' : 'qfq' + klt);
      var arr = node[key] || node.qfqday || node.day || [];
      return arr.map(function (r) {
        return { date: r[0], open: +r[1], close: +r[2], high: +r[3], low: +r[4], vol: +r[5] };
      });
    });
  }

  function fetchFundFlow(code, n) {
    var b = bare(code);
    var secid = (prefixed(code).slice(0, 2) === 'sh' ? '1.' : '0.') + b;
    return jget('https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get'
      + '?secid=' + secid + '&lmt=' + (n || 30) + '&klt=101'
      + '&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65')
      .then(function (d) {
        var klines = ((d || {}).data || {}).klines || [];
        return klines.map(function (row) {
          var f = row.split(',');
          return {
            date: f[0],
            main_net: +f[1], small: +f[2], medium: +f[3],
            large: +f[4], xlarge: +f[5],
            main_pct: +f[6],
            close: f.length > 11 ? +f[11] : 0
          };
        });
      });
  }

  function fetchMinute(code) {
    var p = prefixed(code);
    return jget('https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=' + p)
      .then(function (d) {
        var node = (d && d.data && d.data[p]) || {};
        var arr = [];
        if (Array.isArray(node.data)) arr = node.data;
        else if (node.data && Array.isArray(node.data.data)) arr = node.data.data;
        return arr.map(function (row) {
          var f = String(row).split(' ');
          return { t: f[0], price: +f[1], vol: +f[2], amt: +f[3] };
        }).filter(function (x) { return x.price > 0; });
      });
  }

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

  // ─── charts ─────────────────────────────────────────────────
  function renderKlineChart(bars) {
    if (!bars || bars.length < 5) return '<div class="fl">K-line data unavailable</div>';
    var W = 460, H = 190, PL = 6, PR = 52, PT = 8, PB = 18;
    var cw = W - PL - PR, ch = H - PT - PB;
    var hi = -1e18, lo = 1e18;
    bars.forEach(function (b) { hi = Math.max(hi, b.high); lo = Math.min(lo, b.low); });
    var rng = (hi - lo) || 1;
    var bw = cw / bars.length;
    var out = '';
    bars.forEach(function (b, i) {
      var x = PL + i * bw;
      var col = b.close >= b.open ? UP : DN;
      var yO = PT + (hi - b.open) / rng * ch;
      var yC = PT + (hi - b.close) / rng * ch;
      out += '<line x1="' + (x + bw / 2).toFixed(1) + '" y1="' + (PT + (hi - b.high) / rng * ch).toFixed(1)
        + '" x2="' + (x + bw / 2).toFixed(1) + '" y2="' + (PT + (hi - b.low) / rng * ch).toFixed(1)
        + '" stroke="' + col + '" stroke-width="1"/>';
      var top = Math.min(yO, yC), h = Math.max(1, Math.abs(yC - yO));
      out += '<rect x="' + (x + bw * 0.15).toFixed(1) + '" y="' + top.toFixed(1)
        + '" width="' + (bw * 0.7).toFixed(1) + '" height="' + h.toFixed(1) + '" fill="' + col + '"/>';
    });
    for (var g = 0; g <= 4; g++) {
      var v = hi - rng * g / 4, y = PT + ch * g / 4;
      out += '<line x1="' + PL + '" y1="' + y.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + y.toFixed(1)
        + '" stroke="#1e2738" stroke-width="0.5"/>';
      out += '<text x="' + (W - PR + 4) + '" y="' + (y + 3).toFixed(1) + '" fill="#586069" font-size="8">'
        + v.toFixed(1) + '</text>';
    }
    out += '<text x="' + PL + '" y="' + (H - 5) + '" fill="#586069" font-size="8">' + bars[0].date.slice(5) + '</text>';
    out += '<text x="' + (W - PR - 30) + '" y="' + (H - 5) + '" fill="#586069" font-size="8">'
      + bars[bars.length - 1].date.slice(5) + '</text>';
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:190px;display:block">' + out + '</svg>';
  }

  function renderFundChart(flows) {
    if (!flows || !flows.length) return '<div class="fl">Fund flow unavailable</div>';
    var W = 460, H = 140, PL = 6, PR = 52, PT = 8, PB = 18;
    var cw = W - PL - PR, ch = H - PT - PB;
    var mx = 0;
    flows.forEach(function (f) { mx = Math.max(mx, Math.abs(f.main_net)); });
    mx = mx || 1;
    var bw = cw / flows.length;
    var y0 = PT + ch / 2;
    var out = '<line x1="' + PL + '" y1="' + y0.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + y0.toFixed(1)
      + '" stroke="#263044" stroke-width="1"/>';
    flows.forEach(function (f, i) {
      var x = PL + i * bw;
      var h = Math.abs(f.main_net) / mx * (ch / 2);
      var up = f.main_net >= 0;
      out += '<rect x="' + (x + bw * 0.15).toFixed(1) + '" y="' + (up ? y0 - h : y0).toFixed(1)
        + '" width="' + (bw * 0.7).toFixed(1) + '" height="' + Math.max(1, h).toFixed(1)
        + '" fill="' + (up ? UP : DN) + '" opacity="0.85"/>';
    });
    out += '<text x="' + (W - PR + 4) + '" y="' + (PT + 8) + '" fill="#586069" font-size="8">+' + (mx / 1e8).toFixed(1) + 'Yi</text>';
    out += '<text x="' + (W - PR + 4) + '" y="' + (H - PB) + '" fill="#586069" font-size="8">-' + (mx / 1e8).toFixed(1) + 'Yi</text>';
    out += '<text x="' + PL + '" y="' + (H - 5) + '" fill="#586069" font-size="8">' + flows[0].date.slice(5) + '</text>';
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:140px;display:block">' + out + '</svg>';
  }

  function renderMinuteChart(mins) {
    if (!mins || mins.length < 5) return '<div class="fl">Minute data unavailable</div>';
    var W = 460, H = 130, PL = 6, PR = 52, PT = 8, PB = 18;
    var cw = W - PL - PR, ch = H - PT - PB;
    var base = mins[0].price;
    var hi = -1e18, lo = 1e18;
    mins.forEach(function (m) { hi = Math.max(hi, m.price); lo = Math.min(lo, m.low); });
    var rng = (hi - lo) || 1;
    var bw = cw / mins.length;
    var pts = [];
    mins.forEach(function (m, i) {
      pts.push((PL + i * bw + bw / 2).toFixed(1) + ',' + (PT + (hi - m.price) / rng * ch).toFixed(1));
    });
    var yBase = PT + (hi - base) / rng * ch;
    var out = '<line x1="' + PL + '" y1="' + yBase.toFixed(1) + '" x2="' + (W - PR) + '" y2="' + yBase.toFixed(1)
      + '" stroke="#263044" stroke-width="1" stroke-dasharray="3 2"/>';
    out += '<polyline points="' + pts.join(' ') + '" fill="none" stroke="' + BLUE + '" stroke-width="1.2"/>';
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:130px;display:block">' + out + '</svg>';
  }

  function fmtYi(v) {
    if (v == null) return '-';
    var a = Math.abs(v);
    if (a >= 1e8) return (v / 1e8).toFixed(2) + 'Yi';
    if (a >= 1e4) return (v / 1e4).toFixed(0) + 'W';
    return v.toFixed(0);
  }

  var PERIODS = [{ k: 'day', label: 'D' }, { k: 'week', label: 'W' },
  { k: 'month', label: 'M' }, { k: 'year', label: 'Y' }];
  var curPeriod = 'day';

  function render(code) {
    var det = document.getElementById('symDetail');
    if (!det) return;
    det.style.display = 'block';
    curPeriod = 'day';
    det.innerHTML = '<b style="font-size:13px">' + code + '</b> ' +
      '<span onclick="document.getElementById(\'symDetail\').style.display=\'none\'" ' +
      'style="float:right;cursor:pointer;color:var(--tx3)">x</span>' +
      '<br><span class="fl">Loading (tencent kline + eastmoney flow)...</span>';

    var b = bare(code);
    Promise.all([
      fetchQuote(code), fetchKline(code, 60, curPeriod),
      fetchFundFlow(code, 30), fetchMinute(code),
      window.DB ? window.DB.stockTechnical(b) : Promise.resolve(null),
      window.DB ? window.DB.stockQuote(b) : Promise.resolve(null)
    ]).then(function (res) {
      var q = res[0], bars = res[1], flows = res[2], mins = res[3], tech = res[4], wh = res[5];
      var h = '';
      if (q) {
        var col = q.chg_pct >= 0 ? 'var(--up)' : 'var(--dn)';
        h += '<b style="font-size:14px">' + q.name + ' (' + q.code + ')</b> ' +
          '<span class="fl" style="font-size:9px">' + (q.time || '') + '</span> ' +
          '<span onclick="document.getElementById(\'symDetail\').style.display=\'none\'" ' +
          'style="float:right;cursor:pointer;color:var(--tx3)">x</span><br>';
        h += '<span style="font-size:18px;font-weight:700;color:' + col + '">' + q.price.toFixed(2) + '</span> ' +
          '<span style="color:' + col + '">' + (q.chg_pct >= 0 ? '+' : '') + q.chg_pct.toFixed(2) + '%</span> ';
        h += '<span class="fl" style="font-size:10px">Turnover ' + q.turnover.toFixed(2) + '% ' +
          'PE ' + (q.pe > 0 ? q.pe.toFixed(1) : 'n/a') + ' ' +
          'Cap ' + (q.mktcap / 1e8).toFixed(0) + 'Yi PB ' + (q.pb > 0 ? q.pb.toFixed(1) : '-') + '</span><br>';
      }
      if (tech && tech.data) {
        var t = tech.data;
        h += '<div style="display:flex;flex-wrap:wrap;gap:6px;font-size:9px;color:var(--tx3);margin:4px 0">' +
          '<span>MA5 ' + (t.ma5 || '-') + '</span><span>MA20 ' + (t.ma20 || '-') + '</span>' +
          '<span>MA60 ' + (t.ma60 || '-') + '</span><span>RSI14 ' + (t.rsi14 || '-') + '</span>' +
          '<span>MACD ' + (t.macd_dif || '-') + '</span><span>VolRatio ' + (t.volume_ratio || '-') + '</span>' +
          '<span>Amp ' + (t.amplitude_pct || '-') + '%</span></div>';
      }
      h += '<div class="fl" style="font-size:9px;margin-bottom:2px">Minute (' + (mins ? mins.length : 0) + ' pts)</div>';
      h += renderMinuteChart(mins);
      h += '<div style="display:flex;align-items:center;gap:4px;margin:6px 0 2px">' +
        '<span class="fl" style="font-size:9px">K-line (' + (bars ? bars.length : 0) + ' bars)</span>';
      PERIODS.forEach(function (p) {
        var on = p.k === curPeriod;
        h += '<button data-klt="' + p.k + '" style="font-size:9px;padding:1px 6px;border-radius:8px;cursor:pointer;' +
          'border:1px solid ' + (on ? 'var(--accent)' : 'var(--border)') + ';' +
          'background:' + (on ? 'rgba(137,87,229,.2)' : 'transparent') + ';' +
          'color:' + (on ? 'var(--accent)' : 'var(--tx3)') + ';font-weight:700">' + p.label + '</button>';
      });
      h += '</div><div id="klineChartBox">' + renderKlineChart(bars) + '</div>';
      h += '<div class="fl" style="font-size:9px;margin:6px 0 2px">Main fund flow (last ' +
        (flows ? flows.length : 0) + ' days) - eastmoney push2his</div>';
      h += '<div id="flowChartBox">' + renderFundChart(flows) + '</div>';
      if (flows && flows.length) {
        var inflow = flows.filter(function (f) { return f.main_net > 0; })
          .reduce(function (s, f) { return s + f.main_net; }, 0);
        var outflow = flows.filter(function (f) { return f.main_net < 0; })
          .reduce(function (s, f) { return s + f.main_net; }, 0);
        h += '<div style="display:flex;gap:10px;font-size:10px;margin:4px 0">' +
          '<span>Inflow <b class="up">' + fmtYi(inflow) + '</b></span>' +
          '<span>Outflow <b class="dn">' + fmtYi(outflow) + '</b></span>' +
          '<span>Net <b class="' + (inflow + outflow >= 0 ? 'up' : 'dn') + '">' + fmtYi(inflow + outflow) + '</b></span></div>';
        h += '<table class="pool-table" style="margin-top:6px"><thead><tr><th>Date</th><th>Main</th><th>%</th>' +
          '<th>XLarge</th><th>Large</th><th>Medium</th><th>Small</th><th>Close</th></tr></thead><tbody>';
        flows.slice(0, 12).forEach(function (f) {
          h += '<tr><td>' + f.date.slice(5) + '</td>' +
            '<td class="' + (f.main_net >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.main_net) + '</td>' +
            '<td class="' + (f.main_net >= 0 ? 'up' : 'dn') + '">' + f.main_pct.toFixed(2) + '</td>' +
            '<td class="' + (f.xlarge >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.xlarge) + '</td>' +
            '<td class="' + (f.large >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.large) + '</td>' +
            '<td class="' + (f.medium >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.medium) + '</td>' +
            '<td class="' + (f.small >= 0 ? 'up' : 'dn') + '">' + fmtYi(f.small) + '</td>' +
            '<td>' + (f.close ? f.close.toFixed(2) : '-') + '</td></tr>';
        });
        h += '</tbody></table>';
        h += '<div class="fl" style="font-size:9px;margin-top:4px">Research reference only, not investment advice.</div>';
      }
      det.innerHTML = h;
      Array.prototype.forEach.call(det.querySelectorAll('button[data-klt]'), function (btn) {
        btn.onclick = function () {
          curPeriod = btn.dataset.klt;
          var box = document.getElementById('klineChartBox');
          if (box) box.innerHTML = '<span class="fl" style="font-size:9px">Loading...</span>';
          fetchKline(code, 60, curPeriod).then(function (nb) {
            if (box) box.innerHTML = renderKlineChart(nb);
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
