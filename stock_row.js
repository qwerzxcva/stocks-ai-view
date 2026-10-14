/**
 * stock_row.js — 统一的股票行渲染 (PS1 规范)
 *
 * 所有面板的股票列表共用这一套字段与样式, 保证体验一致:
 *   代码 | 名称(可点开详情) | 现价 | 涨幅 | 实时价值+偏差% | 综合分 | 共振 | 多空 | 行业 | 20日位
 *
 * 设计要点:
 *   - 实时价值 = intrinsic_value (估值引擎算出的每股内在价值), 与现价并列显示,
 *     偏差% 用颜色区分 (低估红/高估绿 — A股红涨绿跌的约定)
 *   - 字段缺失时显示 "—" 并加 .na 类 (置灰), 不用 0 冒充
 *   - 名称点击走 window.StockDetail.open(code), 由 stock-detail.js 提供
 *   - 数据来源兼容: ai_pick_v4 / multidim / decision / scenario / ban_pick 等
 *     各文件的字段名不同, 这里做归一化
 */
(function (global) {
  "use strict";

  var FMT = {
    num: function (v, d) {
      if (v == null || isNaN(v)) return "—";
      return Number(v).toFixed(d == null ? 2 : d);
    },
    pct: function (v, d) {
      if (v == null || isNaN(v)) return "—";
      var s = (v >= 0 ? "+" : "") + Number(v).toFixed(d == null ? 2 : d);
      return s + "%";
    },
    pctPlain: function (v, d) {
      if (v == null || isNaN(v)) return "—";
      return Number(v).toFixed(d == null ? 1 : d) + "%";
    }
  };

  /** 从各种来源的记录里归一化出统一字段 */
  function normalize(r) {
    if (!r) return null;
    var code = r.code || r.symbol || r.secid || "";
    code = String(code).replace(/^(sh|sz|bj)/, "");
    var price = r.price != null ? r.price : (r.close != null ? r.close : null);
    // 涨幅: chg_pct / pct / ret_pct 都可能
    var chg = r.chg_pct != null ? r.chg_pct : (r.pct != null ? r.pct : r.ret_pct);
    // 实时价值: intrinsic_value (估值引擎) / fair_value / value
    var iv = r.intrinsic_value != null ? r.intrinsic_value
           : (r.fair_value != null ? r.fair_value : r.value);
    // 偏差%: value_dev_pct 已有, 否则用 iv 与 price 现算
    var dev = r.value_dev_pct;
    if (dev == null && iv != null && price != null && price > 0) {
      dev = (price - iv) / iv * 100;
    }
    // 综合分: composite / score
    var comp = r.composite != null ? r.composite : r.score;
    // 共振: resonance / resonance_count
    var res = r.resonance != null ? r.resonance : r.resonance_count;
    // 多空: bull/bear 计数
    var bull = r.bull, bear = r.bear;
    if (bull == null && r.votes) {
      bull = 0; bear = 0;
      for (var k in r.votes) {
        var v = r.votes[k];
        if (v > 0) bull++; else if (v < 0) bear++;
      }
    }
    // 行业
    var ind = r.industry || r.sector || (r.why && r.why["行业"]) || "";
    // 20日位: pos240_pct / pos20_pct, 或从 why 文本里抠
    var pos = r.pos240_pct != null ? r.pos240_pct : r.pos20_pct;
    if (pos == null && r.why) {
      var w = r.why["高低位"] || "";
      var m = w.match(/(\d+(?:\.\d+)?)%/);
      if (m) pos = parseFloat(m[1]);
    }
    return {
      code: code, name: r.name || "", price: price, chg: chg,
      intrinsic_value: iv, value_dev_pct: dev,
      composite: comp, resonance: res, bull: bull, bear: bear,
      industry: ind, pos20: pos,
      action: r.action || "", scenario_score: r.scenario_score,
      raw: r
    };
  }

  /** 生成一列 (td) — 复用给表格与卡片两种布局 */
  function cells(s, opts) {
    opts = opts || {};
    var na = '<span class="na">—</span>';
    var priceTd = s.price != null
      ? '<td class="rk-price">' + FMT.num(s.price) + "</td>"
      : "<td>" + na + "</td>";
    var chgTd = s.chg != null
      ? '<td class="' + (s.chg >= 0 ? "up" : "dn") + '">' + FMT.pct(s.chg) + "</td>"
      : "<td>" + na + "</td>";
    // 实时价值 + 偏差
    var valTd;
    if (s.intrinsic_value != null) {
      var devColor = "var(--tx3)";
      if (s.value_dev_pct != null) {
        devColor = s.value_dev_pct <= -10 ? "var(--up)"
                : (s.value_dev_pct >= 30 ? "var(--dn)" : "var(--tx3)");
      }
      valTd = '<td class="rk-val"><b>' + FMT.num(s.intrinsic_value) + "</b>";
      if (s.value_dev_pct != null) {
        valTd += '<span style="font-size:10px;margin-left:3px;color:' + devColor + '">'
              + FMT.pct(s.value_dev_pct, 1) + "</span>";
      }
      valTd += "</td>";
    } else {
      valTd = "<td>" + na + "</td>";
    }
    var compTd = s.composite != null
      ? '<td class="rk-comp">' + FMT.num(s.composite, 1) + "</td>"
      : "<td>" + na + "</td>";
    var resTd = s.resonance != null
      ? '<td class="rk-res">' + FMT.num(s.resonance, 2) + "</td>"
      : "<td>" + na + "</td>";
    var bbTd = (s.bull != null || s.bear != null)
      ? '<td class="rk-bb"><span class="up">' + (s.bull || 0) + '</span>/<span class="dn">'
        + (s.bear || 0) + "</span></td>"
      : "<td>" + na + "</td>";
    var indTd = s.industry
      ? '<td class="rk-ind">' + esc(s.industry) + "</td>"
      : "<td>" + na + "</td>";
    var posTd = s.pos20 != null
      ? '<td class="rk-pos">' + FMT.pctPlain(s.pos20) + "</td>"
      : "<td>" + na + "</td>";
    return priceTd + chgTd + valTd + compTd + resTd + bbTd + indTd + posTd;
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }

  /** 表头 — 供各面板复用, 保证列顺序一致 */
  var HEAD =
    "<thead><tr><th>代码</th><th>名称</th><th>现价</th><th>涨幅</th>" +
    "<th>实时价值</th><th>综合分</th><th>共振</th><th>多空</th>" +
    "<th>行业</th><th>20日位</th></tr></thead>";

  /**
   * 渲染一个股票表格
   * @param records 原始记录数组 (任何来源)
   * @param opts {limit, extraCols:[{title, fn}], showHead:true}
   */
  function table(records, opts) {
    opts = opts || {};
    var limit = opts.limit || 50;
    var list = (records || []).map(normalize).filter(Boolean).slice(0, limit);
    if (!list.length) {
      return '<div class="no-data">暂无数据</div>';
    }
    var extra = opts.extraCols || [];
    var head = opts.showHead === false ? "" : HEAD.replace(
      "</tr></thead>",
      extra.map(function (c) { return "<th>" + esc(c.title) + "</th>"; }).join("")
      + "</tr></thead>");
    var body = "<tbody>";
    list.forEach(function (s) {
      body += '<tr class="clk" data-code="' + esc(s.code) + '">'
        + "<td>" + esc(s.code) + "</td>"
        + '<td><b class="nm">' + esc(s.name || s.code) + "</b></td>"
        + cells(s)
        + extra.map(function (c) {
            var v = c.fn(s);
            return "<td>" + (v == null ? '<span class="na">—</span>' : v) + "</td>";
          }).join("")
        + "</tr>";
    });
    body += "</tbody>";
    return "<table>" + head + body + "</table>";
  }

  /** 行内提示: 字段缺失的置灰说明 */
  function legend() {
    return '<div class="fl" style="font-size:9px;padding:4px 0">'
      + '<span class="na">—</span> = 该字段暂无数据 (已接入的引擎未产出或数据源未覆盖)'
      + " · 实时价值 = 估值引擎算出的每股内在价值, 偏差% = (现价-价值)/价值"
      + " · Research reference only, not investment advice.</div>";
  }

  global.StockRow = {
    normalize: normalize,
    table: table,
    cells: cells,
    HEAD: HEAD,
    legend: legend,
    esc: esc,
    fmt: FMT
  };
})(window);
