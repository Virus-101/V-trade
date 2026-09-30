// Shared by the scheduled check, the "check now" button and the live traders endpoint.

import { timingSafeEqual, createHash } from "node:crypto";

import { atr, candles, consensus, fetchLeaderboard, leaderFromRow, midPrice, pickLeaders } from "./hyperliquid.mjs";
import { blackout, calendar } from "./news.mjs";
import { newAccount, step } from "./copy-engine.mjs";

export const ACCOUNT_KEY = "copy-account";

export async function loadAccount(store, settings) {
  return (await store.get(ACCOUNT_KEY, { type: "json" })) || newAccount(settings);
}

// Leaders: the wallets in config.yaml, else the cached leaderboard pick, else the list published
// with the snapshot (api/traders.json).
export async function getLeaders(store, settings, base) {
  const cached = await store.get("leaders", { type: "json" });
  if (settings.copy.leaders.length) {
    const known = Object.fromEntries((cached?.manual || []).map((l) => [l.address, l]));
    return { leaders: settings.copy.leaders.map((a) => known[a.toLowerCase()] || { address: a.toLowerCase() }), source: "manual", cached };
  }
  if (cached?.auto?.length) return { leaders: cached.auto, source: `top ${settings.copy.top_n} by ${settings.copy.rank_by} profit`, cached };
  try {
    const snap = await (await fetch(`${base}/api/traders.json`, { signal: AbortSignal.timeout(5000) })).json();
    const leaders = (snap.traders || []).map((t) => ({
      address: t.address, name: t.name, pnl: t.pnl, roi: t.roi, account_value: t.leaderboard_account_value,
    }));
    return { leaders, source: snap.source || "published list", cached: { fetched_at: snap.leaderboard_fetched_at, accounts_ranked: snap.accounts_ranked } };
  } catch {
    return { leaders: [], source: "none", cached: null };
  }
}

export async function refreshLeaders(store, settings) {
  const rows = await fetchLeaderboard(25_000);
  const wanted = new Set(settings.copy.leaders.map((a) => a.toLowerCase()));
  const data = {
    fetched_at: new Date().toISOString(),
    accounts_ranked: rows.length,
    auto: pickLeaders(rows, settings.copy),
    manual: rows.filter((r) => wanted.has(String(r.ethAddress).toLowerCase())).map(leaderFromRow),
  };
  await store.setJSON("leaders", data);
  return data;
}

export async function liveConsensus(store, settings, base, maxAgeMs = 60_000) {
  const cached = await store.get("consensus", { type: "json" });
  if (cached && Date.now() - Date.parse(cached.updated_at) < maxAgeMs) return cached;
  const { leaders, source, cached: meta } = await getLeaders(store, settings, base);
  const result = await consensus(leaders, settings.copy.coin, {
    source,
    leaderboard_fetched_at: meta?.fetched_at || null,
    accounts_ranked: meta?.accounts_ranked || null,
  });
  await store.setJSON("consensus", result);
  return result;
}

// One check of the copy account. Skipped while stopped unless forced.
export async function runCopy(store, settings, base, { force = false } = {}) {
  const account = await loadAccount(store, settings);
  if (!account.running && !force) return account;
  const coin = settings.copy.coin;
  const [price, rows, events, cons] = await Promise.all([
    midPrice(coin),
    candles(coin, "1h", 100).catch(() => []),
    calendar(store),
    liveConsensus(store, settings, base, 0).catch((e) => ({ error: String(e.message || e) })),
  ]);
  const next = step(account, {
    settings,
    price,
    atr: atr(rows),
    consensus: cons,
    blackoutEvent: blackout(events, settings.news),
  });
  await store.setJSON(ACCOUNT_KEY, next);
  return next;
}

export function authorized(req) {
  const expected = process.env.VTRADE_ADMIN_KEY || "";
  const given = req.headers.get("x-admin-key") || "";
  if (!expected || !given) return false;
  const digest = (s) => createHash("sha256").update(s).digest();
  return timingSafeEqual(digest(expected), digest(given));
}
