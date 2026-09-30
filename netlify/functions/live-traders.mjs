// GET /api/live/traders -> the leaders' live positions and bias (cached for a minute)

import { getStore } from "@netlify/blobs";

import { liveConsensus } from "../lib/run.mjs";
import { json, loadSettings, siteUrl } from "../lib/settings.mjs";

export default async (req, context) => {
  const store = getStore("vtrade");
  const base = siteUrl(context, req);
  const settings = await loadSettings(base);
  try {
    const result = await liveConsensus(store, settings, base);
    return json({ enabled: true, live: true, ...result, settings: settings.copy });
  } catch (e) {
    return json({ detail: `Top traders unavailable: ${e.message || e}` }, 503);
  }
};

export const config = { path: "/api/live/traders" };
