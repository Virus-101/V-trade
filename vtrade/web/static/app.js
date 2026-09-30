'use strict';

const $ = (id) => document.getElementById(id);
// Set by `vtrade publish`: the page is a read-only snapshot reading api/*.json files.
const SNAPSHOT = window.VTRADE_STATIC || null;
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const S = {
  view: 'dashboard', overview: null, signal: null, signalError: null, backtest: null, paper: null,
  logAfter: 0, logLines: [], howStep: 0, howTrade: 0, allTrades: false, evalFirst: true,
  lastJobStatus: null, charts: {},
  news: null, newsError: null, traders: null, tradersError: null, tradersLoading: false,
  newsImpacts: { High: true, Medium: true, Low: false, Holiday: false }, newsCurrency: 'all',
  live: false, copyAcct: null, copyError: null, copyBusy: false,
};

const TITLES = { dashboard: 'Dashboard', how: 'How it works', paper: 'Paper trading', news: 'News', traders: 'Top traders', backtest: 'Backtest', model: 'Model & data', settings: 'Settings' };
const FORTRADE_URL = 'https://pro.fortrade.com';
const MODE_HELP = {
  off: 'Trades on the ML model\'s signal alone.',
  filter: 'Trades on the ML model\'s signal, but only buys while the top traders are net long.',
  follow: 'Ignores the model: buys when the top traders turn net long, sells when they stop.',
};
const MODE_NAME = { off: 'ML model only', filter: 'ML model + top traders', follow: 'Copy top traders' };

// ------------------------------------------------------------------ formatting
const money = (v, dp = 0) => v == null ? '–' : (v < 0 ? '−$' : '$') + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp });
const signedMoney = (v, dp = 2) => v == null ? '–' : (v > 0 ? '+' : v < 0 ? '−' : '') + '$' + Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp });
const pct = (v, dp = 1, sign = true) => v == null ? '–' : (sign && v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v * 100).toFixed(dp) + '%';
const num = (v, dp = 2) => v == null ? '–' : Number(v).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp });
const tone = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : '');
const pad2 = (n) => String(n).padStart(2, '0');
const candleLabel = (iso) => { const d = new Date(iso); return `${d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
const when = (iso) => { if (!iso) return '–'; const d = new Date(iso); return isNaN(d) ? esc(iso) : d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); };
const ago = (iso) => {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};
const until = (iso) => {
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  const a = Math.abs(s);
  const txt = a < 60 ? 'now' : a < 3600 ? `${Math.round(a / 60)} min` : a < 86400 ? `${Math.floor(a / 3600)} h ${Math.round((a % 3600) / 60)} min` : `${Math.floor(a / 86400)} d ${Math.round((a % 86400) / 3600)} h`;
  return txt === 'now' ? 'now' : s > 0 ? `in ${txt}` : `${txt} ago`;
};
const dayTime = (iso) => { const d = new Date(iso); return `${d.toLocaleDateString('en-US', { weekday: 'short' })} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
const impactBadge = (i) => `<span class="badge ${i === 'High' ? 'bad' : i === 'Medium' ? 'warn' : ''}">${esc(i)}</span>`;
const shortAddr = (a) => `${a.slice(0, 6)}…${a.slice(-4)}`;
const compact = (v) => v == null ? '–' : (v < 0 ? '−$' : '$') + (Math.abs(v) >= 1e6 ? `${(Math.abs(v) / 1e6).toFixed(1)}M` : Math.abs(v) >= 1e3 ? `${(Math.abs(v) / 1e3).toFixed(0)}k` : Math.abs(v).toFixed(0));
const biasText = (b) => b == null ? 'no positions' : b > 0.15 ? 'net long' : b < -0.15 ? 'net short' : 'mixed';
const biasNum = (b) => b == null ? '–' : (b > 0 ? '+' : '') + b.toFixed(2);
function biasBar(b) {
  if (b == null) return '<div class="bias"><div class="mid"></div></div>';
  const w = Math.abs(b) * 50, left = b >= 0 ? 50 : 50 - w;
  return `<div class="bias"><div class="fill" style="left:${left}%;width:${w}%;background:${b >= 0 ? 'var(--good)' : 'var(--bad)'}"></div><div class="mid"></div></div><div class="meter-labels"><span>all short</span><span>neutral</span><span>all long</span></div>`;
}
const tfHours = (tf) => { const n = parseFloat(tf); const u = tf.slice(-1); return u === 'm' ? n / 60 : u === 'h' ? n : u === 'd' ? n * 24 : n * 168; };

// ------------------------------------------------------------------ api
async function api(path, method = 'GET', body) {
  if (SNAPSHOT) {
    if (method !== 'GET') throw new Error('This is a read-only snapshot. Trading controls work in the V-trade app on your computer.');
    const file = path.split('?')[0].replace(/^\/api\//, '').replace(/\//g, '-');
    const res = await fetch(`/api/${file}.json`, { cache: 'no-cache' });
    const data = await res.json();
    if (data && data.unavailable) throw new Error(data.detail || 'Not available in this snapshot');
    return data;
  }
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-VTrade': '1' },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) throw new Error((data && data.detail) || `${res.status} ${res.statusText}`);
  return data;
}

const KEY_NAME = 'vtrade-admin-key';
function adminKey() { try { return localStorage.getItem(KEY_NAME) || ''; } catch { return ''; } }
function saveKey(k) { try { k ? localStorage.setItem(KEY_NAME, k) : localStorage.removeItem(KEY_NAME); } catch { /* private mode */ } }

async function liveApi(path, method = 'GET', body) {
  const headers = { 'Content-Type': 'application/json' };
  if (method !== 'GET') {
    let key = adminKey();
    if (!key) {
      key = (prompt('Admin key for the online copy account (the VTRADE_ADMIN_KEY line in V-trade\'s .env file):') || '').trim();
      if (!key) throw new Error('No admin key entered');
      saveKey(key);
    }
    headers['X-Admin-Key'] = key;
  }
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined, cache: 'no-store' });
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  if (res.status === 401) saveKey('');
  if (!res.ok) throw new Error((data && data.detail) || `${res.status} ${res.statusText}`);
  return data;
}

function toast(message, bad = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' bad' : '');
  el.textContent = message;
  $('toast').appendChild(el);
  setTimeout(() => el.remove(), bad ? 7000 : 4000);
}

// ------------------------------------------------------------------ charts
function palette() {
  return { accent: css('--accent'), gray: css('--ink-3'), good: css('--good'), bad: css('--bad'), grid: css('--grid'), panel: css('--panel'), ink2: css('--ink-2') };
}
const lineDs = (label, data, color, extra = {}) => ({ label, data, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0.15, spanGaps: true, ...extra });
const hlineDs = (label, n, value, color) => lineDs(label, Array(n).fill(value), color, { borderDash: [6, 4], borderWidth: 1.5, pointHoverRadius: 0, tension: 0 });
const markerDs = (label, n, i, value, color, style) => {
  const data = Array(n).fill(null); data[i] = value;
  return { label, data, showLine: false, pointRadius: 7, pointHoverRadius: 8, pointStyle: style, backgroundColor: color, borderColor: palette().panel, borderWidth: 2 };
};
const legend = (items) => items.map(([color, text, dash]) => `<span><i class="${dash ? 'dash' : ''}" style="border-top-color:${color}"></i>${esc(text)}</span>`).join('');

function chart(id, labels, datasets, opts = {}) {
  const el = $(id);
  if (!el || typeof Chart === 'undefined') return;
  const P = palette();
  const yFmt = opts.y || ((v) => v);
  const xTicks = { color: P.gray, maxTicksLimit: opts.xTicks || 6, maxRotation: 0, autoSkipPadding: 12, font: { size: 11 } };
  if (opts.xFmt) xTicks.callback = function (v) { return opts.xFmt(this.getLabelForValue(v)); };
  const options = {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: { filter: (item) => item.raw !== null, callbacks: { label: (c) => ` ${c.dataset.label}: ${yFmt(c.raw)}` } },
    },
    scales: {
      x: { grid: { display: false }, border: { color: P.grid }, ticks: xTicks },
      y: { min: opts.yMin, max: opts.yMax, grid: { color: P.grid }, border: { display: false }, ticks: { color: P.gray, font: { size: 11 }, maxTicksLimit: 6, callback: (v) => yFmt(v) } },
    },
  };
  let existing = S.charts[id];
  if (existing && existing.canvas !== el) { existing.destroy(); delete S.charts[id]; existing = null; }
  if (existing) {
    existing.data.labels = labels;
    existing.data.datasets = datasets;
    existing.options = options;
    existing.update('none');
    return;
  }
  S.charts[id] = new Chart(el, { type: 'line', data: { labels, datasets }, options });
}

// ------------------------------------------------------------------ small components
const kpi = (label, value, sub = '', extra = '') => `<div class="card kpi"><div class="label">${label}</div><div class="value">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ''}${extra}</div>`;
const mini = (label, value, sub = '') => `<div class="mini"><div class="label">${label}</div><div class="value">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ''}</div>`;
const kv = (rows) => `<table class="kv"><tbody>${rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join('')}</tbody></table>`;
const html = (id, markup) => { const el = $(id); if (el && el._html !== markup) { el._html = markup; el.innerHTML = markup; } };
const empty = (text, action = '') => `<div class="empty">${text}${action ? `<div style="margin-top:12px">${action}</div>` : ''}</div>`;
const decisionBadge = (d) => d === 'enter' ? '<span class="badge good">Buy signal</span>' : d === 'exit' ? '<span class="badge bad">Sell signal</span>' : '<span class="badge">Hold</span>';

function meter(prob, entry, exit) {
  const w = Math.max(0, Math.min(1, prob || 0)) * 100;
  return `<div class="meter"><div class="fill" style="width:${w}%"></div><div class="tick" style="left:${exit * 100}%" title="Sell line"></div><div class="tick" style="left:${entry * 100}%" title="Buy line"></div></div>
    <div class="meter-labels"><span>0</span><span>sell ${exit} · buy ${entry}</span><span>1</span></div>`;
}

