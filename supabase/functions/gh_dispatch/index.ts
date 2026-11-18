// Phoenix scheduler — starts GitHub workflows on time.
//
// GitHub's own cron is best-effort: it skipped the 06:00 daily run on 13 and 16 Nov 2026 and ran another 8 hours late.
// Supabase's pg_cron is exact, so pg_cron calls this function (with the vault JWT) and this function asks GitHub to run
// the workflow now (workflow_dispatch). Only the workflows listed below can be started.
//
// Secret: GH_DISPATCH_TOKEN, a fine-grained GitHub token for the Phoenix repo with "Actions: read and write".
// Every call stamps a heartbeat (lane "sched_<workflow>") so a failure shows in Phoenix's health checks.

import { createClient } from "jsr:@supabase/supabase-js@2";

const REPO = "gabrielschmal-hub/Project-Phoenix-";
const ALLOWED: Record<string, Record<string, string>> = {
  "daily.yaml": { scheduled: "true" },   // the morning run: opens the options forward test (18 Nov 2026)
  "mme-test.yaml": { force: "false" },
};

const SB = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } });
const json = (o: unknown, status = 200) =>
  new Response(JSON.stringify(o), { status, headers: { "Content-Type": "application/json" } });

Deno.serve(async (req) => {
  let wf = "";
  try { wf = String((await req.json())?.workflow ?? ""); } catch { /* no body */ }
  if (!(wf in ALLOWED)) return json({ ok: false, error: "workflow not allowed" }, 400);
  const lane = "sched_" + wf.replace(".yaml", "").replace(/-/g, "_");
  const token = Deno.env.get("GH_DISPATCH_TOKEN");
  const beat = (ok: boolean, note: string) =>
    SB.from("heartbeats").upsert({ lane, ts: new Date().toISOString(), ok, note });
  if (!token) {
    await beat(false, "GH_DISPATCH_TOKEN not set");
    return json({ ok: false, error: "GH_DISPATCH_TOKEN not set" }, 500);
  }
  const body: Record<string, unknown> = { ref: "main" };
  if (Object.keys(ALLOWED[wf]).length) body.inputs = ALLOWED[wf];
  let status = 0, text = "";
  for (let attempt = 1; attempt <= 3; attempt++) {              // GitHub hiccups: retry twice
    const r = await fetch(`https://api.github.com/repos/${REPO}/actions/workflows/${wf}/dispatches`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "phoenix-scheduler" },
      body: JSON.stringify(body),
    });
    status = r.status; text = (await r.text()).slice(0, 300);
    if (status === 204 || (status >= 400 && status < 500)) break;
    await new Promise((res) => setTimeout(res, 5000 * attempt));
  }
  const ok = status === 204;
  await beat(ok, ok ? `started ${wf}` : `GitHub ${status}: ${text}`);
  return json({ ok, status, workflow: wf, detail: ok ? undefined : text }, ok ? 200 : 502);
});
