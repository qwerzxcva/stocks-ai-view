import React, { useEffect, useMemo, useState } from "react";
import { Star, BookOpen, StickyNote, Link2, Trash2, Plus, Download, Upload, TrendingUp, X, Brain } from "lucide-react";
import { useLocalStorage, type Stock, type Trade, type Note, type Todo, type Link } from "./lib/store";
import { Card, Btn, Input } from "./components/ui";

type Tab = "ai" | "watch" | "trades" | "notes" | "board";

const uid = () => Math.random().toString(36).slice(2, 9);
const today = () => new Date().toISOString().slice(0, 10);

type AiPick = { symbol: string; name?: string; close: number; ret: number; vratio: number; pos240: number; close_pos: number; score: number; why?: string };
type AiData = { ts?: string; date?: string; rule?: string; picks?: AiPick[]; global?: { global_score?: number; global_mood?: string; suggest?: string }; adapt?: unknown; cycle?: string };

function AiStockPanel({ onAdd }: { onAdd: (code: string, name: string) => void }) {
  const [ai, setAi] = useState<AiData | null>(null);
  const [sim, setSim] = useState<{ stats?: Record<string, number>; samples?: number; state?: { streak?: number; dir?: string } } | null>(null);
  const [themes, setThemes] = useState<[string, number][]>([]);
  const [err, setErr] = useState("");
  useEffect(() => {
    fetch("https://qwerzxcva.github.io/stocks-ai-view/data/latest.json?t=" + Date.now())
      .then(r => r.json()).then(d => {
        let p = d.ai_picks;
        if (Array.isArray(p)) p = { picks: p };
        if (p && Array.isArray(p.picks)) p.picks = p.picks.map((q: Record<string, unknown>) => ({
          symbol: (q.symbol as string) || ((q.code as string) || "").replace(/^(\d{6})$/, m => "sh" + m),
          name: q.name, close: (q.close as number) ?? (q.price as number),
          ret: (q.ret as number) ?? ((q.chg_pct as number) || 0) / 100,
          vratio: q.vratio ?? 0, pos240: q.pos240 ?? 0, close_pos: q.close_pos ?? 0,
          score: q.score ?? 0, why: q.why,
        }));
        setAi(p);
        setSim(d.kline_sim || null);
        setThemes(d.ths_reason?.themes || []);
      }).catch(() => setErr("主站数据拉取失败(CDN/网络), 稍后重试"));
  }, []);
  if (err) return <Card title="AI选股"><div className="text-mut text-xs">{err}</div></Card>;
  if (!ai) return <Card title="AI选股"><div className="text-mut text-xs animate-pulse">拉取引擎数据…</div></Card>;
  const g = ai.global || {};
  const picks = ai.picks || [];
  return (
    <div className="grid gap-3">
      <Card title="引擎状态">
        <div className="flex flex-wrap gap-2 text-xs">
          <span className="rounded border border-bd px-2 py-1">日期 {ai.date || "—"}</span>
          <span className={`rounded border px-2 py-1 ${g.global_score != null && g.global_score > 0 ? "border-up text-up" : g.global_score != null && g.global_score < 0 ? "border-dn text-dn" : "border-bd"}`}>
            全球情绪 {g.global_score ?? "—"} ({g.global_mood || "—"})
          </span>
          <span className="rounded border border-bd px-2 py-1">周期: {String(ai.cycle ?? "—")}</span>
          <span className="rounded border border-bd px-2 py-1">胜率自适应: {ai.adapt ? "接入" : "待样本"}</span>
        </div>
        {g.suggest && <div className="mt-2 text-xs text-amber">💡 {g.suggest}</div>}
      </Card>
      {sim?.stats && (
        <Card title="🎲 明日K线模拟 (纯统计蒙特卡洛)">
          <div className="text-xs">
            同状态历史{sim.samples}样本, 连{sim.state?.streak}{sim.state?.dir === "up" ? "阳" : "阴"} →
            收涨 <b className="text-up">{sim.stats.p_up}%</b> · 冲高+1% <b>{sim.stats.p_high1pct}%</b> ·
            破位-1% <b className="text-dn">{sim.stats.p_low1pct}%</b> · 80%区间 {sim.stats.ret_p10_pct}% ~ +{sim.stats.ret_p90_pct}%
          </div>
        </Card>
      )}
      <Card title={`🤖 今日AI推荐 (${picks.length}) · 未涨停+主板+五因子+CYQ筹码+形态`}>
        <div className="text-[11px] text-mut mb-2">{ai.rule}</div>
        {picks.length === 0 && <div className="text-mut text-xs">当前防御模式无推荐(全球偏空/周期收紧)</div>}
        <div className="grid gap-1.5">
          {picks.map(p => (
            <div key={p.symbol} className="flex flex-wrap items-center gap-2 rounded border border-bd/60 px-2.5 py-2 text-xs">
              <span className="text-amber font-semibold w-20">{p.symbol}</span>
              <span className="w-20 truncate">{p.name || "—"}</span>
              <span>¥{p.close}</span>
              <span className={p.ret >= 0 ? "text-up" : "text-dn"}>{p.ret > 0 ? "+" : ""}{(p.ret * 100).toFixed(1)}%</span>
              <span className="text-mut">量比{p.vratio}</span>
              <span className="text-mut">位{(p.pos240 * 100).toFixed(0)}%</span>
              <span className="text-mut">收盘位{(p.close_pos * 100).toFixed(0)}%</span>
              <span className="text-amber">评分{p.score}</span>
              {p.why && <span className="text-mut w-full sm:w-auto flex-1 truncate" title={p.why}>{p.why}</span>}
              <button onClick={() => onAdd(p.symbol, p.name || p.symbol)}
                className="ml-auto rounded border border-amber px-2 py-0.5 text-amber hover:bg-amber/10">+自选</button>
            </div>
          ))}
        </div>
      </Card>
      {themes.length > 0 && (
        <Card title="🔥 涨停题材热度 (同花顺归因)">
          <div className="flex flex-wrap gap-1.5 text-xs">
            {themes.slice(0, 24).map(t => (
              <span key={t[0]} className="rounded-full border border-amber/60 px-2 py-0.5 text-amber">{t[0]}×{t[1]}</span>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}

function MinuteModal({ code, name, onClose }: { code: string; name: string; onClose: () => void }) {
  const [pts, setPts] = useState<[string, number][]>([]);
  useEffect(() => {
    fetch("https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=" + code).then(r => r.json()).then(d => {
      const arr = (d.data?.[code]?.data?.data || []) as string[];
      setPts(arr.map(s => { const f = s.split(" "); return [f[0], parseFloat(f[1])] as [string, number]; }));
    }).catch(() => {});
  }, [code]);
  if (!pts.length) return null;
  const ps = pts.map(p => p[1]).filter(v => v > 0);
  const lo = Math.min(...ps), hi = Math.max(...ps), pre = ps[0];
  const W = 560, H = 200;
  const xy = (i: number, v: number) => [ (i / (pts.length - 1)) * W, H - ((v - lo) / (hi - lo || 1)) * (H - 20) - 10 ];
  const path = ps.map((v, i) => { const [x, y] = xy(i, v); return (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1); }).join(" ");
  const preY = xy(0, pre)[1];
  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.6)", zIndex: 50, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div onClick={e => e.stopPropagation()} style={{ background: "var(--bg1)", border: "1px solid var(--bd)", borderRadius: 12, padding: 16, maxWidth: 600, width: "92%" }}>
        <div className="flex items-center justify-between mb-2">
          <div className="text-sm text-amber">{name || code} <span className="text-mut text-xs">当日分时</span></div>
          <button onClick={onClose} className="text-mut hover:text-tx text-sm">✕</button>
        </div>
        <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", background: "var(--bg2)", borderRadius: 8 }}>
          <line x1={0} y1={preY} x2={W} y2={preY} stroke="var(--bd)" strokeDasharray="3 3" strokeWidth={1} />
          <path d={path} fill="none" stroke={ps[ps.length - 1] >= pre ? "var(--up)" : "var(--dn)"} strokeWidth={1.5} />
          <text x={4} y={12} fill="var(--tx3)" fontSize={10}>高 {hi.toFixed(2)}</text>
          <text x={4} y={H - 4} fill="var(--tx3)" fontSize={10}>低 {lo.toFixed(2)} · 现价 {(ps[ps.length-1]||0).toFixed(2)}</text>
        </svg>
      </div>
    </div>
  );
}

function useQuotes(codes: string[]) {
  const [q, setQ] = useState<Record<string, { p: number; chg: number; name: string }>>({});
  const key = codes.sort().join(",");
  useEffect(() => {
    if (!codes.length) return;
    let dead = false;
    fetch("https://qt.gtimg.cn/q=" + codes.join(",")).then(r => r.text()).then(t => {
      if (dead) return;
      const out: Record<string, { p: number; chg: number; name: string }> = {};
      for (const m of t.matchAll(/v_(\w+)="([^"]*)"/g)) {
        const f = m[2].split("~");
        if (f.length > 45 && f[1]) out[m[1]] = { p: parseFloat(f[3]) || 0, chg: parseFloat(f[32]) || 0, name: f[1] };
      }
      setQ(out);
    }).catch(() => {});
    return () => { dead = true; };
  }, [key]);
  return q;
}

export default function App() {
  const [tab, setTab] = useState<Tab>("ai");
  const [stocks, setStocks] = useLocalStorage<Stock[]>("wb.stocks", []);
  const [trades, setTrades] = useLocalStorage<Trade[]>("wb.trades", []);
  const [notes, setNotes] = useLocalStorage<Note[]>("wb.notes", []);
  const [todos, setTodos] = useLocalStorage<Todo[]>("wb.todos", []);
  const [links, setLinks] = useLocalStorage<Link[]>("wb.links", [
    { id: uid(), title: "主站", url: "https://qwerzxcva.github.io/stocks-ai-view/" },
    { id: uid(), title: "本地站", url: "http://localhost:8600" },
  ]);
  const [notice, setNotice] = useState("");
  const quoteCodes = useMemo(() => [...new Set([...stocks.map(s => s.code), ...trades.map(t => t.code)])], [stocks, trades]);
  const quotes = useQuotes(quoteCodes);
  const [minuteStock, setMinuteStock] = useState<{ code: string; name: string } | null>(null);

  // ---- 自选股 ----
  const [wc, setWc] = useState(""); const [wn, setWn] = useState("");
  const addStock = () => {
    const code = wc.trim().toLowerCase();
    if (!/^(sh|sz)\d{6}$/.test(code)) { flash("代码格式: sh600000 / sz000001"); return; }
    if (stocks.some(s => s.code === code)) { flash("已在自选"); return; }
    setStocks([{ code, name: wn.trim() || code, note: "", addedAt: today() }, ...stocks]);
    setWc(""); setWn("");
  };
  // ---- 账本 ----
  const [tc, setTc] = useState(""); const [tn, setTn] = useState(""); const [tSide, setTSide] = useState<"buy"|"sell">("buy");
  const [tPrice, setTP] = useState(""); const [tQty, setTQ] = useState(""); const [tDate, setTD] = useState(today()); const [tNote, setTNote] = useState("");
  const addTrade = () => {
    const price = parseFloat(tPrice), qty = parseInt(tQty);
    if (!tc.trim() || !price || !qty) { flash("代码/价格/数量必填"); return; }
    setTrades([{ id: uid(), code: tc.trim().toLowerCase(), name: tn.trim() || tc.trim(), side: tSide, price, qty, date: tDate, note: tNote.trim() }, ...trades]);
    setTc(""); setTn(""); setTP(""); setTQ(""); setTNote("");
  };
  // 持仓汇总: 买卖对冲算净持仓与已实现盈亏(简化FIFO按均价)
  const positions = useMemo(() => {
    const m = new Map<string, { name: string; qty: number; cost: number; realized: number; sellQty: number; sellAmt: number; buyQty: number; buyAmt: number }>();
    const order = new Map(trades.map((t, i) => [t.id, trades.length - i]));  // 数组前=新, 倒序索引=时间正序
    const sorted = [...trades].sort((a, b) => a.date.localeCompare(b.date) || order.get(a.id)! - order.get(b.id)!);
    for (const t of sorted) {
      const p = m.get(t.code) ?? { name: t.name, qty: 0, cost: 0, realized: 0, sellQty: 0, sellAmt: 0, buyQty: 0, buyAmt: 0 };
      if (t.side === "buy") { p.qty += t.qty; p.cost += t.qty * t.price; p.buyQty += t.qty; p.buyAmt += t.qty * t.price; }
      else {
        const avg = p.qty > 0 ? p.cost / p.qty : 0;
        p.realized += (t.price - avg) * t.qty;
        p.qty = Math.max(0, p.qty - t.qty);
        if (p.qty === 0) p.cost = 0; else p.cost = avg * p.qty;
        p.sellQty += t.qty; p.sellAmt += t.qty * t.price;
      }
      m.set(t.code, p);
    }
    return [...m.entries()].map(([code, p]) => ({
      code, name: p.name, qty: p.qty,
      avgCost: p.qty > 0 ? p.cost / p.qty : 0,
      realized: p.realized,
      invested: p.qty * (p.qty > 0 ? p.cost / p.qty : 0),
    }));
  }, [trades]);

  // ---- 复盘笔记 ----
  const [nDate, setNDate] = useState(today()); const [nText, setNText] = useState("");
  // ---- 待办/便签/链接 ----
  const [todoText, setTodoText] = useState(""); const [linkT, setLinkT] = useState(""); const [linkU, setLinkU] = useState("");

  const flash = (m: string) => { setNotice(m); setTimeout(() => setNotice(""), 2500); };
  const exportAll = () => {
    const data = { stocks, trades, notes, todos, links, exportedAt: new Date().toISOString() };
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `workbench-${today()}.json`; a.click();
    flash("已导出备份");
  };
  const importAll = (file: File) => {
    const r = new FileReader();
    r.onload = () => {
      try {
        const d = JSON.parse(r.result as string);
        if (Array.isArray(d.stocks)) setStocks(d.stocks);
        if (Array.isArray(d.trades)) setTrades(d.trades);
        if (Array.isArray(d.notes)) setNotes(d.notes);
        if (Array.isArray(d.todos)) setTodos(d.todos);
        if (Array.isArray(d.links)) setLinks(d.links);
        flash("导入成功");
      } catch { flash("JSON 解析失败"); }
    };
    r.readAsText(file);
  };

  const tabs: { k: Tab; icon: React.ReactNode; label: string }[] = [
    { k: "ai", icon: <Brain size={14} />, label: "🤖 AI选股" },
    { k: "watch", icon: <Star size={14} />, label: "自选股" },
    { k: "trades", icon: <TrendingUp size={14} />, label: "买卖账本" },
    { k: "notes", icon: <BookOpen size={14} />, label: "复盘笔记" },
    { k: "board", icon: <StickyNote size={14} />, label: "便签·待办·链接" },
  ];

  return (
    <div className="mx-auto max-w-5xl p-4">
      <header className="flex flex-wrap items-center gap-3 border-b border-bd pb-3">
        <h1 className="text-base tracking-widest text-amber">投研工作台</h1>
        <span className="text-[10px] text-mut">数据存本机浏览器 · localStorage</span>
        <div className="ml-auto flex gap-2">
          <Btn onClick={exportAll}><span className="inline-flex items-center gap-1"><Download size={12} />导出</span></Btn>
          <label className="cursor-pointer rounded-lg border border-bd px-2.5 py-1 text-xs text-mut transition-colors hover:border-mut hover:text-fg">
            <span className="inline-flex items-center gap-1"><Upload size={12} />导入</span>
            <input type="file" accept=".json" className="hidden" onChange={e => e.target.files?.[0] && importAll(e.target.files[0])} />
          </label>
        </div>
      </header>
      {notice && <div className="mt-2 rounded-lg border border-amber bg-amber/10 px-3 py-1.5 text-xs text-amber">{notice}</div>}
      <nav className="my-3 flex flex-wrap gap-2">
        {tabs.map(t => (
          <button key={t.k} onClick={() => setTab(t.k)}
            className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs transition-colors ${tab === t.k ? "border-amber text-amber" : "border-bd text-mut hover:text-fg"}`}>
            {t.icon}{t.label}
          </button>
        ))}
      </nav>

      {tab === "ai" && <AiStockPanel onAdd={(code, name) => {
        if (stocks.some(s => s.code === code)) { flash("已在自选"); return; }
        setStocks([{ code, name, note: "AI推荐", addedAt: today() }, ...stocks]);
        flash(`已加自选: ${name || code}`);
      }} />}
      {tab === "watch" && (
        <div className="grid gap-3">
          <Card title="添加自选 (代码 sh600000 / sz000001)">
            <div className="flex flex-wrap gap-2">
              <Input placeholder="代码 如 sh600000" value={wc} onChange={e => setWc(e.target.value)} className="w-44" />
              <Input placeholder="备注名(可空)" value={wn} onChange={e => setWn(e.target.value)} className="w-40" />
              <Btn tone="amber" onClick={addStock}><span className="inline-flex items-center gap-1"><Plus size={12} />添加</span></Btn>
            </div>
          </Card>
          <Card title={`自选列表 (${stocks.length})`}>
            {stocks.length === 0 ? <p className="text-xs text-mut">还没有自选股，先添加一个。</p> : (
              <table className="w-full text-xs">
                <thead><tr className="text-left text-mut"><th className="py-1">代码</th><th>名称</th><th>现价</th><th>涨跌</th><th>添加日</th><th>备注</th><th /></tr></thead>
                <tbody>
                  {stocks.map(s => (
                    <tr key={s.code} className="border-t border-bd/50">
                      <td className="py-1.5 text-amber cursor-pointer hover:underline" onClick={() => setMinuteStock({ code: s.code, name: s.name || (quotes[s.code]?.name ?? "") })}>{s.code}</td>
                      <td>
                        <input value={s.name} onChange={e => setStocks(stocks.map(x => x.code === s.code ? { ...x, name: e.target.value } : x))}
                          className="w-24 bg-transparent outline-none focus:text-amber" />
                      </td>
                      <td>{quotes[s.code] ? quotes[s.code].p.toFixed(2) : "…"}
                        {quotes[s.code] && <span className={`ml-1 ${quotes[s.code].chg >= 0 ? "text-up" : "text-dn"}`}>{quotes[s.code].chg > 0 ? "+" : ""}{quotes[s.code].chg}%</span>}
                      </td>
                      <td className="text-mut">{s.addedAt}</td>
                      <td>
                        <input value={s.note} placeholder="点此写备注…" onChange={e => setStocks(stocks.map(x => x.code === s.code ? { ...x, note: e.target.value } : x))}
                          className="w-full bg-transparent outline-none focus:text-amber" />
                      </td>
                      <td><button onClick={() => setStocks(stocks.filter(x => x.code !== s.code))} className="text-mut hover:text-up"><Trash2 size={13} /></button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </div>
      )}

      {tab === "trades" && (
        <div className="grid gap-3">
          <Card title="记一笔 (买卖都记, 盈亏自动算)">
            <div className="flex flex-wrap items-center gap-2">
              <select value={tSide} onChange={e => setTSide(e.target.value as "buy"|"sell")}
                className={`rounded-lg border bg-bg px-2 py-1.5 text-xs ${tSide === "buy" ? "border-up text-up" : "border-dn text-dn"}`}>
                <option value="buy">买入</option><option value="sell">卖出</option>
              </select>
              <Input placeholder="代码" value={tc} onChange={e => setTc(e.target.value)} className="w-28" />
              <Input placeholder="名称" value={tn} onChange={e => setTn(e.target.value)} className="w-24" />
              <Input placeholder="价格" type="number" step="0.01" value={tPrice} onChange={e => setTP(e.target.value)} className="w-20" />
              <Input placeholder="数量" type="number" value={tQty} onChange={e => setTQ(e.target.value)} className="w-20" />
              <Input type="date" value={tDate} onChange={e => setTD(e.target.value)} className="w-32" />
              <Input placeholder="备注" value={tNote} onChange={e => setTNote(e.target.value)} className="w-28" />
              <Btn tone="amber" onClick={addTrade}><span className="inline-flex items-center gap-1"><Plus size={12} />记录</span></Btn>
            </div>
          </Card>
          <Card title="当前持仓 (按均价成本法)">
            {positions.filter(p => p.qty > 0).length === 0 ? <p className="text-xs text-mut">暂无持仓。</p> : (
              <table className="w-full text-xs">
                <thead><tr className="text-left text-mut"><th className="py-1">代码</th><th>名称</th><th>持仓</th><th>均价</th><th>现价</th><th>市值</th><th>浮动盈亏</th></tr></thead>
                <tbody>{positions.filter(p => p.qty > 0).map(p => {
                  const px = quotes[p.code]?.p ?? 0;
                  const mv = px * p.qty;
                  const fl = px ? mv - p.invested : 0;
                  return (
                  <tr key={p.code} className="border-t border-bd/50">
                    <td className="py-1.5 text-amber cursor-pointer hover:underline" onClick={() => setMinuteStock({ code: p.code, name: p.name })}>{p.code}</td><td>{p.name}</td>
                    <td>{p.qty}</td><td>{p.avgCost.toFixed(2)}</td>
                    <td>{px ? px.toFixed(2) : "…"}</td>
                    <td>{mv ? mv.toFixed(0) : "…"}</td>
                    <td className={fl > 0 ? "text-up" : fl < 0 ? "text-dn" : "text-mut"}>{fl ? `${fl > 0 ? "+" : ""}${fl.toFixed(0)} (${(fl / (p.invested || 1) * 100).toFixed(2)}%)` : "…"}</td>
                  </tr>);})}</tbody>
              </table>
            )}
          </Card>
          <Card title={`交易记录 (${trades.length})`}>
            {trades.length === 0 ? <p className="text-xs text-mut">还没有交易记录。</p> : (
              <table className="w-full text-xs">
                <thead><tr className="text-left text-mut"><th className="py-1">日期</th><th>方向</th><th>代码</th><th>价格</th><th>数量</th><th>备注</th><th /></tr></thead>
                <tbody>{trades.map(t => (
                  <tr key={t.id} className="border-t border-bd/50">
                    <td className="py-1.5 text-mut">{t.date}</td>
                    <td className={t.side === "buy" ? "text-up" : "text-dn"}>{t.side === "buy" ? "买入" : "卖出"}</td>
                    <td className="text-amber">{t.code} {t.name}</td>
                    <td><input type="number" step="0.01" value={t.price} onChange={e => setTrades(trades.map(x => x.id === t.id ? { ...x, price: parseFloat(e.target.value) || 0 } : x))} className="w-16 bg-transparent outline-none focus:text-amber" /></td>
                    <td><input type="number" value={t.qty} onChange={e => setTrades(trades.map(x => x.id === t.id ? { ...x, qty: parseInt(e.target.value) || 0 } : x))} className="w-16 bg-transparent outline-none focus:text-amber" /></td>
                    <td><input value={t.note} onChange={e => setTrades(trades.map(x => x.id === t.id ? { ...x, note: e.target.value } : x))} className="w-28 bg-transparent outline-none focus:text-amber" /></td>
                    <td><button onClick={() => setTrades(trades.filter(x => x.id !== t.id))} className="text-mut hover:text-up"><X size={13} /></button></td>
                  </tr>))}</tbody>
              </table>
            )}
            {positions.some(p => p.realized !== 0) && (
              <p className="mt-2 text-xs text-mut">已实现盈亏：{positions.filter(p => p.realized !== 0).map(p => (
                <span key={p.code} className={`mr-3 ${p.realized > 0 ? "text-up" : "text-dn"}`}>{p.name} {p.realized > 0 ? "+" : ""}{p.realized.toFixed(0)}</span>
              ))}</p>
            )}
          </Card>
        </div>
      )}

      {tab === "notes" && (
        <div className="grid gap-3">
          <Card title="写今日复盘">
            <div className="flex gap-2">
              <Input type="date" value={nDate} onChange={e => setNDate(e.target.value)} className="w-36" />
              <Btn tone="amber" onClick={() => { if (!nText.trim()) return; setNotes([{ id: uid(), date: nDate, text: nText.trim() }, ...notes]); setNText(""); }}>
                <span className="inline-flex items-center gap-1"><Plus size={12} />保存</span></Btn>
            </div>
            <textarea value={nText} onChange={e => setNText(e.target.value)} rows={4} placeholder="今天盘面怎么样？操作了什么？明天看什么？…"
              className="mt-2 w-full rounded-lg border border-bd bg-bg p-2 text-xs outline-none focus:border-amber" />
          </Card>
          {notes.map(n => (
            <Card key={n.id} right={<button onClick={() => setNotes(notes.filter(x => x.id !== n.id))} className="text-mut hover:text-up"><Trash2 size={13} /></button>}>
              <p className="text-[10px] text-mut">{n.date}</p>
              <textarea value={n.text} rows={Math.max(2, n.text.split("\n").length)}
                onChange={e => setNotes(notes.map(x => x.id === n.id ? { ...x, text: e.target.value } : x))}
                className="w-full bg-transparent text-xs outline-none focus:text-amber" />
            </Card>
          ))}
        </div>
      )}

      {tab === "board" && (
        <div className="grid gap-3 md:grid-cols-2">
          <Card title="待办">
            <div className="flex gap-2">
              <Input placeholder="要做什么？" value={todoText} onChange={e => setTodoText(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter" && todoText.trim()) { setTodos([{ id: uid(), text: todoText.trim(), done: false }, ...todos]); setTodoText(""); } }} className="flex-1" />
              <Btn tone="amber" onClick={() => { if (todoText.trim()) { setTodos([{ id: uid(), text: todoText.trim(), done: false }, ...todos]); setTodoText(""); } }}><Plus size={12} /></Btn>
            </div>
            <ul className="mt-2 space-y-1">
              {todos.map(t => (
                <li key={t.id} className="flex items-center gap-2 text-xs">
                  <input type="checkbox" checked={t.done} onChange={() => setTodos(todos.map(x => x.id === t.id ? { ...x, done: !x.done } : x))} />
                  <span className={t.done ? "text-mut line-through" : ""}>{t.text}</span>
                  <button onClick={() => setTodos(todos.filter(x => x.id !== t.id))} className="ml-auto text-mut hover:text-up"><X size={12} /></button>
                </li>
              ))}
            </ul>
          </Card>
          <Card title="快捷链接">
            <div className="flex gap-2">
              <Input placeholder="名称" value={linkT} onChange={e => setLinkT(e.target.value)} className="w-24" />
              <Input placeholder="https://…" value={linkU} onChange={e => setLinkU(e.target.value)} className="flex-1" />
              <Btn tone="amber" onClick={() => { if (linkT.trim() && linkU.trim()) { setLinks([{ id: uid(), title: linkT.trim(), url: linkU.trim() }, ...links]); setLinkT(""); setLinkU(""); } }}><Plus size={12} /></Btn>
            </div>
            <ul className="mt-2 space-y-1 text-xs">
              {links.map(l => (
                <li key={l.id} className="flex items-center gap-2">
                  <Link2 size={12} className="text-amber" />
                  <a href={l.url} target="_blank" rel="noopener noreferrer" className="hover:text-amber">{l.title}</a>
                  <span className="truncate text-mut">{l.url}</span>
                  <button onClick={() => setLinks(links.filter(x => x.id !== l.id))} className="ml-auto text-mut hover:text-up"><X size={12} /></button>
                </li>
              ))}
            </ul>
          </Card>
          <Card title="便签 (自动保存)" className="md:col-span-2">
            <textarea placeholder="随手记点什么…自动保存到本机"
              value={localStorage.getItem("wb.scratch") ?? ""}
              onChange={e => localStorage.setItem("wb.scratch", e.target.value)}
              rows={5} className="w-full rounded-lg border border-bd bg-bg p-2 text-xs outline-none focus:border-amber" />
          </Card>
        </div>
      )}
      {minuteStock && <MinuteModal code={minuteStock.code} name={minuteStock.name} onClose={() => setMinuteStock(null)} />}
      <footer className="mt-6 text-center text-[10px] text-mut">投研工作台 · 数据仅存本机浏览器 · 导出JSON可备份迁移 · 不构成投资建议</footer>
    </div>
  );
}
