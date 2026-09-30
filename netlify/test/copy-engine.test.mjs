import assert from "node:assert/strict";
import { test } from "node:test";

import { newAccount, step, equityOf } from "../lib/copy-engine.mjs";
import { atr, biasOf, pickLeaders, positionIn } from "../lib/hyperliquid.mjs";
import { blackout } from "../lib/news.mjs";
import { DEFAULTS } from "../lib/settings.mjs";

const settings = structuredClone(DEFAULTS);
const T0 = Date.parse("2026-10-01T10:00:00Z");
const long = (bias) => ({ bias, error: null });
const running = () => ({ ...newAccount(settings), running: true });

test("buys when leaders are net long, sized to risk 1% at the stop", () => {
  const a = step(running(), { settings, price: 80_000, atr: 400, consensus: long(0.5), now: T0 });
  assert.ok(a.position);
  const p = a.position;
  assert.equal(p.stop, p.entry_price - 800);
  assert.equal(p.take_profit, p.entry_price + 1200);
  // risk 1% of 10k = $100 over an $800 stop -> 0.125 BTC ($10k), capped at 50% of equity -> $5k
  assert.ok(Math.abs(p.qty * 80_000 - 5_000) < 1);
  assert.equal(a.trades.length, 1);
  assert.match(a.trades[0].reason, /copying leaders/);
});

test("does nothing below the entry bias, when data is missing, or when stopped", () => {
  assert.equal(step(running(), { settings, price: 80_000, atr: 400, consensus: long(0.1), now: T0 }).position, null);
  assert.equal(step(running(), { settings, price: 80_000, atr: 400, consensus: long(null), now: T0 }).position, null);
  const broken = step(running(), { settings, price: 80_000, atr: 400, consensus: { error: "down" }, now: T0 });
  assert.equal(broken.position, null);
  assert.equal(broken.events.at(-1).kind, "error");
});

test("sells when the leaders' bias drops, or when they all close", () => {
  let a = step(running(), { settings, price: 80_000, atr: 400, consensus: long(0.5), now: T0 });
  a = step(a, { settings, price: 80_100, atr: 400, consensus: long(-0.2), now: T0 + 300_000 });
  assert.equal(a.position, null);
  assert.equal(a.trades.at(-1).side, "sell");

  let b = step(running(), { settings, price: 80_000, atr: 400, consensus: long(0.5), now: T0 });
  b = step(b, { settings, price: 80_000, atr: 400, consensus: long(null), now: T0 + 300_000 });
  assert.equal(b.trades.at(-1).reason, "leaders closed their positions");
});

test("stop-loss fills and P&L includes fees and slippage", () => {
  let a = step(running(), { settings, price: 80_000, atr: 400, consensus: long(0.5), now: T0 });
  const { qty, entry_price, entry_fee, stop } = a.position;
  a = step(a, { settings, price: stop - 10, atr: 400, consensus: long(0.5), now: T0 + 300_000 });
  const sale = a.trades.at(-1);
  assert.equal(sale.reason, "stop_loss");
  const fill = (stop - 10) * (1 - settings.costs.slippage);
  const expected = qty * fill * (1 - settings.costs.fee_rate) - (qty * entry_price + entry_fee);
  assert.ok(Math.abs(sale.pnl - expected) < 1e-6);
  assert.ok(Math.abs(equityOf(a, stop) - (10_000 + expected)) < 1e-6);
});

test("waits an hour after selling before buying again", () => {
  let a = step(running(), { settings, price: 80_000, atr: 400, consensus: long(0.5), now: T0 });
  a = step(a, { settings, price: a.position.stop - 1, atr: 400, consensus: long(0.5), now: T0 + 300_000 });
  assert.equal(a.position, null);
  a = step(a, { settings, price: 79_000, atr: 400, consensus: long(0.5), now: T0 + 30 * 60_000 });
  assert.equal(a.position, null);
  assert.match(a.last_decision, /cooldown/);
  a = step(a, { settings, price: 79_000, atr: 400, consensus: long(0.5), now: T0 + 70 * 60_000 });
  assert.ok(a.position);
});

test("news blackout and the kill switch block entries", () => {
  const event = { title: "CPI m/m", currency: "USD", time: "2026-10-01T10:15:00Z" };
  const blocked = step(running(), { settings, price: 80_000, atr: 400, consensus: long(0.9), blackoutEvent: event, now: T0 });
  assert.equal(blocked.position, null);
  assert.match(blocked.events.at(-1).detail, /news blackout/);

  const halted = { ...running(), peak_equity: 20_000 }; // 50% below peak
  const h = step(halted, { settings, price: 80_000, atr: 400, consensus: long(0.9), now: T0 });
  assert.equal(h.halted, true);
  assert.equal(h.position, null);
});

test("keeps one equity point per hour plus trade points", () => {
  let a = running();
  for (let i = 0; i < 24; i++) a = step(a, { settings, price: 80_000, atr: 400, consensus: long(0.1), now: T0 + i * 300_000 });
  assert.equal(a.equity.length, 2); // 10:00-10:55 and 11:00-11:55
  a = step(a, { settings, price: 80_000, atr: 400, consensus: long(0.8), now: T0 + 24 * 300_000 });
  assert.equal(a.equity.at(-1).trade, true);
});

test("hyperliquid helpers", () => {
  const row = (addr, acct, pnl, vlm = 1) => ({
    ethAddress: addr, accountValue: String(acct),
    windowPerformances: [["month", { pnl: String(pnl), roi: "0", vlm: String(vlm) }]],
  });
  const picked = pickLeaders([row("0xA", 5e6, 1e6), row("0xB", 2e6, 9e6), row("0xV", 9e8, 5e7, 0), row("0xS", 1e4, 2e7)],
    { rank_by: "month", top_n: 10, min_account_value: 1e6 });
  assert.deepEqual(picked.map((l) => l.address), ["0xb", "0xa"]);

  const state = { marginSummary: { accountValue: "100" }, assetPositions: [
    { position: { coin: "BTC", szi: "-2", positionValue: "160000", entryPx: "80000", leverage: { value: 20 } } }] };
  const p = positionIn(state, "BTC");
  assert.equal(p.side, "short");
  assert.equal(biasOf([p, { side: "long", notional: 480000 }]), 0.5);
  assert.equal(biasOf([{ side: "flat", notional: 0 }]), null);

  const flat = Array.from({ length: 30 }, () => ({ h: 101, l: 99, c: 100 }));
  assert.ok(Math.abs(atr(flat) - 2) < 1e-9);
  assert.equal(atr(flat.slice(0, 5)), null);
});

test("news blackout window", () => {
  const events = [{ title: "Non-Farm Employment Change", country: "USD", date: "2026-10-02T08:30:00-04:00", impact: "High" }];
  const nfp = Date.parse("2026-10-02T12:30:00Z");
  assert.ok(blackout(events, settings.news, nfp - 29 * 60_000));
  assert.equal(blackout(events, settings.news, nfp - 31 * 60_000), null);
  assert.equal(blackout(events, { ...settings.news, enabled: false }, nfp), null);
});