function paperEquity() {
  const st = S.paper?.state || S.overview?.paper?.state;
  const start = S.overview?.config?.risk?.starting_equity ?? 10000;
  if (!st) return { equity: start, cash: start, pos: null, unreal: 0 };
  const price = S.signal?.price;
  const pos = st.position;
  const mark = pos ? (price ?? pos.entry_price) : 0;
  const unreal = pos ? (mark - pos.entry_price) * pos.qty - pos.entry_fee : 0;
  return { equity: st.cash + (pos ? pos.qty * mark : 0), cash: st.cash, pos, unreal, st };
}

// ------------------------------------------------------------------ header & setup banner
function renderChrome() {
  const o = S.overview;
  if (!o) return;
  $('market').textContent = `${o.market.symbol} · ${o.market.timeframe} · ${o.market.exchange}`;
  $('version').textContent = `v${o.version}`;
  const pill = $('paper-pill');
  if (o.paper.running) { pill.className = 'badge good'; pill.innerHTML = '<span class="dot pulse"></span>Paper trading: running'; }
  else if (o.paper.error) { pill.className = 'badge bad'; pill.textContent = 'Paper trading: crashed'; }
  else { pill.className = 'badge'; pill.textContent = 'Paper trading: stopped'; }

  const job = o.job, jp = $('job-pill');
  if (job && job.status === 'running') {
    jp.hidden = false; jp.className = 'badge accent';
    const p = job.progress ? ` ${job.progress.done}/${job.progress.total}` : '';
    jp.innerHTML = `<span class="dot pulse"></span>${esc(jobName(job.kind))}…${p}`;
  } else jp.hidden = true;

  let setup = '';
  const busy = job && job.status === 'running';
  if (SNAPSHOT) {
    pill.className = 'badge accent';
    pill.textContent = 'Read-only snapshot';
    setup = `<div class="banner"><p><b>Snapshot published ${when(SNAPSHOT.generated_at)}</b> (${ago(SNAPSHOT.generated_at)}). Prices, signals and positions are as of then. Paper trading, backtests and every control run in the V-trade app on your computer; run <code>run.bat publish</code> there to update this page.</p></div>`;
    html('setup', setup);
    return;
  }
  if (!o.data.available) {
    setup = `<div class="banner"><p><b>Step 1:</b> download price history (about 5 years of ${esc(o.market.symbol)} candles, ~20 seconds).</p><button class="btn primary" data-action="job" data-kind="fetch" ${busy ? 'disabled' : ''}>Download candles</button></div>`;
  } else if (!o.model.available) {
    setup = `<div class="banner"><p><b>Step 2:</b> train the model on the downloaded candles.</p><button class="btn primary" data-action="job" data-kind="train" ${busy ? 'disabled' : ''}>Train model</button></div>`;
  } else if (S.signalError && ['dashboard', 'how'].includes(S.view)) {
    setup = `<div class="banner warn"><p>Live data unavailable: ${esc(S.signalError)}</p><button class="btn" data-action="refresh">Retry</button></div>`;
  }
  if (o.paper.error) setup += `<div class="banner bad"><p>The paper engine stopped with an error: ${esc(o.paper.error)}</p></div>`;
  html('setup', setup);
}

const jobName = (k) => ({ fetch: 'Updating candles', train: 'Training model', backtest: 'Running backtest' }[k] || k);

// ------------------------------------------------------------------ dashboard
function renderDashboard() {
  const o = S.overview, sig = S.signal, cfg = o?.config;
  if (!o) return;
  const P = palette();
  const pe = paperEquity();
  const start = cfg.risk.starting_equity;
  const risk = pe.st?.risk;
  const kpis = [];
  kpis.push(kpi(esc(o.market.symbol), sig ? money(sig.price) : '–', sig ? `<span class="${tone(sig.change_24h)}">${pct(sig.change_24h, 2)}</span> in 24 h · candle ${candleLabel(sig.bar)}` : 'waiting for data'));
  kpis.push(kpi('Model P(up)', sig ? `${sig.prob.toFixed(2)} ${decisionBadge(sig.decision)}` : '–', '', sig ? meter(sig.prob, cfg.strategy.entry_threshold, cfg.strategy.exit_threshold) : ''));
  kpis.push(kpi('Paper account', money(pe.equity, 2), `<span class="${tone(pe.equity - start)}">${pct(pe.equity / start - 1, 2)}</span> since start`));
  kpis.push(pe.pos
    ? kpi('Open position', `${num(pe.pos.qty, 4)} BTC`, `entry ${money(pe.pos.entry_price)} · <span class="${tone(pe.unreal)}">${signedMoney(pe.unreal)}</span>`)
    : kpi('Open position', 'Flat', 'no trade open'));
  kpis.push(risk?.halted
    ? kpi('Risk', '<span class="down">Halted</span>', esc(risk.halt_reason))
    : kpi('Risk', '<span class="up">OK</span>', `stop at −${(cfg.risk.max_drawdown * 100).toFixed(0)}% drawdown · ${(cfg.risk.risk_per_trade * 100).toFixed(0)}% per trade`));
  const nw = o.news || {};
  if (nw.enabled) {
    kpis.push(nw.blackout
      ? kpi('<a href="#news">News</a>', '<span class="down">Blackout</span>', `${esc(nw.blackout.currency)} ${esc(nw.blackout.title)} · ${dayTime(nw.blackout.time)}`)
      : nw.next ? kpi('<a href="#news">Next big news</a>', until(nw.next.time).replace('in ', ''), `${esc(nw.next.currency)} ${esc(nw.next.title)} · ${dayTime(nw.next.time)}`)
        : kpi('<a href="#news">Next big news</a>', 'None', 'no matching events left this week'));
  }
  if (S.live && S.copyAcct) {
    const c = S.copyAcct;
    kpis.push(kpi('<a href="#traders">Online copy account</a>', money(c.equity, 2), `<span class="${tone(c.equity - c.starting_equity)}">${pct(c.equity / c.starting_equity - 1, 2)}</span> · ${c.running ? 'copying now' : 'stopped'}`));
  }
  const tr = S.traders;
  if (cfg.copy.enabled) {
    kpis.push(tr && !tr.error
      ? kpi(`<a href="#traders">Top traders on ${esc(tr.coin)}</a>`, `<span class="${tone(tr.bias)}">${biasNum(tr.bias)}</span>`, `${tr.longs} long · ${tr.shorts} short · ${tr.flat} flat`)
      : kpi('<a href="#traders">Top traders</a>', '…', S.tradersError ? esc(S.tradersError) : 'loading positions'));
  }
  html('dash-kpis', kpis.join(''));

  if (sig) {
    const labels = sig.history.t.map(candleLabel), n = labels.length;
    const ds = [lineDs('Close', sig.history.close, P.accent)];
    const leg = [[P.accent, 'Close']];
    if (pe.pos) {
      ds.push(hlineDs('Take-profit', n, pe.pos.take_profit, P.good), hlineDs('Entry', n, pe.pos.entry_price, P.gray), hlineDs('Stop-loss', n, pe.pos.stop, P.bad));
      leg.push([P.good, 'Take-profit', 1], [P.gray, 'Entry', 1], [P.bad, 'Stop-loss', 1]);
    }
    html('dash-price-legend', legend(leg));
    $('dash-price-hint').textContent = `Last ${n} candles · updated ${ago(sig.fetched_at)}`;
    chart('c-dash-price', labels, ds, { y: (v) => money(v) });
    html('dash-prob-legend', legend([[P.accent, 'P(up)'], [P.good, `Buy line ${cfg.strategy.entry_threshold}`, 1], [P.bad, `Sell line ${cfg.strategy.exit_threshold}`, 1]]));
    chart('c-dash-prob', labels, [lineDs('P(up)', sig.history.prob, P.accent), hlineDs('Buy line', n, cfg.strategy.entry_threshold, P.good), hlineDs('Sell line', n, cfg.strategy.exit_threshold, P.bad)], { y: (v) => Number(v).toFixed(2), yMin: 0, yMax: 1 });
  }

  // decision card
  let dec = '<h2>What the bot would do now</h2>';
  if (!sig) dec += empty('Waiting for the live signal.');
  else {
    const plan = sig.plan;
    dec += `<p class="hint">For the candle that closed at ${candleLabel(sig.bar)}</p>
      <div class="row" style="margin-bottom:10px">${decisionBadge(sig.decision)}<span>${esc(sig.reason)}</span></div>`;
    if (plan) {
      dec += `<p class="note" style="margin:0 0 8px">${sig.decision === 'enter' ? 'The order it would place:' : 'If it did buy now, the order would be:'}</p>` + kv([
        ['Buy at', money(sig.price)],
        [`Stop-loss (${cfg.risk.stop_atr_mult} × ATR below)`, money(plan.stop)],
        [`Take-profit (${cfg.risk.take_profit_atr_mult} × ATR above)`, money(plan.take_profit)],
        ['Size', `${num(plan.qty, 4)} BTC · ${money(plan.notional)}`],
        ['Loss if the stop is hit', `${money(plan.loss_at_stop, 2)} + fees`],
      ]);
    }
    const gates = [];
    if (nw.enabled) gates.push(nw.blackout ? `<span class="badge bad">News blackout</span> ${esc(nw.blackout.title)} at ${dayTime(nw.blackout.time)}` : '<span class="badge good">News clear</span> no big release inside the blackout window');
    if (cfg.copy.enabled && tr && !tr.error) gates.push(`<span class="badge ${tr.bias != null && tr.bias > 0 ? 'good' : 'warn'}">Top traders ${biasText(tr.bias)}</span> bias ${biasNum(tr.bias)} · paper strategy: ${esc(MODE_NAME[o.paper.copy_mode] || '')}`);
    if (gates.length) dec += `<div class="list" style="margin-top:10px">${gates.map((g) => `<div class="item"><span>${g}</span></div>`).join('')}</div>`;
    dec += `<p class="note">${o.paper.running ? 'Paper trading is running and will act on the next candle close.' : 'Paper trading is stopped, so nothing is traded. <a href="#paper">Start it</a> to act on signals.'}</p>`;
    if (plan) dec += `<div class="row" style="margin-top:12px"><a class="btn" href="${FORTRADE_URL}" target="_blank" rel="noopener">Practice this on Fortrade ↗</a><span class="muted" style="font-size:12.5px">Fortrade has no API, so place it by hand: buy BTC/USD, stop ${money(plan.stop)}, target ${money(plan.take_profit)}.</span></div>`;
  }
  html('dash-decision', dec);

  // activity
  const items = [];
  (S.paper?.fills || []).forEach((f) => items.push({ ts: f.ts, html: `${f.side === 'buy' ? '<span class="badge good">Buy</span>' : '<span class="badge bad">Sell</span>'} ${num(f.qty, 4)} @ ${money(f.price)} ${f.pnl != null ? `<span class="${tone(f.pnl)}">${signedMoney(f.pnl)}</span>` : ''} <span class="muted">${esc(f.reason || '')}</span>` }));
  (S.paper?.events || []).forEach((e) => items.push({ ts: e.ts, html: eventHtml(e) }));
  items.sort((a, b) => (a.ts < b.ts ? 1 : -1));
  html('dash-activity', items.length
    ? items.slice(0, 8).map((i) => `<div class="item"><span>${i.html}</span><span class="when">${when(i.ts)}</span></div>`).join('')
    : empty('No paper trades yet. Start paper trading and the bot acts at each candle close.', '<button class="btn" data-action="paper-start">Start paper trading</button>'));
}

