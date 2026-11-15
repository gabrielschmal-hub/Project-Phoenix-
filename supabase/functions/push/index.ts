// Phoenix web push — delivers push_outbox rows to every phone/computer that turned
// notifications on in the Phoenix app (push_subs).
//
// Who calls it: only the database. An insert into push_outbox fires a trigger that
// posts {"id": n} here with the vault JWT (same pattern as the quotes lane). The MME
// test engine and the app's "Send test" button both just insert a row.
// A caller can only make it deliver rows that are already queued, never write its own.
//
// Keys: the VAPID pair lives in push_config (no read policy: service role only).
// Dead subscriptions (404/410 from the push service) are switched off; other failures
// count up and switch off after 5 in a row.

import { createClient } from "jsr:@supabase/supabase-js@2";
import webpush from "npm:web-push@3.6.7";

const SB = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);

const json = (o: unknown, status = 200) =>
  new Response(JSON.stringify(o), { status, headers: { "Content-Type": "application/json" } });

Deno.serve(async (req) => {
  let body: { id?: number } = {};
  try { body = await req.json(); } catch { /* empty body: deliver everything pending */ }

  const { data: cfg, error: cfgErr } = await SB.from("push_config").select("k,v");
  if (cfgErr) return json({ ok: false, error: cfgErr.message }, 500);
  const C = Object.fromEntries((cfg ?? []).map((r) => [r.k, r.v]));
  webpush.setVapidDetails(C.vapid_subject, C.vapid_public, C.vapid_private);

  let q = SB.from("push_outbox").select("*").eq("status", "pending").order("id").limit(20);
  if (body.id) q = q.eq("id", body.id);
  const { data: msgs } = await q;
  if (!msgs?.length) return json({ ok: true, sent: 0, note: "nothing pending" });

  const { data: subs } = await SB.from("push_subs").select("*").eq("active", true);
  const out = [];
  for (const m of msgs) {
    // claim the row so a double-fire cannot send twice
    const { data: claimed } = await SB.from("push_outbox").update({ status: "sending" })
      .eq("id", m.id).eq("status", "pending").select("id");
    if (!claimed?.length) continue;
    const payload = JSON.stringify({ title: m.title, body: m.body, url: m.url, tag: m.tag || ("px-" + m.id) });
    let sent = 0, failed = 0;
    const notes: string[] = [];
    for (const s of subs ?? []) {
      try {
        await webpush.sendNotification(s.sub, payload, { TTL: 6 * 3600, urgency: "high" });
        sent++;
        await SB.from("push_subs").update({ fails: 0, last_ok: new Date().toISOString() }).eq("id", s.id);
      } catch (e) {
        failed++;
        const code = (e as { statusCode?: number }).statusCode ?? 0;
        notes.push(`${s.label || s.id}: ${code || (e as Error).message}`);
        const dead = code === 404 || code === 410 || (s.fails ?? 0) + 1 >= 5;
        await SB.from("push_subs").update({ fails: (s.fails ?? 0) + 1, active: !dead }).eq("id", s.id);
      }
    }
    await SB.from("push_outbox").update({
      status: sent ? "sent" : (subs?.length ? "failed" : "no devices"),
      sent, failed, note: notes.join("; ").slice(0, 500) || null, sent_at: new Date().toISOString(),
    }).eq("id", m.id);
    out.push({ id: m.id, sent, failed });
  }
  return json({ ok: true, out });
});
