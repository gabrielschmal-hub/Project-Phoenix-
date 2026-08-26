
// Phoenix screener scan — intraday breakout watch.
// Staggered 2 minutes after the quotes lane. Quotes the ~60 gate-passers
// sitting nearest their published resistance level; when live price crosses
// that level, a row lands in signals_intraday — once per ticker per day.
// The triggers come from the daily engine's own stocks.json. This lane never
// invents levels; it only checks the engine's levels against live prices.

import { createClient } from "jsr:@supabase/supabase-js@2";

const SB = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);

function etNow() {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date());
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const dows = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  return {
    dow: dows.indexOf(get("weekday")),
    minutes: parseInt(get("hour"), 10) * 60 + parseInt(get("minute"), 10),
    stamp: `${get("weekday")} ${get("hour")}:${get("minute")} ET`,
  };
}

async function beat(ok: boolean, note: string) {
  await SB.from("heartbeats").upsert({
    lane: "screener", ts: new Date().toISOString(), ok, note,
  });
}

Deno.serve(async (req: Request) => {
  const out = (b: unknown, s = 200) =>
    new Response(JSON.stringify(b), { status: s,
      headers: { "Content-Type": "application/json" } });

  let force = false;
  try { force = (await req.json())?.force === true; } catch { /* empty */ }

  const { data: st } = await SB.from("system_state")
    .select("mode").eq("id", 1).maybeSingle();
  if (st?.mode === "frozen") return out({ skip: "frozen" });

  const { dow, minutes, stamp } = etNow();
  const open = dow >= 1 && dow <= 5 && minutes >= 570 && minutes < 960;
  if (!open && !force) return out({ skip: "market closed", et: stamp });

  if (!force) {
    const { data: hb } = await SB.from("heartbeats")
      .select("ts").eq("lane", "screener").maybeSingle();
    if (hb && Date.now() - new Date(hb.ts).getTime() < 3 * 60 * 1000) {
      return out({ skip: "debounce", last: hb.ts });
    }
  }

  const key = Deno.env.get("FINNHUB_API_KEY");
  if (!key) { await beat(false, "FINNHUB_API_KEY not set");
    return out({ error: "FINNHUB_API_KEY not set" }, 500); }

  const [{ data: watch }, { data: trig }] = await Promise.all([
    SB.from("watch_tickers").select("ticker")
      .eq("active", true).eq("bucket", "screener"),
    SB.from("screener_triggers").select("ticker,trigger"),
  ]);
  if (!watch?.length) { await beat(true, "no watchers");
    return out({ ok: 0, note: "no watchers" }); }

  const T: Record<string, number> = {};
  (trig ?? []).forEach((t) => { T[t.ticker] = Number(t.trigger); });

  const rows: Record<string, unknown>[] = [];
  const failed: string[] = [];
  const tickers = watch.map((w) => w.ticker);

  for (let i = 0; i < tickers.length; i += 8) {
    const batch = tickers.slice(i, i + 8);
    const settled = await Promise.allSettled(batch.map(async (tk) => {
      const sym = tk.replace(/-/g, ".");
      const r = await fetch(
        `https://finnhub.io/api/v1/quote?symbol=${encodeURIComponent(sym)}&token=${key}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const q = await r.json();
      if (!q || typeof q.c !== "number" || q.c === 0) throw new Error("empty quote");
      return { ticker: tk, last: q.c, prev_close: q.pc ?? null,
        source: "finnhub",
        quote_ts: q.t ? new Date(q.t * 1000).toISOString() : null,
        updated_at: new Date().toISOString() };
    }));
    settled.forEach((s, j) => {
      if (s.status === "fulfilled") rows.push(s.value);
      else failed.push(`${batch[j]}`);
    });
    if (i + 8 < tickers.length) await new Promise((r) => setTimeout(r, 9000));
  }

  let crossed = 0;
  if (rows.length) {
    await SB.from("prices_live").upsert(rows);
    await SB.from("prices_intraday").insert(rows.map((r) => ({
      ticker: r.ticker, ts: r.updated_at, last: r.last })));

    // breakout check — once per ticker per day
    const today = new Date(); today.setUTCHours(0, 0, 0, 0);
    const { data: already } = await SB.from("signals_intraday")
      .select("ticker").gte("ts", today.toISOString());
    const seen = new Set((already ?? []).map((a) => a.ticker));
    const hits = rows.filter((r) =>
      T[r.ticker as string] != null &&
      (r.last as number) >= T[r.ticker as string] &&
      !seen.has(r.ticker as string));
    if (hits.length) {
      await SB.from("signals_intraday").insert(hits.map((h) => ({
        ticker: h.ticker, kind: "breakout", price: h.last,
        trigger: T[h.ticker as string],
        note: "live cross of daily resistance" })));
      crossed = hits.length;
    }
  }

  await beat(failed.length === 0,
    `${rows.length}/${tickers.length} ok · ${crossed} crossed` +
    (failed.length ? ` · failed: ${failed.slice(0, 4).join(",")}` : ""));
  return out({ ok: rows.length, crossed, failed: failed.length, et: stamp });
});
