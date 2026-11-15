// Runs once a day (see "crons" in vercel.json). Makes one tiny database call so the
// free Supabase project never looks inactive and never gets paused.
module.exports = async (req, res) => {
  try {
    const r = await fetch("https://jbhhgxgkypjotyaqehzu.supabase.co/rest/v1/rpc/wealth_ping", {
      method: "POST",
      headers: { apikey: "sb_publishable_JFQf-suKuonfKYTAyFMsrA_puiJI2Sz", "Content-Type": "application/json" },
      body: "{}"
    });
    res.status(r.ok ? 200 : 502).json({ ok: r.ok, at: await r.json().catch(() => null) });
  } catch (e) {
    res.status(502).json({ ok: false });
  }
};