function eventHtml(e) {
  let detail = e.detail;
  try { detail = JSON.parse(e.detail); } catch { /* plain text */ }
  if (e.kind === 'llm_review' && typeof detail === 'object') {
    const b = detail.approved ? '<span class="badge good">Claude approved</span>' : '<span class="badge warn">Claude vetoed</span>';
    return `${b} <span class="muted">${num(detail.confidence, 2)} · ${esc(detail.reasoning)}</span>`;
  }
  const badgeCls = { halted: 'bad', error: 'bad', blocked: 'warn', reconcile: 'warn' }[e.kind] || '';
  return `<span class="badge ${badgeCls}">${esc(e.kind)}</span> <span class="muted">${esc(typeof detail === 'string' ? detail : JSON.stringify(detail))}</span>`;
}

// ------------------------------------------------------------------ how it works
function howSteps() {
  const o = S.overview, cfg = o?.config, sig = S.signal, bt = S.backtest, P = palette();
  if (!cfg) return [];
  const tfh = tfHours(cfg.timeframe);
  const liveMissing = () => ({ stats: '', extra: empty(S.signalError ? `Live data unavailable: ${esc(S.signalError)}` : 'Loading live data…'), chart: null });
  const btMissing = () => ({ stats: '', extra: empty('Run a backtest to see this step with real results.', '<button class="btn primary" data-action="job" data-kind="backtest">Run backtest</button>'), chart: null });
  const f = sig?.features || {};
  const horizonH = cfg.model.horizon * tfh;
  return [
    {
      t: 'Read the market',
      text: `Every ${cfg.engine.poll_seconds} seconds the bot checks the price. When a ${cfg.timeframe} candle closes, it downloads the latest candles from ${cfg.exchange.id}. These are the last ${sig ? sig.history.t.length : 72} candles of ${cfg.symbol}, fetched live.`,
      render() {
        if (!sig) return liveMissing();
        const c = sig.history.close;
        return {
          stats: mini('Last close', money(sig.price), candleLabel(sig.bar)) + mini('Low', money(Math.min(...c))) + mini('High', money(Math.max(...c))) + mini('24 h change', `<span class="${tone(sig.change_24h)}">${pct(sig.change_24h, 2)}</span>`),
          legend: [[P.accent, 'Close']],
          chart: [sig.history.t.map(candleLabel), [lineDs('Close', c, P.accent)], { y: (v) => money(v) }],
        };
      },
    },
    {
      t: 'Turn prices into numbers',
      text: 'The model can\'t read a chart, so the bot describes the latest candle with 25 measurements. Six of them, as they stand right now:',
      render() {
        if (!sig) return liveMissing();
        const rsi = f.rsi_14 * 100, vr = f.vol_ratio, vz = f.volume_z;
        return {
          stats: mini('Trend', pct(f.dist_ema_200, 1), `${f.dist_ema_200 >= 0 ? 'above' : 'below'} the 200-candle average`)
            + mini('Momentum (RSI)', rsi.toFixed(1), rsi > 70 ? 'overbought' : rsi < 30 ? 'oversold' : 'neutral, 50 is the middle')
            + mini('Last 24 candles', pct(f.ret_24, 2), 'price change')
            + mini('Volatility', `${vr.toFixed(2)}×`, vr > 1.2 ? 'choppier than usual' : vr < 0.8 ? 'calmer than usual' : 'about normal')
            + mini('Typical move (ATR)', money(sig.atr), `${pct(f.atr_pct, 2, false)} of price per candle`)
            + mini('Volume', vz > 1 ? 'Busy' : vz < -0.3 ? 'Quiet' : 'Normal', `${vz >= 0 ? '+' : '−'}${Math.abs(vz).toFixed(1)} std vs average`),
          extra: '<p class="note">The other 19 cover shorter and longer returns, MACD, Bollinger bands, distance to the 20 and 50-candle averages, candle shape, hour of day and day of week.</p>',
          chart: null,
        };
      },
    },
    {
      t: 'Predict',
      text: `A gradient-boosted model, trained on years of candles, turns those measurements into one number: the chance ${cfg.symbol.split('/')[0]} rises more than ${(cfg.model.label_threshold * 100).toFixed(1)}% in the next ${horizonH} hours. The bot buys at ${cfg.strategy.entry_threshold} or higher and sells when it falls to ${cfg.strategy.exit_threshold}.`,
      render() {
        if (!sig) return liveMissing();
        const labels = sig.history.t.map(candleLabel), n = labels.length;
        return {
          stats: mini('P(up) now', sig.prob.toFixed(2), candleLabel(sig.bar)) + mini('Buy at', `≥ ${cfg.strategy.entry_threshold}`, 'entry line') + mini('Decision', sig.decision === 'enter' ? 'Buy' : 'Hold', esc(sig.reason)),
          legend: [[P.accent, 'P(up) per candle'], [P.good, `Buy line ${cfg.strategy.entry_threshold}`, 1], [P.bad, `Sell line ${cfg.strategy.exit_threshold}`, 1]],
          chart: [labels, [lineDs('P(up)', sig.history.prob, P.accent), hlineDs('Buy line', n, cfg.strategy.entry_threshold, P.good), hlineDs('Sell line', n, cfg.strategy.exit_threshold, P.bad)], { y: (v) => Number(v).toFixed(2), yMin: 0, yMax: 1 }],
        };
      },
    },
    {
      t: 'Size the trade',
      text: `${sig && sig.decision === 'enter' ? 'The model says buy.' : 'Say the model had said buy on the last candle.'} Before any order goes out, the risk manager sets a stop, a target and a size so that hitting the stop costs about ${(cfg.risk.risk_per_trade * 100).toFixed(0)}% of the account.`,
      render() {
        if (!sig) return liveMissing();
        const p = sig.plan;
        if (!p) return { stats: '', extra: empty('No valid order right now (size below the minimum or no ATR).'), chart: null };
        const labels = sig.history.t.map(candleLabel), n = labels.length;
        const eq = cfg.risk.starting_equity;
        const capped = p.uncapped_qty && p.uncapped_qty > p.qty * 1.001;
        return {
          stats: mini('Buy at', money(sig.price), 'last price') + mini('Stop-loss', money(p.stop), `${cfg.risk.stop_atr_mult} × ${money(sig.atr)} below`) + mini('Take-profit', money(p.take_profit), `${cfg.risk.take_profit_atr_mult} × ${money(sig.atr)} above`) + mini('Size', `${num(p.qty, 4)} BTC`, `${money(p.notional)} of ${money(eq)}`),
          extra: `<p class="note">Risking ${(cfg.risk.risk_per_trade * 100).toFixed(0)}% (${money(eq * cfg.risk.risk_per_trade)}) on a ${money(sig.price - p.stop)} stop would mean ${num(p.uncapped_qty, 4)} BTC (${money(p.uncapped_qty * sig.price)}).
            ${capped ? `That breaks the ${(cfg.risk.max_position_pct * 100).toFixed(0)}%-of-account cap, so the order shrinks to ${money(p.notional)}.` : 'That fits within the position cap.'}
            If the stop is hit, the loss is about ${money(p.loss_at_stop)} plus fees.</p>`,
          legend: [[P.accent, 'Close'], [P.good, 'Take-profit', 1], [P.gray, 'Buy price', 1], [P.bad, 'Stop-loss', 1]],
          chart: [labels, [lineDs('Close', sig.history.close, P.accent), hlineDs('Take-profit', n, p.take_profit, P.good), hlineDs('Buy price', n, sig.price, P.gray), hlineDs('Stop-loss', n, p.stop, P.bad)], { y: (v) => money(v) }],
        };
      },
    },
    {
      t: 'Manage the trade',
      text: `After buying, the bot checks the price every ${cfg.engine.poll_seconds} seconds. It sells at the target, at the stop, when P(up) falls to ${cfg.strategy.exit_threshold}, or after ${cfg.strategy.max_hold_bars * tfh} hours, whichever comes first. Two real trades from the backtest:`,
      render() {
        const ex = bt?.examples || [];
        if (!bt?.available || !ex.length) return btMissing();
        const i = Math.min(S.howTrade, ex.length - 1), x = ex[i], n = x.close.length, win = x.pnl > 0;
        return {
          stats: mini(win ? 'Winner' : 'Loser', `<span class="${tone(x.pnl)}">${signedMoney(x.pnl)}</span>`, `${pct(x.return_pct, 2)} on the trade`) + mini('Bought', money(x.entry_price), x.entry_time) + mini('Sold', money(x.exit_price), x.reason === 'take_profit' ? 'hit take-profit' : 'hit stop-loss') + mini('Held', `${x.bars_held * tfh} hours`, `${money(x.fees, 2)} in fees`),
          extra: `<div class="row" style="margin-top:12px">${ex.map((e, j) => `<button class="step-pill ${j === i ? 'on' : ''}" data-action="how-trade" data-i="${j}">${e.reason === 'take_profit' ? 'Take-profit trade' : 'Stop-loss trade'}</button>`).join('')}</div>`,
          legend: [[P.accent, 'Close'], [P.good, 'Take-profit', 1], [P.bad, 'Stop-loss', 1], [P.accent, 'Buy ▲'], [P.gray, 'Sell ◆']],
          chart: [x.t, [lineDs('Close', x.close, P.accent), hlineDs('Take-profit', n, x.take_profit, P.good), hlineDs('Stop-loss', n, x.stop, P.bad), markerDs('Buy', n, x.entry_idx, x.entry_price, P.accent, 'triangle'), markerDs('Sell', n, x.exit_idx, x.exit_price, P.gray, 'rectRot')], { y: (v) => money(v) }],
        };
      },
    },
    {
      t: 'The result',
      text: 'The backtest runs this loop over years of data the model never trained on, with fees and slippage. This is what it would have done:',
      render() {
        if (!bt?.available) return btMissing();
        const s = bt.summary, m = s.metrics, b = s.benchmark_metrics;
        return {
          stats: mini('V-trade', `<span class="${tone(m.total_return)}">${pct(m.total_return)}</span>`, `${money(s.starting_equity)} → ${money(s.final_equity)}`) + mini('Buy & hold', `<span class="${tone(b.total_return)}">${pct(b.total_return)}</span>`, `${money(s.starting_equity)} → ${money(s.final_benchmark)}`) + mini('Worst drop', `${pct(m.max_drawdown, 0)} vs ${pct(b.max_drawdown, 0)}`, 'bot vs buy & hold') + mini('Trades', num(m.trades, 0), `${pct(m.win_rate, 0, false)} winners`),
          extra: `<p class="note">${m.total_return < 0 ? 'The model\'s small edge was smaller than the fees, so the bot lost money.' : 'The bot finished positive in this test. Check the drawdown and number of trades before trusting it.'} ${s.halted ? `The kill switch stopped it: ${esc(s.halted)}.` : ''}</p>`,
          legend: [[P.accent, 'V-trade'], [P.gray, 'Buy & hold', 1]],
          chart: [bt.equity.t, [lineDs('V-trade', bt.equity.bot, P.accent, { tension: 0 }), lineDs('Buy & hold', bt.equity.hold, P.gray, { borderDash: [6, 4], tension: 0 })], { y: (v) => '$' + Math.round(v / 1000) + 'k', xFmt: (l) => l.slice(0, 7) }],
        };
      },
    },
  ];
}

