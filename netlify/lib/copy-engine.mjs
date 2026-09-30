// The online copy account: a paper account that follows the top traders.
// Same rules as the local app in follow mode (vtrade/strategy.py decide_follow + vtrade/risk.py):
// long while the leaders' size-weighted bias is at or above the entry level, flat when it drops to the
// exit level, ATR stop and target, fixed-fractional sizing, daily loss limit and drawdown kill switch.
// Spot only: it never shorts and never uses leverage. No real money is involved.

const round2 = (v) => Math.round(v * 100) / 100;
const COOLDOWN_MS = 60 * 60_000; // after a sale, wait an hour before buying again (the local app decides hourly)
const signed = (b) => (b > 0 ? "+" : "") + b.toFixed(2);

export function newAccount(settings) {
  const start = settings.risk.starting_equity;
  return {
    running: false, created_at: new Date().toISOString(), started_at: null, last_run: null,
    last_price: null, last_bias: null, last_decision: "not started",
    cash: start, position: null, realized_pnl: 0,
    peak_equity: start, day: "", day_start_equity: start, halted: false, halt_reason: "",
    trades: [], equity: [], events: [],
  };
}

export function equityOf(account, price) {
  return account.cash + (account.position ? account.position.qty * price : 0);
}

/**
 * Run one check. Pure: returns a new account object.
 * consensus: result of hyperliquid.consensus() (or null when it failed)
 * blackoutEvent: the news event blocking entries right now, or null
 */
export function step(account, { settings, price, atr, consensus, blackoutEvent = null, now = Date.now() }) {
  const a = structuredClone(account);
  const { risk, costs, copy } = settings;
  const iso = new Date(now).toISOString();
  let traded = false;

  const event = (kind, detail) => {
    const last = a.events[a.events.length - 1];
    if (last && last.kind === kind && last.detail === detail) { last.ts = iso; return; } // don't repeat every 5 min
    a.events.push({ ts: iso, kind, detail });
    a.events = a.events.slice(-100);
  };
  const equity = () => equityOf(a, price);

  const sell = (reason) => {
    const p = a.position;
    const fill = price * (1 - costs.slippage);
    const proceeds = p.qty * fill;
    const fee = proceeds * costs.fee_rate;
    const pnl = proceeds - fee - (p.qty * p.entry_price + p.entry_fee);
    a.cash += proceeds - fee;
    a.realized_pnl += pnl;
    a.trades.push({ ts: iso, side: "sell", qty: p.qty, price: fill, fee, reason, pnl });
    a.position = null;
    a.cooldown_until = new Date(now + COOLDOWN_MS).toISOString();
    traded = true;
  };

  const buy = (reason) => {
    if (!(atr > 0)) return event("blocked", "no ATR yet, can't size the trade");
    const eq = equity();
    const stopDistance = risk.stop_atr_mult * atr;
    const perUnit = price * (1 + costs.fee_rate + costs.slippage);
    const qty = Math.min(eq * risk.risk_per_trade / stopDistance, eq * risk.max_position_pct / price, a.cash / perUnit);
    if (!(qty > 0) || qty * price < risk.min_notional) return event("blocked", "position size below the minimum");
    const fill = price * (1 + costs.slippage);
    const fee = qty * fill * costs.fee_rate;
    a.cash -= qty * fill + fee;
    a.position = {
      qty, entry_price: fill, entry_time: iso, entry_fee: fee,
      stop: fill - stopDistance, take_profit: fill + risk.take_profit_atr_mult * atr,
    };
    a.trades.push({ ts: iso, side: "buy", qty, price: fill, fee, reason, pnl: null });
    traded = true;
  };

  a.last_run = iso;
  a.last_price = price;
  const known = consensus && !consensus.error;
  const bias = known ? consensus.bias : undefined; // null = leaders hold no position, undefined = unknown
  a.last_bias = known ? bias : null;

  // Account-level risk: new UTC day, peak, drawdown kill switch.
  const day = iso.slice(0, 10);
  if (day !== a.day) { a.day = day; a.day_start_equity = equity(); }
  a.peak_equity = Math.max(a.peak_equity, equity());
  const drawdown = a.peak_equity > 0 ? 1 - equity() / a.peak_equity : 0;
  if (!a.halted && drawdown >= risk.max_drawdown) {
    a.halted = true;
    a.halt_reason = `max drawdown ${(drawdown * 100).toFixed(1)}% at ${iso}`;
    event("halted", a.halt_reason);
  }

  // Stop-loss and take-profit first.
  if (a.position) {
    if (price <= a.position.stop) sell("stop_loss");
    else if (price >= a.position.take_profit) sell("take_profit");
  }

  if (a.halted) {
    if (a.position) sell("kill_switch");
    a.last_decision = `halted: ${a.halt_reason}`;
  } else if (bias === undefined) {
    a.last_decision = "waiting: top-trader data unavailable";
    event("error", consensus?.error || "top-trader data unavailable");
  } else if (a.position) {
    if (bias === null ? copy.follow_exit_bias >= 0 : bias <= copy.follow_exit_bias) {
      sell(bias === null ? "leaders closed their positions" : `leaders' bias fell to ${signed(bias)}`);
      a.last_decision = "sold: leaders stopped being net long";
    } else {
      a.last_decision = `holding: leaders net long ${bias === null ? "" : signed(bias)}`.trim();
    }
  } else if (bias !== null && bias >= copy.follow_entry_bias) {
    if (a.cooldown_until && now < Date.parse(a.cooldown_until)) {
      a.last_decision = `waiting: cooldown after selling, until ${a.cooldown_until.slice(11, 16)} UTC`;
    } else if (blackoutEvent) {
      event("blocked", `news blackout: ${blackoutEvent.currency} ${blackoutEvent.title} at ${blackoutEvent.time}`);
      a.last_decision = "waiting: news blackout";
    } else if (a.day_start_equity > 0 && equity() <= a.day_start_equity * (1 - risk.daily_loss_limit)) {
      event("blocked", "daily loss limit reached");
      a.last_decision = "waiting: daily loss limit";
    } else {
      buy(`copying leaders, net long ${signed(bias)}`);
      a.last_decision = a.position ? `bought: leaders net long ${signed(bias)}` : "wanted to buy but couldn't size the trade";
    }
  } else {
    a.last_decision = bias === null
      ? "flat: leaders hold no position"
      : `flat: leaders' bias ${signed(bias)} below ${signed(copy.follow_entry_bias)}`;
  }

  // One equity point per clock hour (updated until the hour ends), plus one at every trade.
  const point = { t: iso, equity: round2(equity()), price, bias: known ? bias : null };
  const last = a.equity[a.equity.length - 1];
  if (!traded && last && !last.trade && last.t.slice(0, 13) === iso.slice(0, 13)) {
    a.equity[a.equity.length - 1] = point;
  } else {
    a.equity.push(traded ? { ...point, trade: true } : point);
  }
  a.equity = a.equity.slice(-2000);
  a.trades = a.trades.slice(-500);
  return a;
}
