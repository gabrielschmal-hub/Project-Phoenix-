#!/usr/bin/env python3
"""
PHOENIX SIGNAL LOG — append-only record of what the screener said, then what happened.

  python signal_log.py append   # after the daily run: snapshot screener_triggers -> signal_log (is_new / age_days)
  python signal_log.py mark     # nightly: fill r_1d/r_5d/r_20d, MFE/MAE, hit_stop for rows old enough
  python signal_log.py report   # print the pre-registered kill-criteria scorecard (see KILL_CRITERIA.md)

Only NEW signals (first appearance in 5 sessions) count as events. A name that sits on the list for
ten days is one signal, not ten — that is the "same names every day" problem, measured instead of guessed.
"""
import os, sys, json, datetime as dt
import pandas as pd

STOP_ATR = 2.5           # position policy: 2.5 x ATR(14)
NEW_WINDOW = 5           # sessions without appearing -> next appearance is a new event
MIN_SAMPLE = 30          # kill criteria need at least this many MARKED new signals
HURDLE_R = 0.054         # E must beat 0.054R, never zero

def sb():
    from supabase import create_client
    url = os.environ["SUPABASE_URL"].strip().rstrip("/"); key = os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip()
    return create_client(url, key)

def load_universe(path="universe.csv"):
    if not os.path.exists(path): return {}
    u = pd.read_csv(path); tcol = next((c for c in u.columns if c.lower() in ("ticker","symbol")), u.columns[0])
    out = {}
    for _, r in u.iterrows():
        out[str(r[tcol]).strip()] = {"sector": r.get("sector"), "industry": r.get("industry")}
    return out

def load_profitability(path="outputs/earnings_state.json"):
    """True/False per ticker if the engine's OCF-based classifier exists; None otherwise (never guessed)."""
    if not os.path.exists(path): return {}
    try:
        d = json.load(open(path)); rows = d.get("tickers", d) if isinstance(d, dict) else d
        out = {}
        it = rows.items() if isinstance(rows, dict) else ((r.get("ticker"), r) for r in rows)
        for t, r in it:
            if isinstance(r, dict) and "profitable" in r: out[t] = bool(r["profitable"])
        return out
    except Exception: return {}

def build_rows(triggers, prior, today, universe, profit, now=None, engine="screener_triggers"):
    """Pure function: today's trigger rows + prior log -> new signal_log rows. Tested offline."""
    now = now or dt.datetime.utcnow()
    last_seen = {}; streak = {}
    for r in prior:                                 # prior = list of {date, ticker, age_days}
        d = pd.Timestamp(r["date"]).date()
        if r["ticker"] not in last_seen or d > last_seen[r["ticker"]]:
            last_seen[r["ticker"]] = d; streak[r["ticker"]] = int(r.get("age_days") or 1)
    out = []
    for t in triggers:
        tk = str(t["ticker"]).strip(); ls = last_seen.get(tk)
        gap = (today - ls).days if ls else None
        is_new = ls is None or gap > NEW_WINDOW + 2        # +2 covers weekends in calendar days
        age = 1 if is_new else (streak.get(tk, 0) + 1)
        atr = float(t["atr_pct"]) if t.get("atr_pct") is not None else None
        asof = t.get("asof"); age_min = None
        try:
            ts = pd.Timestamp(asof)
            if ts.tzinfo is None: ts = ts.tz_localize("UTC")
            age_min = round((pd.Timestamp(now, tz="UTC") - ts).total_seconds() / 60, 1)
        except Exception: pass
        u = universe.get(tk, {})
        out.append({"date": today.isoformat(), "ticker": tk, "is_new": bool(is_new), "age_days": int(age),
                    "trigger": t.get("trigger"), "close": t.get("close"), "atr_pct": atr,
                    "stop_pct": round(STOP_ATR * atr, 4) if atr is not None else None,
                    "opp_score": t.get("opp_score"), "rank": t.get("rank"),
                    "sector": u.get("sector"), "industry": u.get("industry"),
                    "profitable_ocf": profit.get(tk), "data_asof": asof, "data_age_min": age_min, "engine": engine})
    return out