function renderHow() {
  const steps = howSteps();
  if (!steps.length) return;
  S.howStep = Math.min(S.howStep, steps.length - 1);
  const st = steps[S.howStep];
  html('how-steps', steps.map((x, i) => `<button class="step-pill ${i === S.howStep ? 'on' : ''}" data-action="how-step" data-i="${i}">${i + 1}. ${esc(x.t)}</button>`).join(''));
  $('how-title').textContent = `Step ${S.howStep + 1} · ${st.t}`;
  $('how-text').textContent = st.text;
  const r = st.render();
  html('how-stats', r.stats || '');
  html('how-extra', r.extra || '');
  html('how-legend', r.legend ? legend(r.legend) : '');
  const wrap = $('how-chart-wrap');
  if (r.chart) {
    wrap.style.display = '';
    const [labels, ds, opts] = r.chart;
    chart('c-how', labels, ds, opts);
    S.charts['c-how']?.resize();
  } else wrap.style.display = 'none';
  $('how-pos').textContent = `${S.howStep + 1} of ${steps.length}`;
  $('how-back').style.visibility = S.howStep ? 'visible' : 'hidden';
  $('how-next').style.visibility = S.howStep < steps.length - 1 ? 'visible' : 'hidden';
}

// ------------------------------------------------------------------ paper trading
function renderPaper() {
  const o = S.overview, p = S.paper;
  if (!o || !p) return;
  const P = palette();
  const pe = paperEquity();
  const start = p.starting_equity;
  const st = p.state;
  const halted = st?.risk?.halted;
  html('paper-status-title', p.running ? '<span class="badge good"><span class="dot pulse"></span>Running</span>&nbsp; Paper trading' : 'Paper trading is stopped');
  $('paper-status-hint').textContent = p.running
    ? `Started ${when(p.started_at)}. Checks the price every ${o.config.engine.poll_seconds} s and decides at each ${o.config.timeframe} candle close.`
    : 'A pretend account that trades live prices with the same rules as the backtest. Nothing real is bought or sold.';
  let controls = p.running
    ? '<button class="btn" data-action="paper-stop">Stop</button>'
    : `<button class="btn primary" data-action="paper-start" ${o.model.available ? '' : 'disabled title="Train a model first"'}>Start paper trading</button><button class="btn danger" data-action="paper-reset">Reset account</button>`;
  if (halted && !p.running) controls += '<button class="btn" data-action="paper-reset-halt">Clear kill switch</button>';
  html('paper-controls', modeSelect(o.paper.copy_mode) + controls);
  if (p.running && o.paper.running_copy_mode && o.paper.running_copy_mode !== o.paper.copy_mode) {
    $('paper-status-hint').textContent += ' The new strategy applies after you stop and start again.';
  } else if (p.running && o.paper.running_copy_mode) {
    $('paper-status-hint').textContent += ` Strategy: ${MODE_NAME[o.paper.running_copy_mode]}.`;
  }

  const wins = p.stats.wins, closed = p.stats.closed_trades;
  html('paper-kpis', [
    kpi('Account value', money(pe.equity, 2), `started with ${money(start)}`),
    kpi('Cash', money(pe.cash, 2), pe.pos ? 'rest is in the open position' : 'fully in cash'),
    kpi('Realized P&L', `<span class="${tone(st?.realized_pnl)}">${signedMoney(st?.realized_pnl || 0)}</span>`, 'from closed trades'),
    kpi('Return', `<span class="${tone(pe.equity - start)}">${pct(pe.equity / start - 1, 2)}</span>`, 'including open position'),
    kpi('Closed trades', num(closed, 0), closed ? `${wins} winners (${pct(wins / closed, 0, false)})` : 'none yet'),
  ].join(''));

  const eqT = p.equity.t;
  if (eqT.length) chart('c-paper-equity', eqT.map(candleLabel), [lineDs('Account value', p.equity.equity, P.accent, { tension: 0 })], { y: (v) => money(v) });

  let pos = '<div class="card-head"><div><h2>Open position</h2><p class="hint">Stops and targets are watched on every poll</p></div></div>';
  if (pe.pos) {
    const x = pe.pos;
    pos += kv([
      ['Size', `${num(x.qty, 6)} BTC`], ['Entry price', money(x.entry_price, 2)], ['Entry time', when(x.entry_time)],
      ['Stop-loss', money(x.stop, 2)], ['Take-profit', money(x.take_profit, 2)], ['Held', `${x.bars_held} candles`],
      ['Unrealized P&L', `<span class="${tone(pe.unreal)}">${signedMoney(pe.unreal)}</span>`],
    ]);
  } else pos += empty(eqT.length ? 'Flat: no trade open. The bot buys when P(up) reaches the buy line.' : 'No history yet. The chart fills in at each candle close while paper trading runs.');
  if (halted) pos += `<div class="banner bad" style="margin-top:12px"><p>Kill switch: ${esc(st.risk.halt_reason)}</p></div>`;
  html('paper-position', pos);

  html('paper-fills', p.fills.length
    ? `<table><thead><tr><th>Time</th><th>Side</th><th class="num">Qty</th><th class="num">Price</th><th class="num">Fee</th><th class="num">P&amp;L</th><th>Reason</th></tr></thead><tbody>${p.fills.map((f) => `<tr><td>${when(f.ts)}</td><td>${f.side === 'buy' ? '<span class="badge good">Buy</span>' : '<span class="badge bad">Sell</span>'}</td><td class="num">${num(f.qty, 5)}</td><td class="num">${money(f.price, 2)}</td><td class="num">${money(f.fee, 2)}</td><td class="num ${tone(f.pnl)}">${f.pnl == null ? '' : signedMoney(f.pnl)}</td><td class="muted">${esc(f.reason || '')}</td></tr>`).join('')}</tbody></table>`
    : empty('No trades yet.'));
  html('paper-events', p.events.length
    ? p.events.map((e) => `<div class="item"><span>${eventHtml(e)}</span><span class="when">${when(e.ts)}</span></div>`).join('')
    : empty('No events yet. Claude reviews show up here when the reviewer is enabled.'));
}

async function pollLogs() {
  try {
    const { lines } = await api(`/api/logs?after=${S.logAfter}`);
    if (!lines.length) return;
    S.logAfter = lines[lines.length - 1].id;
    S.logLines = S.logLines.concat(lines).slice(-300);
    const el = $('paper-log');
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.innerHTML = S.logLines.map((l) => `<div class="${l.level}"><span class="t">${new Date(l.ts).toLocaleTimeString()}</span> ${esc(l.msg)}</div>`).join('');
    if (atBottom) el.scrollTop = el.scrollHeight;
  } catch { /* server restarting */ }
}

