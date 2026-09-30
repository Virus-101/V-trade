// Public Hyperliquid data: leaderboard, account positions, prices and candles.
// Mirrors vtrade/copytrade.py so the online account behaves like the local one.

const INFO_URL = "https://api.hyperliquid.xyz/info";
const LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard";
const WINDOWS = ["day", "week", "month", "allTime"];

export async function info(payload, timeoutMs = 10_000) {
  const res = await fetch(INFO_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!res.ok) throw new Error(`Hyperliquid answered ${res.status}`);
  return res.json();
}

export async function fetchLeaderboard(timeoutMs = 25_000) {
  const res = await fetch(LEADERBOARD_URL, { signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`Leaderboard answered ${res.status}`);
  return (await res.json()).leaderboardRows || [];
}

export function leaderFromRow(row) {
  const windows = Object.fromEntries(row.windowPerformances || []);
  const pick = (field) => Object.fromEntries(WINDOWS.map((w) => [w, parseFloat(windows[w]?.[field] || 0)]));
  return {
    address: row.ethAddress.toLowerCase(),
    name: row.displayName || null,
    account_value: parseFloat(row.accountValue || 0),
    pnl: pick("pnl"),
    roi: pick("roi"),
  };
}

// Most profitable *active* accounts: traded this month (vaults and idle wallets have no volume).
export function pickLeaders(rows, { rank_by, top_n, min_account_value }) {
  return rows
    .filter((r) => {
      const month = Object.fromEntries(r.windowPerformances || []).month || {};
      return parseFloat(month.vlm || 0) > 0 && parseFloat(r.accountValue || 0) >= min_account_value;
    })
    .map(leaderFromRow)
    .sort((a, b) => (b.pnl[rank_by] || 0) - (a.pnl[rank_by] || 0))
    .slice(0, top_n);
}

export function positionIn(state, coin) {
  const positions = state.assetPositions || [];
  const out = {
    account_value: parseFloat(state.marginSummary?.accountValue || 0),
    open_positions: positions.length,
    side: "flat", size: 0, notional: 0, entry: null, leverage: null, unrealized_pnl: 0, liquidation: null,
  };
  for (const item of positions) {
    const p = item.position || {};
    if (p.coin !== coin) continue;
    const size = parseFloat(p.szi || 0);
    Object.assign(out, {
      side: size > 0 ? "long" : size < 0 ? "short" : "flat",
      size,
      notional: Math.abs(parseFloat(p.positionValue || 0)),
      entry: p.entryPx ? parseFloat(p.entryPx) : null,
      leverage: p.leverage?.value ?? null,
      unrealized_pnl: parseFloat(p.unrealizedPnl || 0),
      liquidation: p.liquidationPx ? parseFloat(p.liquidationPx) : null,
    });
  }
  return out;
}

// Size-weighted net direction: +1 all long, -1 all short, null if nobody holds the coin.
export function biasOf(rows) {
  const long = rows.filter((r) => r.side === "long").reduce((s, r) => s + r.notional, 0);
  const short = rows.filter((r) => r.side === "short").reduce((s, r) => s + r.notional, 0);
  return long + short === 0 ? null : (long - short) / (long + short);
}

export async function consensus(leaders, coin, meta = {}) {
  const results = await Promise.allSettled(
    leaders.map((l) => info({ type: "clearinghouseState", user: l.address })),
  );
  const traders = leaders.map((l, i) => {
    const row = { address: l.address, name: l.name || null, pnl: l.pnl || {}, roi: l.roi || {},
                  leaderboard_account_value: l.account_value || 0, error: null };
    const r = results[i];
    return r.status === "fulfilled"
      ? { ...row, ...positionIn(r.value, coin) }
      : { ...row, side: "unknown", notional: 0, error: String(r.reason?.message || r.reason) };
  });
  const ok = traders.filter((t) => !t.error);
  const sum = (side) => ok.filter((t) => t.side === side).reduce((s, t) => s + t.notional, 0);
  return {
    coin,
    bias: biasOf(ok),
    longs: ok.filter((t) => t.side === "long").length,
    shorts: ok.filter((t) => t.side === "short").length,
    flat: ok.filter((t) => t.side === "flat").length,
    failed: traders.length - ok.length,
    long_notional: sum("long"),
    short_notional: sum("short"),
    traders,
    updated_at: new Date().toISOString(),
    error: traders.length && !ok.length ? "Could not read any leader's positions" : (traders.length ? null : "No leaders to follow"),
    ...meta,
  };
}

export async function midPrice(coin) {
  const mids = await info({ type: "allMids" });
  const price = parseFloat(mids[coin]);
  if (!(price > 0)) throw new Error(`No price for ${coin}`);
  return price;
}

export async function candles(coin, interval = "1h", count = 100) {
  const end = Date.now();
  const rows = await info({ type: "candleSnapshot", req: { coin, interval, startTime: end - count * 3_600_000, endTime: end } });
  return rows.map((c) => ({ t: c.t, o: +c.o, h: +c.h, l: +c.l, c: +c.c }));
}

// Wilder's ATR, same recursion as pandas ewm(alpha=1/period, adjust=False) in vtrade/indicators.py.
export function atr(rows, period = 14) {
  if (rows.length < period + 1) return null;
  let value = null;
  rows.forEach((r, i) => {
    const prev = i > 0 ? rows[i - 1].c : null;
    const tr = prev === null ? r.h - r.l : Math.max(r.h - r.l, Math.abs(r.h - prev), Math.abs(r.l - prev));
    value = value === null ? tr : value + (tr - value) / period;
  });
  return value;
}