def cmd_append():
    s = sb(); today = dt.date.today()
    trig = s.table("screener_triggers").select("*").execute().data
    since = (today - dt.timedelta(days=NEW_WINDOW + 10)).isoformat()
    prior = s.table("signal_log").select("date,ticker,age_days").gte("date", since).execute().data
    rows = build_rows(trig, prior, today, load_universe(), load_profitability())
    if rows: s.table("signal_log").upsert(rows, on_conflict="date,ticker", ignore_duplicates=True).execute()
    os.makedirs("outputs", exist_ok=True)
    with open("outputs/signal_log.jsonl", "a") as f:            # git-tracked twin: survives any DB accident
        for r in rows: f.write(json.dumps(r, default=str) + "\n")
    print(f"[signal_log] {today} appended {len(rows)} rows · new events {sum(r['is_new'] for r in rows)}")

def mark_row(row, px):
    """px: DataFrame of daily OHLC after row.date (index = date). Returns forward-return fields."""
    if px is None or len(px) < 1 or not row.get("close"): return None
    c0 = float(row["close"]); stop = c0 * (1 - (row.get("stop_pct") or 0) / 100) if row.get("stop_pct") else None
    def r(n): return float(px["Close"].iloc[n - 1]) / c0 - 1 if len(px) >= n else None
    w = px.iloc[:20]
    out = {"r_1d": r(1), "r_5d": r(5), "r_20d": r(20) if len(px) >= 20 else None,
           "mfe_20d": float(w["High"].max()) / c0 - 1, "mae_20d": float(w["Low"].min()) / c0 - 1,
           "hit_stop_20d": bool(stop and float(w["Low"].min()) <= stop) if stop else None,
           "marked_at": dt.datetime.utcnow().isoformat()}
    return out

def cmd_mark():
    import yfinance as yf
    s = sb(); today = dt.date.today()
    due = s.table("signal_log").select("*").is_("r_20d", "null").lte("date", (today - dt.timedelta(days=2)).isoformat()).execute().data
    if not due: print("[signal_log] nothing to mark"); return
    for row in due:
        start = pd.Timestamp(row["date"]) + pd.Timedelta(days=1)
        try:
            px = yf.download(row["ticker"], start=start.date().isoformat(), interval="1d", auto_adjust=True, progress=False)
            if isinstance(px.columns, pd.MultiIndex): px.columns = px.columns.get_level_values(0)
        except Exception: continue
        m = mark_row(row, px)
        if m: s.table("signal_log").update(m).eq("date", row["date"]).eq("ticker", row["ticker"]).execute()
    print(f"[signal_log] marked {len(due)} rows")

def scorecard(rows):
    """Pre-registered kill criteria on NEW, MARKED signals. Returns dict; verdict is CONTINUE / STOP / INSUFFICIENT."""
    df = pd.DataFrame([r for r in rows if r.get("is_new") and r.get("r_20d") is not None])
    n = len(df)
    if n < MIN_SAMPLE: return {"n": n, "verdict": "INSUFFICIENT", "need": MIN_SAMPLE - n}
    R = df["stop_pct"].astype(float) / 100
    e_price = float((df["r_20d"].astype(float) / R).mean())          # 20-day return in units of initial risk
    hit = float(df["hit_stop_20d"].fillna(False).astype(bool).mean())
    mfe_r = float((df["mfe_20d"].astype(float) / R).median())
    win = float((df["r_20d"].astype(float) > 0).mean())
    fails = []
    if e_price < HURDLE_R: fails.append(f"E_price {e_price:.3f}R < {HURDLE_R}R")
    if hit > 0.60: fails.append(f"stop hit {hit:.0%} > 60%")
    if mfe_r < 1.0: fails.append(f"median MFE {mfe_r:.2f}R < 1R (never reaches breakeven)")
    return {"n": n, "e_price_R": round(e_price, 3), "stop_hit": round(hit, 3), "median_mfe_R": round(mfe_r, 2),
            "win_20d": round(win, 3), "fails": fails, "verdict": "STOP" if fails else "CONTINUE"}

def cmd_report():
    rows = sb().table("signal_log").select("*").execute().data
    sc = scorecard(rows); print(json.dumps(sc, indent=1))
    os.makedirs("outputs", exist_ok=True); json.dump(sc, open("outputs/signal_scorecard.json", "w"), indent=1)

if __name__ == "__main__":
    {"append": cmd_append, "mark": cmd_mark, "report": cmd_report}[sys.argv[1] if len(sys.argv) > 1 else "append"]()