// ------------------------------------------------------------------ backtest
function jobProgress(kind) {
  const job = S.overview?.job;
  if (!job || job.kind !== kind) return '';
  if (job.status === 'running') {
    const p = job.progress;
    return p ? `<div class="progress"><div style="width:${(p.done / p.total) * 100}%"></div></div><p class="hint" style="margin:6px 0 0">${jobName(kind)}: ${p.done} of ${p.total} models trained</p>`
      : `<div class="progress indeterminate"><div></div></div><p class="hint" style="margin:6px 0 0">${jobName(kind)}…</p>`;
  }
  if (job.status === 'error') return `<div class="banner bad" style="margin:12px 0 0"><p>${jobName(kind)} failed: ${esc(job.error)}</p></div>`;
  return '';
}

function renderBacktest() {
  const bt = S.backtest, o = S.overview;
  if (!o) return;
  const P = palette();
  const running = o.job?.status === 'running';
  $('bt-run').disabled = running || !o.data.available;
  html('bt-progress', jobProgress('backtest'));
  if (!bt) return;
  if (!bt.available) {
    html('bt-body', `<div class="card">${empty('No backtest yet. It takes about 2 minutes: the model is retrained dozens of times so every prediction is out-of-sample.')}</div>`);
    return;
  }
  const s = bt.summary, m = s.metrics, b = s.benchmark_metrics;
  $('bt-hint').textContent = `${s.symbol} ${s.timeframe} · ${s.period.start.slice(0, 10)} to ${s.period.end.slice(0, 10)} · run ${when(s.generated_at)} · kill switch ${s.kill_switch ? 'on' : 'off'}`;
  const kpis = [
    kpi('V-trade return', `<span class="${tone(m.total_return)}">${pct(m.total_return)}</span>`, `${money(s.starting_equity)} → ${money(s.final_equity)}`),
    kpi('Buy & hold', `<span class="${tone(b.total_return)}">${pct(b.total_return)}</span>`, `${money(s.starting_equity)} → ${money(s.final_benchmark)}`),
    kpi('Max drawdown', pct(m.max_drawdown, 1), `buy & hold ${pct(b.max_drawdown, 1)}`),
    kpi('Sharpe ratio', num(m.sharpe, 2), `buy & hold ${num(b.sharpe, 2)}`),
    kpi('Trades', num(m.trades, 0), `${pct(m.win_rate, 0, false)} winners · PF ${num(m.profit_factor, 2)}`),
    kpi('Model AUC', num(s.model_report?.auc, 3), '0.5 = coin flip'),
  ];
  const rows = [
    ['Total return', pct(m.total_return), pct(b.total_return)], ['Yearly return (CAGR)', pct(m.cagr), pct(b.cagr)],
    ['Sharpe', num(m.sharpe), num(b.sharpe)], ['Sortino', num(m.sortino), num(b.sortino)],
    ['Max drawdown', pct(m.max_drawdown), pct(b.max_drawdown)], ['Calmar', num(m.calmar), num(b.calmar)],
    ['Time in market', pct(m.exposure, 1, false), '100%'], ['Win rate', pct(m.win_rate, 1, false), '–'],
    ['Profit factor', num(m.profit_factor), '–'], ['Average trade', pct(m.avg_trade, 2), '–'],
    ['Best / worst trade', `${pct(m.best_trade, 2)} / ${pct(m.worst_trade, 2)}`, '–'], ['Fees paid', money(m.total_fees), '–'],
  ];
  const reasons = Object.entries(bt.exit_reasons || {}).sort((a, c) => c[1] - a[1]);
  const maxR = Math.max(1, ...reasons.map((r) => r[1]));
  const reasonName = (r) => ({ stop_loss: 'Stop-loss hit', take_profit: 'Take-profit hit', 'signal faded': 'Signal faded', kill_switch: 'Kill switch', end_of_data: 'End of data' }[r] || r.replace(/^max hold.*/, 'Max hold time'));
  const trades = bt.trades.slice().reverse();
  const shown = S.allTrades ? trades : trades.slice(0, 25);
  html('bt-body', `
    ${s.halted ? `<div class="banner warn"><p>The kill switch fired during this backtest: ${esc(s.halted)}. The bot stayed flat afterwards.${SNAPSHOT ? '' : ' Tick "Ignore kill switch" to see the whole period.'}</p></div>` : ''}
    <div class="grid kpis">${kpis.join('')}</div>
    <div class="card" style="margin-bottom:14px">
      <div class="card-head"><div><h2>Account value</h2><p class="hint">Weekly, starting from ${money(s.starting_equity)}</p></div></div>
      <div class="legend">${legend([[P.accent, 'V-trade'], [P.gray, 'Buy & hold', 1]])}</div>
      <div class="chart tall"><canvas id="c-bt-equity" role="img" aria-label="Backtest account value versus buy and hold"></canvas></div>
    </div>
    <div class="grid two">
      <div class="card"><h2>Metrics</h2><p class="hint">Out-of-sample period only</p>
        <table><thead><tr><th></th><th class="num">V-trade</th><th class="num">Buy &amp; hold</th></tr></thead><tbody>${rows.map((r) => `<tr><td>${r[0]}</td><td class="num">${r[1]}</td><td class="num">${r[2]}</td></tr>`).join('')}</tbody></table>
      </div>
      <div class="card"><h2>Why trades ended</h2><p class="hint">${num(m.trades, 0)} trades</p>
        ${reasons.map(([r, c]) => `<div style="margin:10px 0"><div class="row" style="justify-content:space-between"><span>${esc(reasonName(r))}</span><span class="muted">${c}</span></div><div class="meter" style="margin:4px 0 0"><div class="fill" style="width:${(c / maxR) * 100}%"></div></div></div>`).join('')}
      </div>
    </div>
    <div class="card">
      <div class="card-head"><div><h2>Trades</h2><p class="hint">${S.allTrades ? `All ${trades.length}` : `Latest ${shown.length} of ${trades.length}`}, newest first</p></div>
        <button class="btn" data-action="toggle-trades">${S.allTrades ? 'Show latest 25' : 'Show all'}</button></div>
      <div class="table-wrap"><table><thead><tr><th>Entry</th><th>Exit</th><th class="num">Entry price</th><th class="num">Exit price</th><th class="num">P&amp;L</th><th class="num">Return</th><th class="num">Candles</th><th>Reason</th></tr></thead>
      <tbody>${shown.map((t) => `<tr><td>${t.entry_time}</td><td>${t.exit_time}</td><td class="num">${money(t.entry_price, 2)}</td><td class="num">${money(t.exit_price, 2)}</td><td class="num ${tone(t.pnl)}">${signedMoney(t.pnl)}</td><td class="num ${tone(t.return_pct)}">${pct(t.return_pct, 2)}</td><td class="num">${t.bars_held}</td><td class="muted">${esc(t.exit_reason)}</td></tr>`).join('')}</tbody></table></div>
    </div>`);
  if (S.charts['c-bt-equity'] && S.charts['c-bt-equity'].canvas !== $('c-bt-equity')) {
    S.charts['c-bt-equity'].destroy();  // the canvas was replaced by a re-render
    delete S.charts['c-bt-equity'];
  }
  chart('c-bt-equity', bt.equity.t, [lineDs('V-trade', bt.equity.bot, P.accent, { tension: 0 }), lineDs('Buy & hold', bt.equity.hold, P.gray, { borderDash: [6, 4], tension: 0 })], { y: (v) => money(v), xFmt: (l) => l.slice(0, 7) });
}

// ------------------------------------------------------------------ model & data
function renderModel() {
  const o = S.overview;
  if (!o) return;
  const d = o.data, m = o.model, job = o.job;
  const busy = job?.status === 'running';
  html('model-data', `<div class="card-head"><div><h2>Market data</h2><p class="hint">${esc(o.market.symbol)} ${esc(o.market.timeframe)} candles from ${esc(o.market.exchange)}</p></div>
      <button class="btn" data-action="job" data-kind="fetch" ${busy ? 'disabled' : ''}>${d.available ? 'Update candles' : 'Download candles'}</button></div>
    ${d.available ? kv([['Candles', num(d.rows, 0)], ['First candle', when(d.start)], ['Last candle', when(d.end)], ['Stored in', `<code>${esc(d.path.split(/[\\/]/).slice(-2).join('/'))}</code>`]]) : empty('No data yet.')}
    <p class="note">Updating only downloads candles newer than the last one stored.</p>`);
  const wf = m.walk_forward;
  html('model-model', `<div class="card-head"><div><h2>Signal model</h2><p class="hint">Gradient-boosted trees over 25 features</p></div>
      <button class="btn primary" data-action="job" data-kind="train" ${busy || !d.available ? 'disabled' : ''}>${m.available ? 'Retrain model' : 'Train model'}</button></div>
    ${m.available ? kv([
      ['Trained', when(m.trained_at)], ['Training rows', num(m.train_rows, 0)], ['Training period', `${esc((m.train_start || '').slice(0, 10))} → ${esc((m.train_end || '').slice(0, 10))}`],
      ['Predicts', `rise > ${pct(m.label_threshold, 1, false)} within ${m.horizon} candles`],
      ...(wf ? [['Walk-forward AUC', `${num(wf.auc, 3)} <span class="muted">(0.5 = coin flip)</span>`], ['Walk-forward accuracy', pct(wf.accuracy, 1, false)]] : []),
    ]) : empty('No model trained yet.')}
    <label class="check" style="margin-top:12px"><input type="checkbox" data-action="eval-first" ${S.evalFirst ? 'checked' : ''}> Measure out-of-sample accuracy first (about 2 minutes)</label>
    <p class="note">Paper trading picks up a new model the next time you start it.</p>`);
  let jobHtml = '<h2>Tasks</h2><p class="hint">Recommended order: update candles → retrain model → run backtest</p>';
  if (!job) jobHtml += empty('No tasks run in this session yet.');
  else {
    const status = job.status === 'running' ? '<span class="badge accent"><span class="dot pulse"></span>Running</span>' : job.status === 'done' ? '<span class="badge good">Done</span>' : '<span class="badge bad">Failed</span>';
    jobHtml += `<div class="row" style="justify-content:space-between"><div class="row">${status}<b>${esc(jobName(job.kind))}</b></div><span class="muted">started ${when(job.started)}${job.finished ? ` · finished ${when(job.finished)}` : ''}</span></div>${jobProgress(job.kind)}`;
    if (job.status === 'done' && job.result) {
      const r = job.result;
      if (job.kind === 'fetch') jobHtml += `<p class="note">${num(r.rows, 0)} candles stored, up to ${when(r.end)}.</p>`;
      if (job.kind === 'train') jobHtml += `<p class="note">Trained on ${num(r.meta?.train_rows, 0)} rows.${r.report ? ` Walk-forward AUC ${num(r.report.auc, 3)}.` : ''}</p>`;
      if (job.kind === 'backtest') jobHtml += `<p class="note">Return ${pct(r.metrics?.total_return)} vs buy &amp; hold ${pct(r.benchmark_metrics?.total_return)}. <a href="#backtest">Open the backtest</a>.</p>`;
    }
  }
  html('model-job', jobHtml);
}

