// GET  /api/live/copy  -> the online copy account (public, read-only)
// POST /api/live/copy  -> {action: "start" | "stop" | "check" | "reset"}, needs the X-Admin-Key header

import { getStore } from "@netlify/blobs";

import { equityOf, newAccount } from "../lib/copy-engine.mjs";
import { ACCOUNT_KEY, authorized, loadAccount, runCopy } from "../lib/run.mjs";
import { json, loadSettings, siteUrl } from "../lib/settings.mjs";

function view(account, settings) {
  const price = account.last_price;
  const equity = price ? equityOf(account, price) : account.cash;
  const p = account.position;
  return {
    live: true,
    running: account.running,
    started_at: account.started_at,
    last_run: account.last_run,
    last_price: price,
    last_bias: account.last_bias,
    last_decision: account.last_decision,
    starting_equity: settings.risk.starting_equity,
    cash: account.cash,
    equity,
    realized_pnl: account.realized_pnl,
    position: p ? { ...p, unrealized_pnl: price ? (price - p.entry_price) * p.qty - p.entry_fee : 0 } : null,
    halted: account.halted,
    halt_reason: account.halt_reason,
    closed_trades: account.trades.filter((t) => t.side === "sell").length,
    wins: account.trades.filter((t) => t.side === "sell" && t.pnl > 0).length,
    trades: account.trades.slice(-100).reverse(),
    equity_history: account.equity,
    events: account.events.slice(-30).reverse(),
    rules: {
      coin: settings.copy.coin,
      entry_bias: settings.copy.follow_entry_bias,
      exit_bias: settings.copy.follow_exit_bias,
      risk_per_trade: settings.risk.risk_per_trade,
      check_every_minutes: 5,
    },
    admin_key_configured: Boolean(process.env.VTRADE_ADMIN_KEY),
  };
}

export default async (req, context) => {
  const store = getStore("vtrade");
  const base = siteUrl(context, req);
  const settings = await loadSettings(base);
  let account = await loadAccount(store, settings);

  if (req.method === "GET") return json(view(account, settings));
  if (req.method !== "POST") return json({ detail: "Method not allowed" }, 405);
  if (!authorized(req)) return json({ detail: "Wrong or missing admin key" }, 401);

  const { action } = await req.json().catch(() => ({}));
  const now = new Date().toISOString();
  if (action === "start") {
    if (!account.running) {
      account.running = true;
      account.started_at = now;
      account.events.push({ ts: now, kind: "started", detail: "copy account started" });
      await store.setJSON(ACCOUNT_KEY, account);
      account = await runCopy(store, settings, base, { force: true }); // first check right away
    }
  } else if (action === "stop") {
    account.running = false;
    account.last_decision = "stopped";
    account.events.push({ ts: now, kind: "stopped", detail: "copy account stopped" });
    await store.setJSON(ACCOUNT_KEY, account);
  } else if (action === "check") {
    account = await runCopy(store, settings, base, { force: true });
  } else if (action === "reset") {
    if (account.running) return json({ detail: "Stop the copy account before resetting it." }, 409);
    account = newAccount(settings);
    await store.setJSON(ACCOUNT_KEY, account);
  } else {
    return json({ detail: "action must be start, stop, check or reset" }, 400);
  }
  return json(view(account, settings));
};

export const config = { path: "/api/live/copy" };
