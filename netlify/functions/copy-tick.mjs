// Every 5 minutes: one check of the online copy account (does nothing while it's stopped).

import { getStore } from "@netlify/blobs";

import { runCopy } from "../lib/run.mjs";
import { loadSettings, siteUrl } from "../lib/settings.mjs";

export default async (req, context) => {
  const store = getStore("vtrade");
  const base = siteUrl(context, req);
  const settings = await loadSettings(base);
  try {
    const account = await runCopy(store, settings, base);
    console.log(`copy check: running=${account.running} decision="${account.last_decision}"`);
  } catch (e) {
    console.error("copy check failed:", e);
  }
};

export const config = { schedule: "*/5 * * * *" };