// ------------------------------------------------------------------ strategy selector
function modeSelect(current) {
  return `<select data-action="copy-mode" title="Paper trading strategy">${Object.entries(MODE_NAME).map(([v, t]) => `<option value="${v}" ${v === current ? 'selected' : ''}>Strategy: ${t}</option>`).join('')}</select>`;
}

// ------------------------------------------------------------------ news
function renderNews() {
  const n = S.news;
  if (!n) { html('news-body', `<div class="card">${empty(S.newsError ? `Calendar unavailable: ${esc(S.newsError)}` : 'Loading the calendar…')}</div>`); return; }
  if (n.enabled === false) { html('news-body', `<div class="card">${empty('The news filter is off (news.enabled in config.yaml).')}</div>`); return; }
  const st = n.settings;
  const rule = `Blocks new entries ${st.block_before_minutes} min before and ${st.block_after_minutes} min after <b>${esc(st.impacts.join('/'))}</b>-impact <b>${esc(st.currencies.join(', '))}</b> releases. Selling open positions before them: <b>${st.close_before_event ? 'on' : 'off'}</b>.`;
  let banner = '';
  if (n.blackout) banner = `<div class="banner bad"><p><b>Blackout now:</b> ${esc(n.blackout.currency)} ${esc(n.blackout.title)} at ${dayTime(n.blackout.time)}. The bot won't open new trades until the window passes.</p></div>`;
  else if (n.next) banner = `<div class="banner"><p><b>Next big release ${until(n.next.time)}:</b> ${esc(n.next.currency)} ${esc(n.next.title)} · ${dayTime(n.next.time)} · forecast ${esc(n.next.forecast || '–')}, previous ${esc(n.next.previous || '–')}</p></div>`;
  const currencies = [...new Set(n.events.map((e) => e.currency))].sort();
  const filters = `<div class="row" style="margin-bottom:10px">
      ${['High', 'Medium', 'Low', 'Holiday'].map((i) => `<label class="check"><input type="checkbox" data-action="news-impact" data-impact="${i}" ${S.newsImpacts[i] ? 'checked' : ''}> ${i}</label>`).join('')}
      <select data-action="news-currency"><option value="all" ${S.newsCurrency === 'all' ? 'selected' : ''}>All currencies</option><option value="relevant" ${S.newsCurrency === 'relevant' ? 'selected' : ''}>Only what the bot watches</option>${currencies.map((c) => `<option value="${c}" ${S.newsCurrency === c ? 'selected' : ''}>${c}</option>`).join('')}</select>
    </div>`;
  const now = Date.now();
  const shown = n.events.filter((e) => (S.newsImpacts[e.impact] ?? true) && (S.newsCurrency === 'all' || (S.newsCurrency === 'relevant' ? e.relevant : e.currency === S.newsCurrency)));
  let rows = '', lastDay = '';
  shown.forEach((e) => {
    const d = new Date(e.time);
    const day = d.toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric' });
    if (day !== lastDay) { rows += `<tr class="day-row"><td colspan="7">${day}</td></tr>`; lastDay = day; }
    const past = d.getTime() < now;
    const hot = n.blackout && n.blackout.time === e.time && n.blackout.title === e.title;
    rows += `<tr class="${hot ? 'hot' : past ? 'dim' : ''}"><td>${pad2(d.getHours())}:${pad2(d.getMinutes())}</td><td><b>${esc(e.currency)}</b></td><td>${impactBadge(e.impact)}</td><td>${esc(e.title)}${e.relevant ? ' <span class="badge accent">watched</span>' : ''}</td><td class="num">${esc(e.forecast || '')}</td><td class="num">${esc(e.previous || '')}</td><td class="muted">${until(e.time)}</td></tr>`;
  });
  html('news-body', `${banner}
    <div class="card" style="margin-bottom:14px"><p class="note" style="margin:0">${rule} Edit the <code>news:</code> section of <code>config.yaml</code> to change it. The calendar says <i>when</i> a release is due, not what it will say: the actual number only appears after the release.</p>
      <p class="hint" style="margin:8px 0 0">Updated ${when(n.status.fetched_at)}${n.status.error ? ` · last update failed: ${esc(n.status.error)}` : ''}</p></div>
    <div class="card">${filters}
      <div class="table-wrap"><table><thead><tr><th>Time</th><th>Currency</th><th>Impact</th><th>Event</th><th class="num">Forecast</th><th class="num">Previous</th><th></th></tr></thead><tbody>${rows || `<tr><td colspan="7">${empty('No events match these filters.')}</td></tr>`}</tbody></table></div>
    </div>`);
}

// ------------------------------------------------------------------ top traders
function renderTraders() {
  const t = S.traders, o = S.overview;
  if (!o) return;
  if (!o.config.copy.enabled) { html('traders-body', `<div class="card">${empty('Top traders are off (copy.enabled in config.yaml).')}</div>`); return; }
  if (!t) {
    html('traders-body', `<div class="card">${empty(S.tradersError ? `Top traders unavailable: ${esc(S.tradersError)}` : 'Loading the Hyperliquid leaderboard (about 40 MB; the first time takes around 20 seconds)…')}</div>`);
    return;
  }
  const period = o.config.copy.rank_by === 'allTime' ? 'of all time' : `this ${o.config.copy.rank_by}`;
  $('traders-title').textContent = `Top traders on ${t.coin}`;
  $('traders-hint').textContent = `${t.source === 'manual' ? 'The wallets listed in config.yaml' : `The ${t.traders.length} most profitable active Hyperliquid accounts ${period}`}${t.accounts_ranked ? ` (out of ${num(t.accounts_ranked, 0)})` : ''}: the traders copy-trading apps such as Invo follow. Public on-chain data, updated ${ago(t.updated_at)}.`;
  const kpis = [
    kpi("Leaders' bias", `<span class="${tone(t.bias)}">${biasNum(t.bias)}</span>`, biasText(t.bias), biasBar(t.bias)),
    kpi('Long', `${t.longs}`, `${compact(t.long_notional)} in positions`),
    kpi('Short', `${t.shorts}`, `${compact(t.short_notional)} in positions`),
    kpi('No position', `${t.flat}`, t.failed ? `${t.failed} could not be read` : `in ${esc(t.coin)} right now`),
  ];
  const mode = o.paper.copy_mode;
  const extra = { off: '', filter: ` Needs bias ≥ ${o.config.copy.min_bias}.`, follow: ` Buys at bias ≥ ${o.config.copy.follow_entry_bias}, sells at ≤ ${o.config.copy.follow_exit_bias}.` };
  const modeCard = `<div class="card" style="margin-bottom:14px"><div class="card-head"><div><h2>Use in paper trading</h2><p class="hint">Applies when paper trading starts. Live trading uses <code>copy.mode</code> in config.yaml.</p></div></div>
    <div class="mode">${Object.entries(MODE_NAME).map(([v, label]) => `<label class="${v === mode ? 'on' : ''}"><input type="radio" name="copy-mode" value="${v}" data-action="copy-mode" ${v === mode ? 'checked' : ''}><span><b>${label}</b><small>${MODE_HELP[v]}${extra[v]}</small></span></label>`).join('')}</div>
    <p class="note">Leaders often use 10–40× leverage and some positions are hedges. V-trade copies only the direction: it buys spot ${esc(t.coin)} with its own position sizing and stops, never shorts and never uses leverage. None of this can be backtested, because past leader positions aren't available.</p></div>`;
  const rows = t.traders.map((r, i) => {
    const side = r.side === 'long' ? '<span class="badge good">Long</span>' : r.side === 'short' ? '<span class="badge bad">Short</span>' : r.side === 'unknown' ? '<span class="badge warn">Error</span>' : '<span class="badge">Flat</span>';
    const held = r.side === 'long' || r.side === 'short';
    const pnlM = r.pnl ? r.pnl.month : null, pnlA = r.pnl ? r.pnl.allTime : null;
    return `<tr><td class="muted">${i + 1}</td><td><a class="addr" href="https://app.hyperliquid.xyz/explorer/address/${esc(r.address)}" target="_blank" rel="noopener">${esc(shortAddr(r.address))}</a>${r.name ? ` <span class="muted">${esc(r.name)}</span>` : ''}</td>
      <td class="num ${tone(pnlM)}">${compact(pnlM)}</td><td class="num ${tone(pnlA)}">${compact(pnlA)}</td><td class="num">${compact(r.account_value || r.leaderboard_account_value)}</td>
      <td>${side} ${held ? num(Math.abs(r.size), 3) : ''}</td><td class="num">${held ? compact(r.notional) : ''}</td><td class="num">${held && r.entry ? money(r.entry) : ''}</td><td class="num">${held && r.leverage ? `${r.leverage}×` : ''}</td>
      <td class="num ${tone(r.unrealized_pnl)}">${held ? compact(r.unrealized_pnl) : ''}</td><td class="num">${held && r.liquidation ? money(r.liquidation) : ''}</td><td class="num">${r.open_positions ?? ''}</td></tr>`;
  }).join('');
  html('traders-body', `<div class="grid kpis">${kpis.join('')}</div>${S.live ? copyCard() : modeCard}
    <div class="card"><div class="card-head"><div><h2>Leaders</h2><p class="hint">Click a wallet to see all its trades on Hyperliquid</p></div></div>
    <div class="table-wrap"><table><thead><tr><th>#</th><th>Wallet</th><th class="num">Month P&amp;L</th><th class="num">All-time</th><th class="num">Account</th><th>${esc(t.coin)} position</th><th class="num">Size ($)</th><th class="num">Entry</th><th class="num">Leverage</th><th class="num">Unrealized</th><th class="num">Liquidation</th><th class="num">Open positions</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="note">To follow specific traders (for example ones you like on Invo), put their 0x wallet addresses in <code>copy.leaders</code> in config.yaml.</p></div>`);
  if (S.live && S.copyAcct && S.copyAcct.equity_history.length) {
    const h = S.copyAcct.equity_history;
    chart('c-copy-equity', h.map((x) => candleLabel(x.t)), [lineDs('Account value', h.map((x) => x.equity), palette().accent, { tension: 0 })], { y: (v) => money(v) });
  }
}

