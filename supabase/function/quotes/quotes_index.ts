// Phoenix fast lane — quotes
// Every 5 minutes during US regular session: pull the active watch list from
// Finnhub, upsert prices_live, stamp a heartbeat. Everything else exits early.
//
// Order of gates, cheapest first:
//   1. freeze switch        (system_state.mode)
//   2. market hours, DST-proof (clock read in America/New_York, not UTC math)
//   3. debounce             (heartbeat < 3 min ago -> skip; damps double-fires
//                            and quota-drain abuse)
// Body {"force": true} bypasses gates 2 and 3 for smoke tests. Never gate 1.

import { createClient } from "jsr:@supabase/supabase-js@2";

const SB = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);

function etNow(): { dow: number; minutes: number; stamp: string } {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }).formatToParts(new Date());
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  const dows = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  const dow = dows.indexOf(get("weekday"));
  const minutes = parseInt(get("hour"), 10) * 60 + parseInt(get("minute"), 10);
  return { dow, minutes, stamp: `${get("weekday")} ${get("hour")}:${get("minute")} ET` };
}

function marketOpen(): { open: boolean; stamp: string } {
  const { dow, minutes, stamp } = etNow();
  const open = dow >= 1 && dow <= 5 && minutes >= 9 * 60 + 30 && minutes < 16 * 60;
  return { open, stamp };
}

async function beat(ok: boolean, note: string) {
  await SB.from("heartbeats").upsert({
    lane: "fast", ts: new Date().toISOString(), ok, note,
  });
}

Deno.serve(async (req: Request) => {
  const out = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status, headers: { "Content-Type": "application/json" },
    });

  let force = false;
  try { force = (await req.json())?.force === true; } catch { /* empty body */ }

  // gate 1 — freeze. force does NOT bypass this one.
  const { data: st } = await SB.from("system_state")
    .select("mode,reason").eq("id", 1).maybeSingle();
  if (st?.mode === "frozen") {
    return out({ skip: "frozen", reason: st.reason ?? null });
  }

  // gate 2 — market hours
  const { open, stamp } = marketOpen();
  if (!open && !force) {
    return out({ skip: "market closed", et: stamp });
  }

  // gate 3 — debounce
  if (!force) {
    const { data: hb } = await SB.from("heartbeats")
      .select("ts").eq("lane", "fast").maybeSingle();
    if (hb && Date.now() - new Date(hb.ts).getTime() < 3 * 60 * 1000) {
      return out({ skip: "debounce", last: hb.ts });
    }
  }

  const key = Deno.env.get("FINNHUB_API_KEY");
  if (!key) {
    await beat(false, "FINNHUB_API_KEY not set");
    return out({ error: "FINNHUB_API_KEY not set" }, 500);
  }

  const { data: watch, error: werr } = await SB.from("watch_tickers")
    .select("ticker,bucket").eq("active", true);
  if (werr || !watch?.length) {
    await beat(false, `watch list: ${werr?.message ?? "empty"}`);
    return out({ error: "watch list unavailable" }, 500);
  }

  const rows: Record<string, unknown>[] = [];
  const failed: string[] = [];

  // priority order: tape buckets first, so positions and macro are the
  // freshest rows even if a later batch fails or the clock runs out.
  const PRIO: Record<string, number> = {
    position: 0, macro: 1, index_proxy: 2, watch: 3, sector: 4, sp50: 5 };
  const tickers = watch
    .sort((a, b) => (PRIO[a.bucket] ?? 9) - (PRIO[b.bucket] ?? 9))
    .map((w) => w.ticker);

  // pacing: 70 tickers vs Finnhub's 60 calls/min. Batches of 8 every 9s
  // ≈ 53/min sustained — under the limit with margin. ~80s wall for the
  // full list, inside the Edge Function window.
  for (let i = 0; i < tickers.length; i += 8) {
    const batch = tickers.slice(i, i + 8);
    const settled = await Promise.allSettled(batch.map(async (tk) => {
      // Finnhub wants dots in share classes (BRK.B); our data may carry dashes.
      const sym = tk.replace(/-/g, ".");
      const r = await fetch(
        `https://finnhub.io/api/v1/quote?symbol=${encodeURIComponent(sym)}&token=${key}`,
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const q = await r.json();
      // Finnhub quote: c=last, pc=prev close, t=unix ts. c===0 means unknown symbol.
      if (!q || typeof q.c !== "number" || q.c === 0) throw new Error("empty quote");
      return {
        ticker: tk,
        last: q.c,
        prev_close: q.pc ?? null,
        source: "finnhub",
        quote_ts: q.t ? new Date(q.t * 1000).toISOString() : null,
        updated_at: new Date().toISOString(),
      };
    }));
    settled.forEach((s, j) => {
      if (s.status === "fulfilled") rows.push(s.value);
      else failed.push(`${batch[j]}: ${s.reason?.message ?? s.reason}`);
    });
    if (i + 8 < tickers.length) await new Promise((r) => setTimeout(r, 9000));
  }

  if (rows.length) {
    const { error: uerr } = await SB.from("prices_live").upsert(rows);
    if (uerr) {
      await beat(false, `upsert: ${uerr.message}`);
      return out({ error: uerr.message, fetched: rows.length }, 500);
    }
  }

  const note = `${rows.length}/${tickers.length} ok` +
    (failed.length ? ` · failed: ${failed.slice(0, 5).join(", ")}` : "");
  await beat(failed.length === 0, note);
  return out({ ok: rows.length, failed, et: stamp, forced: force });
});
