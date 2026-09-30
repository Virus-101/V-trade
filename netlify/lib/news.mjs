// ForexFactory's weekly calendar export, cached for an hour (the export allows 2 downloads per 5 min).

const FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json";

export async function calendar(store) {
  const cached = await store.get("ff-calendar", { type: "json" });
  if (cached && Date.now() - Date.parse(cached.fetched_at) < 60 * 60_000) return cached.events;
  try {
    const res = await fetch(FF_URL, {
      headers: { "User-Agent": "V-trade/0.1 (+https://github.com/Virus-101/V-trade)" },
      signal: AbortSignal.timeout(8000),
    });
    const text = await res.text();
    if (text.trimStart().startsWith("<")) throw new Error("ForexFactory rate limit");
    const events = JSON.parse(text);
    await store.setJSON("ff-calendar", { fetched_at: new Date().toISOString(), events });
    return events;
  } catch {
    return cached?.events || [];
  }
}

export function blackout(events, settings, now = Date.now()) {
  if (!settings.enabled) return null;
  const before = settings.block_before_minutes * 60_000;
  const after = settings.block_after_minutes * 60_000;
  for (const e of events) {
    const t = Date.parse(e.date);
    if (settings.currencies.includes(String(e.country).toUpperCase()) && settings.impacts.includes(e.impact)
        && t - before <= now && now <= t + after) {
      return { title: e.title, currency: e.country, time: new Date(t).toISOString(), impact: e.impact };
    }
  }
  return null;
}
