// Settings for the online copy account. They come from config.yaml via the published snapshot
// (api/overview.json), so `run.bat publish` keeps Netlify in sync with the local app.

export const DEFAULTS = {
  copy: {
    coin: "BTC", leaders: [], top_n: 10, rank_by: "month", min_account_value: 1_000_000,
    follow_entry_bias: 0.3, follow_exit_bias: 0.0, leaderboard_refresh_hours: 6,
  },
  risk: {
    starting_equity: 10_000, risk_per_trade: 0.01, max_position_pct: 0.5, stop_atr_mult: 2.0,
    take_profit_atr_mult: 3.0, daily_loss_limit: 0.03, max_drawdown: 0.2, min_notional: 10,
  },
  costs: { fee_rate: 0.001, slippage: 0.0005 },
  news: { enabled: true, currencies: ["USD"], impacts: ["High"], block_before_minutes: 30, block_after_minutes: 30 },
};

export function siteUrl(context, req) {
  return context?.site?.url || process.env.URL || (req ? new URL(req.url).origin : "");
}

export async function loadSettings(base) {
  const merged = structuredClone(DEFAULTS);
  try {
    const res = await fetch(`${base}/api/overview.json`, { signal: AbortSignal.timeout(5000) });
    const config = (await res.json()).config || {};
    for (const section of Object.keys(merged)) Object.assign(merged[section], config[section] || {});
  } catch {
    // keep the defaults
  }
  return merged;
}

export function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", "X-Robots-Tag": "noindex" },
  });
}