function copyCard() {
  const c = S.copyAcct;
  if (!c) return `<div class="card" style="margin-bottom:14px">${empty(S.copyError ? `Online copy account unavailable: ${esc(S.copyError)}` : 'Loading the online copy account…')}</div>`;
  const start = c.starting_equity, r = c.rules;
  const busy = S.copyBusy ? 'disabled' : '';
  const buttons = c.running
    ? `<button class="btn" data-action="live-copy" data-op="check" ${busy}>Check now</button><button class="btn" data-action="live-copy" data-op="stop" ${busy}>Stop</button>`
    : `<button class="btn primary" data-action="live-copy" data-op="start" ${busy}>Start copying</button><button class="btn danger" data-action="live-copy" data-op="reset" ${busy}>Reset</button>`;
  const status = c.running
    ? `<span class="badge good"><span class="dot pulse"></span>Copying</span> since ${when(c.started_at)} · last check ${ago(c.last_run) || 'pending'}`
    : '<span class="badge">Stopped</span>';
  const p = c.position;
  const position = p ? kv([
    ['Holding', `${num(p.qty, 5)} ${esc(r.coin)} · ${money(p.qty * c.last_price)}`], ['Bought at', `${money(p.entry_price, 2)} · ${when(p.entry_time)}`],
    ['Stop-loss / target', `${money(p.stop)} / ${money(p.take_profit)}`], ['Unrealized P&L', `<span class="${tone(p.unrealized_pnl)}">${signedMoney(p.unrealized_pnl)}</span>`],
  ]) : `<p class="note" style="margin:0">No position: it buys when the leaders' bias reaches ${biasNum(r.entry_bias)}.</p>`;
  const trades = c.trades.slice(0, 15).map((t) => `<tr><td>${when(t.ts)}</td><td>${t.side === 'buy' ? '<span class="badge good">Buy</span>' : '<span class="badge bad">Sell</span>'}</td><td class="num">${num(t.qty, 5)}</td><td class="num">${money(t.price, 2)}</td><td class="num ${tone(t.pnl)}">${t.pnl == null ? '' : signedMoney(t.pnl)}</td><td class="muted">${esc(t.reason)}</td></tr>`).join('');
  const events = c.events.slice(0, 5).map((e) => `<div class="item"><span><span class="badge ${e.kind === 'error' || e.kind === 'halted' ? 'bad' : e.kind === 'blocked' ? 'warn' : ''}">${esc(e.kind)}</span> <span class="muted">${esc(e.detail)}</span></span><span class="when">${when(e.ts)}</span></div>`).join('');
  return `<div class="card" style="margin-bottom:14px">
    <div class="card-head"><div><h2>Online copy account</h2><p class="hint">A ${money(start)} paper account running on Netlify. Every ${r.check_every_minutes} minutes it checks these leaders and copies them, even when your computer is off.</p></div><div class="row">${buttons}</div></div>
    <div class="row" style="margin-bottom:12px">${status}</div>
    <div class="grid kpis" style="margin-bottom:12px">
      ${mini('Account value', money(c.equity, 2), `<span class="${tone(c.equity - start)}">${pct(c.equity / start - 1, 2)}</span> since start`)}
      ${mini('Cash', money(c.cash, 2))}
      ${mini('Realized P&L', `<span class="${tone(c.realized_pnl)}">${signedMoney(c.realized_pnl)}</span>`)}
      ${mini('Closed trades', num(c.closed_trades, 0), c.closed_trades ? `${c.wins} winners` : 'none yet')}
    </div>
    <p class="note" style="margin:0 0 10px"><b>Last decision:</b> ${esc(c.last_decision)}${c.last_bias != null ? ` · leaders' bias ${biasNum(c.last_bias)}` : ''}. Buys at ${biasNum(r.entry_bias)} or more, sells at ${biasNum(r.exit_bias)} or less, risks ${(r.risk_per_trade * 100).toFixed(0)}% per trade.</p>
    ${c.halted ? `<div class="banner bad"><p>Kill switch: ${esc(c.halt_reason)}. Reset the account to start over.</p></div>` : ''}
    ${position}
    ${c.equity_history.length ? '<div class="chart" style="margin-top:12px"><canvas id="c-copy-equity" role="img" aria-label="Online copy account value over time"></canvas></div>' : ''}
    ${trades ? `<div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>Time</th><th>Side</th><th class="num">Qty</th><th class="num">Price</th><th class="num">P&amp;L</th><th>Reason</th></tr></thead><tbody>${trades}</tbody></table></div>` : ''}
    ${events ? `<div class="list" style="margin-top:10px">${events}</div>` : ''}
    ${c.admin_key_configured ? '' : '<div class="banner warn"><p><b>Controls locked:</b> the admin key is not set on Netlify yet, so Start, Stop and Reset are refused. Add <code>VTRADE_ADMIN_KEY</code> to the environment variables of the Netlify project, then publish again.</p></div>'}
    <p class="note">Paper money only. Anyone with this link can watch the account; only someone with your admin key can start, stop or reset it. ${adminKey() ? 'Your key is saved in this browser. <a href="#traders" data-action="forget-key">Forget it</a>' : 'The buttons ask for the key once.'}</p>
  </div>`;
}

// ------------------------------------------------------------------ settings
function renderSettings() {
  const o = S.overview;
  if (!o) return;
  const c = o.config;
  const pctv = (v) => pct(v, 2, false);
  const groups = [
    ['Market', [['Exchange', c.exchange.id + (c.exchange.sandbox ? ' (testnet)' : '')], ['Symbol', c.symbol], ['Timeframe', c.timeframe], ['History from', c.history_since]]],
    ['Model', [['Horizon', `${c.model.horizon} candles`], ['"Up" means a rise of more than', pctv(c.model.label_threshold)], ['Walk-forward: first prediction after', `${num(c.model.min_train_bars, 0)} candles`], ['Retrain every', `${num(c.model.retrain_every, 0)} candles`]]],
    ['Strategy', [['Buy when P(up) ≥', c.strategy.entry_threshold], ['Sell when P(up) ≤', c.strategy.exit_threshold], ['Only buy above the 200-candle average', c.strategy.trend_filter ? 'Yes' : 'No'], ['Max holding time', `${c.strategy.max_hold_bars} candles`]]],
    ['Risk', [['Account size (paper and backtest)', money(c.risk.starting_equity)], ['Risk per trade', pctv(c.risk.risk_per_trade)], ['Max position', pctv(c.risk.max_position_pct)], ['Stop-loss', `${c.risk.stop_atr_mult} × ATR`], ['Take-profit', `${c.risk.take_profit_atr_mult} × ATR`], ['Daily loss limit', pctv(c.risk.daily_loss_limit)], ['Kill switch drawdown', pctv(c.risk.max_drawdown)], ['Minimum order', money(c.risk.min_notional)]]],
    ['Costs', [['Fee per fill', pctv(c.costs.fee_rate)], ['Slippage per fill', pctv(c.costs.slippage)]]],
    ['Claude reviewer', [['Enabled', c.llm.enabled ? '<span class="badge good">On</span>' : '<span class="badge">Off</span>'], ['Model', `<code>${esc(c.llm.model)}</code>`], ['API key in .env', o.llm.key_set ? 'Set' : 'Not set'], ['Veto below confidence', c.llm.min_confidence], ['If the API fails', c.llm.on_error === 'veto' ? 'Skip the trade' : 'Trade anyway']]],
    ['Engine', [['Price check every', `${c.engine.poll_seconds} s`], ['Candles loaded for features', c.engine.warmup_bars]]],
    ['News (ForexFactory)', [['Enabled', c.news.enabled ? 'Yes' : 'No'], ['Watched currencies', c.news.currencies.join(', ')], ['Watched impact', c.news.impacts.join(', ')], ['Block before / after', `${c.news.block_before_minutes} / ${c.news.block_after_minutes} min`], ['Sell before big news', c.news.close_before_event ? 'Yes' : 'No']]],
    ['Top traders (Hyperliquid)', [['Enabled', c.copy.enabled ? 'Yes' : 'No'], ['Coin', c.copy.coin], ['Leaders', c.copy.leaders.length ? `${c.copy.leaders.length} chosen wallets` : `top ${c.copy.top_n} by ${c.copy.rank_by} profit`], ['Minimum account', money(c.copy.min_account_value)], ['Mode in config.yaml (live)', MODE_NAME[c.copy.mode]], ['Mode for paper trading', MODE_NAME[o.paper.copy_mode]]]],
  ];
  const live = o.live.state;
  html('settings-body', groups.map(([t, rows]) => `<div class="card"><h2>${t}</h2><div style="margin-top:8px">${kv(rows.map(([k, v]) => [esc(k), typeof v === 'string' && v.startsWith('<') ? v : esc(v)]))}</div></div>`).join('')
    + `<div class="card"><h2>Live trading</h2><p class="hint">Real money stays in the terminal on purpose</p>
      <p class="note" style="margin-top:0">Live trading needs exchange API keys in <code>.env</code> and a typed confirmation, so it is not a button here. Paper trade first, then run <code>python -m vtrade live</code> in the project folder.</p>
      <p class="note"><b>Fortrade</b> (<a href="${FORTRADE_URL}" target="_blank" rel="noopener">pro.fortrade.com</a>) has no public API, so V-trade can't place orders there. The dashboard shows each trade idea with its stop and target so you can practice it on your Fortrade demo account by hand.</p>
      ${live ? kv([['Live account cash', money(live.cash, 2)], ['Live position', live.position ? `${num(live.position.qty, 6)} BTC` : 'Flat'], ['Realized P&L', signedMoney(live.realized_pnl)]]) : '<p class="note">No live trading history.</p>'}</div>`);
}

// ------------------------------------------------------------------ routing & actions
function render() {
  renderChrome();
  ({ dashboard: renderDashboard, how: renderHow, paper: renderPaper, news: renderNews, traders: renderTraders, backtest: renderBacktest, model: renderModel, settings: renderSettings }[S.view] || (() => {}))();
}

function setView(view) {
  if (!TITLES[view]) view = 'dashboard';
  S.view = view;
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${view}`));
  document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.view === view));
  $('title').textContent = TITLES[view];
  document.title = `${TITLES[view]} · V-trade`;
  if (view === 'backtest' && !S.backtest) refreshBacktest().then(render);
  if (view === 'how' && !S.backtest) refreshBacktest().then(render);
  if (view === 'paper') pollLogs();
  if (view === 'news' && !S.news) refreshNews().then(render);
  if (view === 'traders' && !S.traders) refreshTraders().then(render);
  render();
  Object.values(S.charts).forEach((c) => c.resize());
}

