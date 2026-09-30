// Every 6 hours: re-rank the Hyperliquid leaderboard and store the leaders to follow.

import { getStore } from "@netlify/blobs";

import { refreshLeaders } from "../lib/run.mjs";
import { loadSettings, siteUrl } from "../lib/settings.mjs";

export default async (req, context) => {
  const store = getStore("vtrade");
  const settings = await loadSettings(siteUrl(context, req));
  try {
    const data = await refreshLeaders(store, settings);
    console.log(`leaders refreshed from ${data.accounts_ranked} accounts`);
  } catch (e) {
    // Keep the previous list (or the one published with the snapshot).
    console.error("leaderboard refresh failed:", e);
  }
};

export const config = { schedule: "17 */6 * * *" };
