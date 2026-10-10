/* TradingBots terminal: read-only views over /api/*. No order controls exist. */
"use strict";

const REFRESH_MS = 15000;
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x, d = 1) => (x == null || isNaN(x) ? "—" : `${(x * 100).toFixed(d)}%`);
const money = (x) => (x == null || isNaN(x) ? "—" : `$${Number(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const signed = (x, f) => (x == null || isNaN(x) ? "—" : (x >= 0 ? "+" : "−") + f(Math.abs(x)));
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

const SERIES = {
  momentum: { color: "--s-momentum", dash: "" },
  faber: { color: "--s-faber", dash: "9 5" },
  rsi2: { color: "--s-rsi2", dash: "2 4" },
  "buy & hold SPY": { color: "--s-bench", dash: "5 4" },
};
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

/* ---- FX: scanlines, glow, rain ----------------------------------------------- */
function setFx(on) {
  document.body.classList.toggle("fx-off", !on);
  const b = $("#fx-toggle");
  b.textContent = `FX: ${on ? "ON" : "OFF"}`;
  b.setAttribute("aria-pressed", String(on));
  store.set("fx", on ? "on" : "off");
}

function startRain() {
  const canvas = $("#rain");
  if (reduceMotion || !canvas.getContext) return;
  const ctx = canvas.getContext("2d");
  const glyphs = "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789$%+-<>";
  const size = 16;
  let cols = 0, drops = [];
  const resize = () => {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    cols = Math.ceil(canvas.width / size);
    drops = Array.from({ length: cols }, () => Math.floor(Math.random() * -50));
  };
  resize();
  window.addEventListener("resize", resize);
  let last = 0;
  const frame = (t) => {
    requestAnimationFrame(frame);
    if (t - last < 60 || document.body.classList.contains("fx-off") || document.hidden) return;
    last = t;
    ctx.fillStyle = "rgba(2, 7, 2, 0.12)";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.font = `${size}px monospace`;
    for (let i = 0; i < cols; i++) {
      const y = drops[i] * size;
      ctx.fillStyle = Math.random() < 0.04 ? "#d8ffe2" : "#2fd463";
      ctx.fillText(glyphs[(Math.random() * glyphs.length) | 0], i * size, y);
      if (y > canvas.height && Math.random() > 0.975) drops[i] = 0;
      drops[i]++;
    }
  };
  requestAnimationFrame(frame);
}

/* ---- boot sequence ------------------------------------------------------------ */
async function boot(summary) {
  const el = $("#boot");
  const acct = summary.account;
  const lines = [
    "> RISK ENGINE ............ ONLINE",
    "> LIVE TRADING ENDPOINT .. LOCKED (PAPER ONLY)",
    `> STRATEGY ............... ${summary.strategy.toUpperCase()}`,
    `> PAPER BROKER LINK ...... ${acct.available ? "ESTABLISHED" : "OFFLINE"}`,
  ];
  if (reduceMotion) { el.textContent = lines.join("\n"); return; }
  el.textContent = "";
  for (const line of lines) {
    for (const ch of line) { el.textContent += ch; await new Promise((r) => setTimeout(r, 6)); }
    el.textContent += "\n";
  }
  el.insertAdjacentHTML("beforeend", '<span class="cursor"></span>');
}

/* ---- helpers ------------------------------------------------------------------ */
function meter(value, limit, text = null, width = 16) {
  const ratio = limit > 0 ? Math.max(0, value) / limit : 0;
  const filled = Math.min(width, Math.round(ratio * width));
  const cls = ratio >= 1 ? "bad-text" : ratio >= 0.7 ? "warn-text" : "";
  const label = text ?? `${pct(value)} / ${pct(limit)}`;
  return `<div class="meter ${cls}"><span class="bar">[${"█".repeat(filled)}${"░".repeat(width - filled)}]</span> <span>${label}</span></div>`;
}

function table(rows, opts = {}) {
  if (!rows || !rows.length) return `<div class="empty">${opts.empty || "NO DATA"}</div>`;
  const cols = opts.cols || Object.keys(rows[0]);
  const left = new Set(opts.left || [cols[0]]);
  const head = cols.map((c) => `<th class="${left.has(c) ? "left" : ""}">${esc(opts.labels?.[c] || c)}</th>`).join("");
  const body = rows.map((r) => {
    const cls = opts.rowClass ? opts.rowClass(r) : "";
    const cells = cols.map((c) => {
      const v = opts.cell ? opts.cell(c, r) : esc(r[c]);
      return `<td class="${left.has(c) ? "left" : ""}">${v}</td>`;
    }).join("");
    return `<tr class="${cls}">${cells}</tr>`;
  }).join("");
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

/* ---- STAT ----------------------------------------------------------------------- */
function renderStat(s) {
  const st = s.state.risk, lim = s.limits, acct = s.account;
  const equity = acct.available ? acct.equity : null;
  const dayStart = acct.available ? acct.last_equity : st.day_start_equity;
  const dayPL = equity != null && dayStart ? equity - dayStart : null;
  const dayLoss = equity != null && dayStart ? Math.max(0, 1 - equity / dayStart) : 0;
  const peak = Math.max(st.peak_equity || 0, equity || 0);
  const dd = equity != null && peak ? Math.max(0, 1 - equity / peak) : 0;
  const halted = st.daily_halt_day && st.daily_halt_day === st.day;

  const kb = $("#kill-badge");
  kb.hidden = !(st.kill_switch || halted);
  kb.className = `badge ${st.kill_switch ? "bad" : "warn"}`;
  kb.textContent = st.kill_switch ? "KILL SWITCH TRIPPED" : halted ? "DAILY HALT" : "";

  $("#tab-stat").innerHTML = `
    ${acct.available ? "" : `<div class="notice">PAPER BROKER OFFLINE — ${esc(acct.reason)}. Showing the bot's saved state only.</div>`}
    <div class="grid">
      <div class="panel"><h2>EQUITY</h2>
        <div class="stat">${equity != null ? money(equity) : "NO LINK"}</div>
        <div class="sub">CASH ${acct.available ? money(acct.cash) : "—"}</div></div>
      <div class="panel"><h2>TODAY</h2>
        <div class="stat ${dayPL < 0 ? "warn-text" : ""}">${signed(dayPL, money)}</div>
        <div class="label">LOSS VS PREVIOUS CLOSE · HALT AT LIMIT</div>${meter(dayLoss, lim.daily_loss_limit_pct)}</div>
      <div class="panel"><h2>DRAWDOWN</h2>
        <div class="stat ${dd >= lim.max_drawdown_pct ? "bad-text" : ""}">${pct(dd)}</div>
        <div class="label">FROM PEAK ${money(peak || null)} · KILL SWITCH AT LIMIT</div>${meter(dd, lim.max_drawdown_pct)}</div>
      <div class="panel"><h2>ORDERS TODAY</h2>
        <div class="stat">${st.orders_today ?? 0}<span class="sub"> / ${lim.max_orders_per_day}</span></div>
        <div class="label">RUNAWAY-LOOP LIMIT</div>${meter(st.orders_today || 0, lim.max_orders_per_day, `${st.orders_today || 0} / ${lim.max_orders_per_day}`)}</div>
    </div>
    <div class="grid" style="margin-top:14px">
      <div class="panel"><h2>SYSTEM</h2><dl class="kv">
        <dt>KILL SWITCH</dt><dd class="${st.kill_switch ? "bad-text" : ""}">${st.kill_switch ? "TRIPPED" : "ARMED"}</dd>
        ${st.kill_switch ? `<dt>REASON</dt><dd class="bad-text">${esc(st.kill_reason)}</dd>` : ""}
        <dt>DAILY HALT</dt><dd class="${halted ? "warn-text" : ""}">${halted ? "ACTIVE" : "CLEAR"}</dd>
        <dt>TRADING DAY</dt><dd>${esc(st.day || "—")}</dd>
        <dt>LAST CYCLE</dt><dd>${esc(s.state.last_cycle_day || "NEVER")}</dd>
        <dt>STRATEGY</dt><dd>${esc(s.strategy.toUpperCase())}</dd>
        <dt>MARKET FILTER</dt><dd>${esc(s.market_filter.symbol)} &gt; ${esc(s.market_filter.sma_days)}D SMA</dd>
      </dl></div>
      <div class="panel"><h2>RISK LIMITS</h2><dl class="kv">
        <dt>RISK PER TRADE</dt><dd>${pct(lim.max_risk_per_trade_pct)}</dd>
        <dt>MAX POSITION</dt><dd>${pct(lim.max_position_pct)}</dd>
        <dt>DAILY LOSS HALT</dt><dd>${pct(lim.daily_loss_limit_pct)}</dd>
        <dt>DRAWDOWN KILL</dt><dd>${pct(lim.max_drawdown_pct)}</dd>
        <dt>MAX ORDERS / DAY</dt><dd>${lim.max_orders_per_day}</dd>
        <dt>NO-TRADE WINDOW</dt><dd>${lim.skip_open_minutes}m OPEN · ${lim.skip_close_minutes}m CLOSE</dd>
        <dt>ACCOUNT RULES</dt><dd>LONG ONLY · NO MARGIN</dd>
      </dl></div>
      <div class="panel"><h2>ALLOWLIST</h2>
        <div class="sub" style="line-height:1.9">${s.symbols.map(esc).join(" · ")}</div></div>
    </div>`;
}

/* ---- INV ------------------------------------------------------------------------- */
function renderInv(s) {
  const meta = s.state.positions || {};
  const live = s.account.available ? s.account.positions : null;
  const syms = new Set([...Object.keys(meta), ...(live || []).map((p) => p.symbol)]);
  const rows = [...syms].sort().map((sym) => {
    const m = meta[sym] || {}, p = (live || []).find((x) => x.symbol === sym) || {};
    return {
      SYMBOL: sym, QTY: p.qty ?? "—", ENTRY: money(p.avg_entry_price ?? m.entry_price),
      PRICE: money(p.current_price), "UNREAL P/L": signed(p.unrealized_pl, money),
      STOP: money(m.stop_price), "ENTERED": m.entry_date || "—", STRATEGY: (m.strategy || "?").toUpperCase(),
      _pl: p.unrealized_pl,
    };
  });
  $("#tab-inv").innerHTML = `<div class="panel full"><h2>INVENTORY</h2>${table(rows, {
    cols: ["SYMBOL", "QTY", "ENTRY", "PRICE", "UNREAL P/L", "STOP", "ENTERED", "STRATEGY"],
    empty: "NO POSITIONS HELD. CASH IS A POSITION TOO.",
    cell: (c, r) => (c === "UNREAL P/L" && r._pl < 0 ? `<span class="warn-text">${esc(r[c])}</span>` : esc(r[c])),
  })}${live ? "" : '<div class="sub">BROKER OFFLINE: QTY AND PRICES UNAVAILABLE.</div>'}</div>`;
}

/* ---- DATA (backtest) ---------------------------------------------------------------- */
let dataPeriod = store.get("period", "test");

function renderData(b) {
  const el = $("#tab-data");
  if (!b.available) {
    el.innerHTML = `<div class="panel full"><h2>BACKTEST</h2><div class="empty">NO BACKTEST ON FILE. RUN <code>python -m scripts.backtest</code></div></div>`;
    return;
  }
  const m = b.meta;
  const range = m[dataPeriod];
  const events = (m.events?.[dataPeriod] || []);
  el.innerHTML = `
    ${m.synthetic ? '<div class="notice">SYNTHETIC RANDOM DATA — THIS RUN CHECKS THE PLUMBING ONLY. IT SAYS NOTHING ABOUT REAL MARKETS.</div>' : ""}
    ${m.overrides?.length ? `<div class="notice">CONFIG OVERRIDES IN THIS RUN: ${esc(m.overrides.join(", "))}</div>` : ""}
    <div class="panel full">
      <div class="chart-head"><h2>EQUITY CURVES · ${dataPeriod === "test" ? "HELD-OUT TEST" : "BUILD"} ${esc(range[0])} → ${esc(range[1])}</h2>
        <div class="seg" role="group" aria-label="Period">
          <button data-period="build" aria-pressed="${dataPeriod === "build"}">BUILD</button>
          <button data-period="test" aria-pressed="${dataPeriod === "test"}">TEST</button></div></div>
      <div class="chart" id="chart"><div class="tooltip" id="tip"></div></div>
      <div class="legend" id="legend"></div>
      <div class="sub">START = 100 · ${esc(m.data_note)} · GENERATED ${esc(m.generated)}</div>
    </div>
    <div class="panel full" style="margin-top:14px"><h2>SCORECARD</h2>
      ${table(b.comparison[dataPeriod], { cell: (c, r) => c === "Strategy" ? swatch(r[c]) + esc(r[c]) : esc(r[c]) })}
      ${events.length ? `<div class="sub" style="margin-top:8px">${events.map((e) => `<div class="${e.kind === "kill_switch" ? "bad-text" : "warn-text"}">! ${esc(e.strategy)} ${esc(e.kind)} ${esc(e.date)}: ${esc(e.message)}</div>`).join("")}</div>` : ""}
    </div>
    <div class="panel full" style="margin-top:14px"><h2>SENSITIVITY · MOMENTUM REBALANCE DAY</h2>
      <div class="sub" style="margin-bottom:6px">SAME STRATEGY, TRADED ON THE 1ST–6TH TRADING DAY OF THE MONTH. BIG SWINGS BETWEEN ROWS = TIMING LUCK.</div>
      ${table(b.sensitivity.filter((r) => r.Period === dataPeriod), { cols: Object.keys(b.sensitivity[0] || {}).filter((c) => c !== "Period"), labels: { "Trading day of month": "Day" } })}
    </div>`;
  el.querySelectorAll("[data-period]").forEach((btn) => btn.addEventListener("click", () => {
    dataPeriod = btn.dataset.period; store.set("period", dataPeriod); renderData(b);
  }));
  lineChart($("#chart"), b.curves[dataPeriod] || {});
  lastBacktest = b;
}

let lastBacktest = null, resizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { if (lastBacktest?.available) lineChart($("#chart"), lastBacktest.curves[dataPeriod] || {}); }, 150);
});

function swatch(name) {
  const s = SERIES[name];
  if (!s) return "";
  return `<span class="swatch" style="border-top-color:${cssVar(s.color)};border-top-style:${s.dash ? "dashed" : "solid"}"></span>`;
}

function lineChart(host, seriesMap) {
  const names = Object.keys(seriesMap);
  const tip = $("#tip", host);
  host.querySelector("svg")?.remove();
  if (!names.length) { host.insertAdjacentHTML("afterbegin", '<div class="empty">NO CURVES</div>'); return; }
  // Draw at the container's real width so text stays legible on phones.
  const W = Math.max(320, Math.round(host.clientWidth || 1000));
  const narrow = W < 640;
  const H = narrow ? 280 : 360, ml = 40, mr = narrow ? 104 : 150, mt = 10, mb = 26;
  const iw = W - ml - mr, ih = H - mt - mb;
  const parsed = names.map((n) => ({ name: n, pts: seriesMap[n].map(([d, v]) => [Date.parse(d), v]) }));
  const all = parsed.flatMap((s) => s.pts);
  const x0 = Math.min(...all.map((p) => p[0])), x1 = Math.max(...all.map((p) => p[0]));
  let y0 = Math.min(...all.map((p) => p[1])), y1 = Math.max(...all.map((p) => p[1]));
  const pad = (y1 - y0) * 0.06 || 5; y0 -= pad; y1 += pad;
  const sx = (x) => ml + ((x - x0) / (x1 - x0 || 1)) * iw;
  const sy = (y) => mt + (1 - (y - y0) / (y1 - y0)) * ih;

  const step = niceStep((y1 - y0) / 5);
  let grid = "";
  for (let v = Math.ceil(y0 / step) * step; v <= y1; v += step) {
    grid += `<line class="grid-line" x1="${ml}" x2="${ml + iw}" y1="${sy(v)}" y2="${sy(v)}"/><text class="axis-text" x="${ml - 8}" y="${sy(v) + 4}" text-anchor="end">${+v.toFixed(1)}</text>`;
  }
  const yStart = new Date(x0).getUTCFullYear(), yEnd = new Date(x1).getUTCFullYear();
  for (let yr = yStart + 1; yr <= yEnd; yr++) {
    const x = sx(Date.UTC(yr, 0, 1));
    if (narrow && (yr - yStart) % 2 === 0 && yEnd - yStart > 4) continue;
    grid += `<text class="axis-text" x="${x}" y="${H - 6}" text-anchor="middle">${yr}</text>`;
  }
  if (y0 < 100 && y1 > 100) grid += `<line x1="${ml}" x2="${ml + iw}" y1="${sy(100)}" y2="${sy(100)}" stroke="${cssVar("--line-strong")}" stroke-width="1.5"/>`;

  const lines = parsed.map((s) => {
    const st = SERIES[s.name] || { color: "--fg-dim", dash: "" };
    const d = s.pts.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join("");
    return `<path d="${d}" fill="none" stroke="${cssVar(st.color)}" stroke-width="2" stroke-dasharray="${st.dash}" stroke-linejoin="round" stroke-linecap="round"/>`;
  }).join("");

  // End labels, pushed apart so they never overlap.
  const ends = parsed.map((s) => ({ name: s.name, v: s.pts.at(-1)[1], y: sy(s.pts.at(-1)[1]) })).sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) ends[i].y = Math.max(ends[i].y, ends[i - 1].y + 15);
  const short = (n) => (narrow ? n.replace("buy & hold SPY", "SPY") : n);
  const labels = ends.map((e) => `<text class="end-label" x="${ml + iw + 8}" y="${e.y + 4}">${esc(short(e.name))} ${e.v.toFixed(0)}</text>`).join("");

  host.insertAdjacentHTML("afterbegin", `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Equity curves, start = 100">
    ${grid}${lines}${labels}
    <line class="xhair" id="xhair" y1="${mt}" y2="${mt + ih}" visibility="hidden"/>
    <g id="dots"></g>
    <rect x="${ml}" y="${mt}" width="${iw}" height="${ih}" fill="transparent" id="hit"/></svg>`);

  $("#legend").innerHTML = names.map((n) => `<span>${swatch(n)}${esc(n)}</span>`).join("");

  const svg = host.querySelector("svg"), hit = $("#hit", svg), xh = $("#xhair", svg), dots = $("#dots", svg);
  const ref = parsed.reduce((a, b) => (b.pts.length > a.pts.length ? b : a));
  const move = (ev) => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * W;
    const t = x0 + ((px - ml) / iw) * (x1 - x0);
    let lo = 0, hi = ref.pts.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (ref.pts[mid][0] < t) lo = mid + 1; else hi = mid; }
    const when = ref.pts[lo][0], x = sx(when);
    xh.setAttribute("x1", x); xh.setAttribute("x2", x); xh.setAttribute("visibility", "visible");
    let rows = "", ds = "";
    for (const s of parsed) {
      const p = s.pts.find((q) => q[0] === when);
      if (!p) continue;
      const st = SERIES[s.name] || { color: "--fg-dim" };
      ds += `<circle cx="${x}" cy="${sy(p[1])}" r="4" fill="${cssVar("--bg")}" stroke="${cssVar(st.color)}" stroke-width="2"/>`;
      rows += `<div class="row"><span>${swatch(s.name)}${esc(s.name)}</span><span>${p[1].toFixed(1)}</span></div>`;
    }
    dots.innerHTML = ds;
    tip.innerHTML = `<div class="label">${new Date(when).toISOString().slice(0, 10)}</div>${rows}`;
    tip.style.display = "block";
    const left = (x / W) * box.width;
    tip.style.left = `${Math.min(left + 14, box.width - tip.offsetWidth - 4)}px`;
    tip.style.top = `${Math.max(0, ((ev.clientY - box.top) - tip.offsetHeight - 12))}px`;
  };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerleave", () => { tip.style.display = "none"; xh.setAttribute("visibility", "hidden"); dots.innerHTML = ""; });
}

function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / p;
  return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
}

/* ---- TRADES ------------------------------------------------------------------------- */
function renderTrades(rows) {
  const flagged = rows.filter((r) => r.possible_wash_sale === "True").length;
  $("#tab-trades").innerHTML = `<div class="panel full"><h2>TRADE LOG · PAPER</h2>
    ${flagged ? `<div class="notice">⚠ ${flagged} ROW(S) FLAGGED AS POSSIBLE WASH SALES — CHECK BEFORE TAX TIME.</div>` : ""}
    ${table(rows.slice().reverse(), {
      cols: ["date", "symbol", "side", "qty", "price", "realized_pnl", "reason", "wash_sale_note"],
      left: ["date", "symbol", "side", "reason", "wash_sale_note"],
      labels: { realized_pnl: "P/L", wash_sale_note: "wash sale" },
      empty: "NO FILLS YET. START PAPER TRADING: <code>python -m scripts.run_paper</code>",
      rowClass: (r) => (r.possible_wash_sale === "True" ? "wash" : ""),
      cell: (c, r) => {
        if (c === "wash_sale_note") return r[c] ? `⚠ ${esc(r[c])}` : "";
        if (c === "realized_pnl") return r[c] ? signed(Number(r[c]), money) : "";
        if (c === "price") return money(Number(r[c]));
        if (c === "side") return esc(String(r[c]).toUpperCase());
        return esc(r[c]);
      },
    })}</div>`;
}

/* ---- LOG ---------------------------------------------------------------------------- */
let logFilter = "";
let lastDecisions = [];

function renderLog(items) {
  lastDecisions = items;
  const el = $("#tab-log");
  if (!$("#log-filter", el)) {
    el.innerHTML = `<div class="panel full"><div class="chart-head"><h2>DECISION FEED</h2>
      <input class="filter" id="log-filter" placeholder="filter: event, symbol, reason…" aria-label="Filter decisions"></div>
      <div class="feed" id="feed"></div></div>`;
    $("#log-filter", el).addEventListener("input", (e) => { logFilter = e.target.value.toLowerCase(); drawFeed(); });
  }
  drawFeed();
}

function drawFeed() {
  const feed = $("#feed");
  const rows = lastDecisions.filter((d) => !logFilter || JSON.stringify(d).toLowerCase().includes(logFilter));
  if (!rows.length) { feed.innerHTML = '<div class="empty">NO DECISIONS LOGGED YET.</div>'; return; }
  feed.innerHTML = rows.map((d) => {
    const { ts, level, event, ...rest } = d;
    let detail = Object.entries(rest).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join("  ");
    if (detail.length > 360) detail = detail.slice(0, 357) + "…";
    return `<div class="line"><span class="ts">${esc((ts || "").replace("T", " ").replace("+00:00", "Z"))}</span>
      <span class="lvl-${esc(level)}">${esc(level)}</span>
      <span><span class="ev lvl-${esc(level)}">${esc(event)}</span> <span class="detail">${esc(detail)}</span></span></div>`;
  }).join("");
}

/* ---- wiring ------------------------------------------------------------------------- */
function selectTab(name) {
  document.querySelectorAll("button.tab").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  document.querySelectorAll("main > section").forEach((s) => { s.hidden = s.id !== `tab-${name}`; });
  store.set("tab", name);
  if (name === "data" && lastBacktest?.available) lineChart($("#chart"), lastBacktest.curves[dataPeriod] || {});
}

async function refresh(first = false) {
  try {
    const [summary, backtest, trades, decisions] = await Promise.all([
      getJSON("/api/summary"), getJSON("/api/backtest"), getJSON("/api/trades"), getJSON("/api/decisions?limit=300"),
    ]);
    renderStat(summary); renderInv(summary); renderTrades(trades); renderLog(decisions);
    if (first || !$("#chart svg")) renderData(backtest);
    if (first) boot(summary);
  } catch (e) {
    $("#boot").textContent = `> LINK ERROR: ${e.message}. RETRYING…`;
  }
  $("#clock").textContent = `LAST SYNC ${new Date().toLocaleTimeString()}`;
}

document.querySelectorAll("button.tab").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));
$("#fx-toggle").addEventListener("click", () => setFx(document.body.classList.contains("fx-off")));
setFx(store.get("fx", "on") === "on");
selectTab(store.get("tab", "stat"));
startRain();
refresh(true);
setInterval(refresh, REFRESH_MS);