async function startJob(kind, body = {}) {
  try {
    await api(`/api/jobs/${kind}`, 'POST', body);
    toast(`${jobName(kind)} started`);
    await tick();
  } catch (e) { toast(e.message, true); }
}

const actions = {
  job: (el) => {
    const kind = el.dataset.kind;
    if (kind === 'train') return startJob('train', { evaluate: S.evalFirst });
    if (kind === 'backtest') return startJob('backtest', { ignore_kill_switch: $('bt-ignore-ks').checked });
    return startJob(kind);
  },
  refresh: async () => { await refreshSignal(true); render(); toast('Live signal refreshed'); },
  'paper-start': async () => {
    try { await api('/api/paper/start', 'POST'); toast('Paper trading started. It acts at the next candle close.'); await tick(); } catch (e) { toast(e.message, true); }
  },
  'paper-stop': async () => { try { await api('/api/paper/stop', 'POST'); toast('Paper trading stopped'); await tick(); } catch (e) { toast(e.message, true); } },
  'paper-reset': async () => {
    if (!confirm(`Reset the paper account to ${money(S.overview.config.risk.starting_equity)}? The open position and P&L are cleared (the trade history stays in the journal).`)) return;
    try { await api('/api/paper/reset', 'POST'); toast('Paper account reset'); await tick(); } catch (e) { toast(e.message, true); }
  },
  'paper-reset-halt': async () => { try { await api('/api/paper/reset-halt', 'POST'); toast('Kill switch cleared'); await tick(); } catch (e) { toast(e.message, true); } },
  'how-step': (el) => { S.howStep = +el.dataset.i; renderHow(); },
  'how-trade': (el) => { S.howTrade = +el.dataset.i; renderHow(); },
  'toggle-trades': () => { S.allTrades = !S.allTrades; renderBacktest(); },
  'eval-first': (el) => { S.evalFirst = el.checked; },
  'live-copy': async (el) => {
    const op = el.dataset.op;
    if (op === 'reset' && !confirm('Reset the online copy account? Its trades and P&L are cleared.')) return;
    S.copyBusy = true; renderTraders();
    try {
      S.copyAcct = await liveApi('/api/live/copy', 'POST', { action: op });
      toast({ start: 'Copying started. It checks the leaders every 5 minutes.', stop: 'Copying stopped', check: `Checked: ${S.copyAcct.last_decision}`, reset: 'Copy account reset' }[op]);
      await refreshTraders();
    } catch (e) { toast(e.message, true); }
    S.copyBusy = false; render();
  },
  'forget-key': () => { saveKey(''); toast('Admin key removed from this browser'); render(); },
  'news-refresh': async () => { await refreshNews(true); render(); toast(S.newsError ? `Calendar: ${S.newsError}` : 'Calendar updated', !!S.newsError); },
  'traders-refresh': async () => { toast('Updating top traders…'); await refreshTraders(true); render(); },
  'news-impact': (el) => { S.newsImpacts[el.dataset.impact] = el.checked; renderNews(); },
  'news-currency': (el) => { S.newsCurrency = el.value; renderNews(); },
  'copy-mode': async (el) => {
    try {
      const r = await api('/api/paper/settings', 'POST', { copy_mode: el.value });
      toast(`Paper strategy: ${MODE_NAME[r.copy_mode]}.${r.applies_on_restart ? ' Stop and start paper trading to apply it.' : ''}`);
      await tick();
    } catch (e) { toast(e.message, true); }
  },
};

document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-action]');
  if (!el || el.tagName === 'INPUT' || el.tagName === 'SELECT') return;
  const fn = actions[el.dataset.action];
  if (fn) fn(el);
});
document.addEventListener('change', (e) => {
  const el = e.target.closest('input[data-action], select[data-action]');
  if (el && actions[el.dataset.action]) actions[el.dataset.action](el);
});
$('nav').addEventListener('click', (e) => { const a = e.target.closest('a[data-view]'); if (a) location.hash = a.dataset.view; });
window.addEventListener('hashchange', () => setView(location.hash.slice(1)));
$('refresh').addEventListener('click', () => actions.refresh());
$('bt-run').addEventListener('click', () => startJob('backtest', { ignore_kill_switch: $('bt-ignore-ks').checked }));
$('how-back').addEventListener('click', () => { S.howStep = Math.max(0, S.howStep - 1); renderHow(); });
$('how-next').addEventListener('click', () => { S.howStep += 1; renderHow(); });

// ------------------------------------------------------------------ polling
async function refreshSignal(force = false) {
  try { S.signal = await api('/api/signal' + (force ? '?refresh=true' : '')); S.signalError = S.signal.stale_error || null; }
  catch (e) { S.signalError = e.message; }
}
async function refreshBacktest() { try { S.backtest = await api('/api/backtest'); } catch (e) { S.backtest = { available: false }; } }
async function refreshNews(force = false) {
  try { S.news = await api('/api/news' + (force ? '?refresh=true' : '')); S.newsError = null; } catch (e) { S.newsError = e.message; }
}
async function refreshCopy() {
  try { S.copyAcct = await liveApi('/api/live/copy'); S.copyError = null; S.live = true; }
  catch (e) { S.copyError = e.message; }
}
async function refreshTraders(force = false) {
  if (S.tradersLoading) return;
  S.tradersLoading = true;
  try {
    if (SNAPSHOT && S.live) {
      try { S.traders = await liveApi('/api/live/traders'); } catch { S.traders = await api('/api/traders'); }
    } else S.traders = await api('/api/traders' + (force ? '?refresh=true' : ''));
    S.tradersError = S.traders.error || null;
  }
  catch (e) { S.tradersError = e.message; } finally { S.tradersLoading = false; }
}
async function refreshPaper() { try { S.paper = await api('/api/paper'); } catch { /* keep last */ } }

let ticking = false;
async function tick() {
  if (ticking) return;
  ticking = true;
  try {
    S.overview = await api('/api/overview');
    const job = S.overview.job;
    const status = job ? `${job.kind}:${job.status}:${job.finished}` : null;
    if (S.lastJobStatus && status !== S.lastJobStatus && job && job.status !== 'running') {
      if (job.status === 'done') toast(`${jobName(job.kind)}: done`); else toast(`${jobName(job.kind)} failed: ${job.error}`, true);
      if (job.kind === 'backtest') await refreshBacktest();
      if (job.kind === 'train' || job.kind === 'fetch') await refreshSignal(true);
    }
    S.lastJobStatus = status;
    if (['dashboard', 'paper'].includes(S.view)) await refreshPaper();
    if (S.view === 'paper') await pollLogs();
    render();
  } catch (e) {
    $('paper-pill').className = 'badge bad';
    $('paper-pill').textContent = 'Dashboard offline';
  } finally { ticking = false; }
}

function loop() {
  const running = S.overview?.job?.status === 'running';
  setTimeout(async () => { await tick(); loop(); }, running ? 1500 : 5000);
}

(async function init() {
  if (SNAPSHOT) {
    document.body.classList.add('static');
    setView(location.hash.slice(1) || 'dashboard');
    await Promise.all([tick(), refreshSignal(), refreshPaper(), refreshBacktest(), refreshNews(), refreshCopy()]);
    await refreshTraders();  // live leader positions when the Netlify functions are there
    render();
    setInterval(async () => {
      if (S.live) { await refreshCopy(); if (['dashboard', 'traders'].includes(S.view)) await refreshTraders(); }
      render();
    }, 30000);
    return;
  }
  setView(location.hash.slice(1) || 'dashboard');
  await tick();
  if (S.overview?.model?.available) await refreshSignal();
  await refreshPaper();
  render();
  loop();
  setInterval(async () => { if (S.overview?.model?.available) { await refreshSignal(); render(); } }, 60000);
  refreshTraders().then(render);
  setInterval(async () => { if (['dashboard', 'traders'].includes(S.view)) { await refreshTraders(); render(); } }, 60000);
  setInterval(async () => { if (S.view === 'news') { await refreshNews(); render(); } }, 300000);
})();
