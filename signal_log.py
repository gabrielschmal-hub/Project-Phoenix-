#!/usr/bin/env python3
"""
PHOENIX SIGNAL LOG — append-only record of what the screener said, then what happened.

  python signal_log.py append   # snapshot screener_triggers -> signal_log (is_new / age_days)
  python signal_log.py mark     # fill entry close, forward returns, and the three-ATR path outcomes
  python signal_log.py report   # pre-registered kill-criteria scorecard + the three-ATR study

Only NEW signals (first appearance in 5 sessions) count as events. A name that sits on the list for
ten days is one signal, not ten — the "same names every day" problem, measured instead of guessed.

--------------------------------------------------------------------------------------------------
CHANGES 2 Sep 2026 (day-one audit found two bugs that made the log unmeasurable)

B1  close was NULL on all 163 day-one rows, because screener_triggers has no `close` column and
    build_rows read t.get("close"). mark_row returns None when close is falsy, so the nightly mark
    would have skipped every row forever and the verdict would have sat at INSUFFICIENT for good.
    Fix: mark downloads from the signal date INCLUSIVE and takes that bar's close as the entry, so
    entry and forward returns come from one series. append also records `entry_ref` from the live
    lane for the UI, which is never used in the scorecard.

B2  screener_triggers is an accumulating table keyed by ticker: each daily run upserts the names
    that triggered that day and older rows persist untouched. `select *` therefore returned six
    days of triggers stacked up (16 rows from 25 Aug through 60 from 1 Sep) and dated all 163 as
    today. Forward returns measured from four days after the signal are not the signal's returns.
    Fix: each row is dated by its OWN asof day and skipped if that (date, ticker) is already logged.
    Appending is idempotent, so a re-run backfills rather than duplicating.

THREE-ATR STUDY. Each signal is walked bar by bar under 1.5x / 2.0x / 2.5x ATR stops, recording
which of the stop, the 2:1 target and the 3:1 target was touched FIRST. MFE and MAE cannot answer
that: a name can show MFE 3R and MAE -1R and still have been stopped out on day two. Within a bar
the stop is assumed to resolve before the target — pessimistic, survival-first.

INTEGRITY NOTE. KILL_CRITERIA.md was pre-registered on the 2.5x stop. That remains the decision
variable. 1.5x and 2.0x are descriptive columns only: picking the best of three multiples after
seeing the data is how a losing screener gets kept alive. See scorecard()["decision_variable"].
--------------------------------------------------------------------------------------------------
"""
import os, sys, json, datetime as dt
import pandas as pd

STOP_ATR = 2.5           # position policy and pre-registered decision variable: 2.5 x ATR(14)
STOP_MULTS = (1.5, 2.0, 2.5)
NEW_WINDOW = 5           # sessions without appearing -> next appearance is a new event
MIN_SAMPLE = 30          # kill criteria need at least this many MARKED new signals
HURDLE_R = 0.054         # E must beat 0.054R, never zero
HORIZON = 20             # bars after entry


def mkey(m):
    """1.5 -> 'stop15'. Column prefix for a stop multiple."""
    return "stop" + str(int(round(m * 10)))


def sb():
    from supabase import create_client
    url = os.environ["SUPABASE_URL"].strip().rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip()
    return create_client(url, key)


def load_universe(path="universe.csv"):
    if not os.path.exists(path):
        return {}
    u = pd.read_csv(path)
    tcol = next((c for c in u.columns if c.lower() in ("ticker", "symbol")), u.columns[0])
    out = {}
    for _, r in u.iterrows():
        out[str(r[tcol]).strip()] = {"sector": r.get("sector"), "industry": r.get("industry")}
    return out


PROFIT_TRUE = ("profitable", "investing")      # earning AND operating-cash positive
PROFIT_FALSE = ("marginal", "lossmaking")      # thin/inconsistent, or profit without cash, or losing


def load_profitability(history_dir="outputs/history", stocks_path="outputs/stocks.json"):
    """(date, ticker) -> engine profitability state, from the engine's own daily snapshots.

    Day one was null on all 163 rows because the loader read outputs/earnings_state.json, which
    is a cursor of next-earnings dates and never held a classifier. The classifier
    (phoenix.profitability_flag, OCF not FCF) is written per candidate into
    outputs/history/signals_<date>.json and outputs/stocks.json. Read those.

    Returns {"by_date": {(date, ticker): state}, "latest": {ticker: state}}. States are the
    engine's own strings; "unknown" is a real answer and stays distinct from missing.
    """
    by_date, latest = {}, {}
    files = []
    if os.path.isdir(history_dir):
        files = sorted(f for f in os.listdir(history_dir) if f.startswith("signals_") and f.endswith(".json"))
    for fn in files:
        try:
            d = json.load(open(os.path.join(history_dir, fn)))
        except Exception:
            continue
        day = d.get("date") or fn[len("signals_"):-len(".json")]
        for r in (d.get("trade") or []) + (d.get("invest") or []):
            t, st = r.get("ticker"), r.get("profitability")
            if t and st:
                by_date[(day, str(t).strip())] = st
                latest[str(t).strip()] = st                  # files are sorted: last write wins
    if os.path.exists(stocks_path):
        try:
            d = json.load(open(stocks_path))
            for r in (d.get("trade_ranked") or []) + (d.get("stocks") or []):
                t, st = r.get("ticker"), r.get("profitability")
                if t and st and str(t).strip() not in latest:
                    latest[str(t).strip()] = st
        except Exception:
            pass
    return {"by_date": by_date, "latest": latest}


# Column names as Postgres actually stores them. An unquoted mixed-case identifier in a
# CREATE TABLE is folded to lower case, so the migration's mcap_B became mcap_b; the append
# sent mcap_B and PostgREST rejected the whole batch (PGRST204, 3 Sep). The engine snapshot
# still uses the mixed-case keys, hence the mapping.
CAND_FIELDS = ("mcap_b", "breakout", "days_on_list", "pos_vs_high", "surge",
               "industry_mom_3m", "dollar_vol_m", "trade_score")
SNAP_KEY = {"mcap_b": "mcap_B", "dollar_vol_m": "dollar_vol_M"}      # column -> key in signals_<date>.json
MARKET_FIELDS = ("regime", "regime_held_weeks", "gex_regime", "spx_close",
                 "spx_vs_50d_pct", "spx_vs_200d_pct", "vix")


def load_snapshots(history_dir="outputs/history"):
    """The engine's per-day snapshot, for the research lenses.

    Returns {"cand": {(date, ticker): {CAND_FIELDS...}}, "market": {date: {MARKET_FIELDS...}}}.
    The market block is null on every snapshot before 2 Sep 2026 (engine passed a key that never
    existed); those rows keep null regime. SPX fields for them are latched later by the mark step
    from the ^GSPC series, which is objective; the engine regime is not reconstructed.
    """
    cand, market = {}, {}
    if not os.path.isdir(history_dir):
        return {"cand": cand, "market": market}
    for fn in sorted(f for f in os.listdir(history_dir) if f.startswith("signals_") and f.endswith(".json")):
        try:
            d = json.load(open(os.path.join(history_dir, fn)))
        except Exception:
            continue
        day = d.get("date") or fn[len("signals_"):-len(".json")]
        ctx = d.get("context") or {}
        mk = dict(ctx.get("market") or {})
        if mk.get("regime") is None and ctx.get("regime"):
            mk["regime"] = ctx["regime"]
        if mk.get("spx_close") is None and ctx.get("spx_close") is not None:
            mk["spx_close"] = ctx["spx_close"]
        market[day] = {k: mk.get(k) for k in MARKET_FIELDS if mk.get(k) is not None}
        for r in (d.get("trade") or []) + (d.get("invest") or []):
            t = r.get("ticker")
            if not t:
                continue
            key = (day, str(t).strip())
            if key in cand:                         # trade book wins over invest for the same name
                continue
            cand[key] = {k: r.get(SNAP_KEY.get(k, k)) for k in CAND_FIELDS
                         if r.get(SNAP_KEY.get(k, k)) is not None}
    return {"cand": cand, "market": market}


def snapshot_fields(snap, day, ticker):
    """Lens fields for one signal: candidate fields for its own day + market state of that day."""
    if not snap:
        return {}
    out = dict((snap.get("cand") or {}).get((day, ticker)) or {})
    out.update((snap.get("market") or {}).get(day) or {})
    return out


def profit_lookup(profit, day, ticker):
    """State for one signal: the snapshot of its own day, else the latest known. None if unseen."""
    if not profit:
        return None
    if isinstance(profit, dict) and "by_date" in profit:
        st = profit["by_date"].get((day, ticker))
        return st if st is not None else profit["latest"].get(ticker)
    v = profit.get(ticker)                                   # legacy flat {ticker: bool} map
    return None if v is None else ("profitable" if v else "lossmaking")


def profit_bool(state):
    if state in PROFIT_TRUE:
        return True
    if state in PROFIT_FALSE:
        return False
    return None                                              # unknown or missing: never guessed


def _asof_day(asof, fallback):
    """The row's own trigger date. screener_triggers accumulates, so this is not `today`."""
    try:
        ts = pd.Timestamp(asof)
        if not pd.isna(ts):
            return ts.date()
    except Exception:
        pass
    return fallback


def build_rows(triggers, prior, today, universe, profit, now=None,
               engine="screener_triggers", live=None, snap=None):
    """Pure function: trigger rows + prior log -> signal_log rows, dated by their own asof."""
    now = now or dt.datetime.utcnow()
    live = live or {}

    logged = set()                       # (date, ticker) already in the log: never re-append
    seen = {}                            # ticker -> [dates seen], ascending
    for r in prior:
        d = pd.Timestamp(r["date"]).date()
        logged.add((d, str(r["ticker"]).strip()))
        seen.setdefault(str(r["ticker"]).strip(), []).append(d)
    for v in seen.values():
        v.sort()

    staged = []
    for t in triggers:
        tk = str(t["ticker"]).strip()
        d = _asof_day(t.get("asof"), today)
        if d > today:                    # a clock-skewed asof must not create a future signal
            d = today
        staged.append((d, tk, t))
    staged.sort(key=lambda x: (x[0], x[1]))   # ascending, so age_days builds forward in time

    out = []
    for d, tk, t in staged:
        if (d, tk) in logged:
            continue
        hist = [x for x in seen.get(tk, []) if x < d]
        prev = hist[-1] if hist else None
        gap = (d - prev).days if prev else None
        is_new = prev is None or gap > NEW_WINDOW + 2      # +2 covers weekends in calendar days
        age = 1 if is_new else sum(1 for x in hist if (d - x).days <= NEW_WINDOW + 2) + 1

        atr = float(t["atr_pct"]) if t.get("atr_pct") is not None else None
        asof = t.get("asof")
        age_min = None
        try:
            ts = pd.Timestamp(asof)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            age_min = round((pd.Timestamp(now, tz="UTC") - ts).total_seconds() / 60, 1)
        except Exception:
            pass

        u = universe.get(tk, {})
        row = {"date": d.isoformat(), "ticker": tk, "is_new": bool(is_new), "age_days": int(age),
               "trigger": t.get("trigger"), "close": None, "atr_pct": atr,
               "stop_pct": round(STOP_ATR * atr, 4) if atr is not None else None,
               "opp_score": t.get("opp_score"), "rank": t.get("rank"),
               "sector": u.get("sector"), "industry": u.get("industry"),
               "profitability": profit_lookup(profit, d.isoformat(), tk),
               "data_asof": asof, "data_age_min": age_min, "engine": engine}
        row["profitable_ocf"] = profit_bool(row["profitability"])
        row.update(snapshot_fields(snap, d.isoformat(), tk))     # lens fields, frozen today

        # entry_ref: provisional price for the UI so a signal is visible the evening it fires.
        # Only accepted when the live quote is from the signal day itself. Never enters the
        # scorecard — `close` from the daily bar is the only price the measurement trusts.
        lq = live.get(tk)
        if lq and lq.get("last") is not None:
            try:
                qd = pd.Timestamp(lq.get("quote_ts")).date()
            except Exception:
                qd = None
            if qd == d:
                row["entry_ref"] = float(lq["last"])
        out.append(row)
        seen.setdefault(tk, []).append(d)
        seen[tk].sort()
        logged.add((d, tk))
    return out


def enrich_profitability(s, profit):
    """Fill profitability on logged rows that are still null. Idempotent; runs every append.

    profitability is not a signal field, so the append-only guard permits this. A row that the
    engine has never classified stays null: filling is not guessing.
    """
    try:
        rows = s.table("signal_log").select("date,ticker").is_("profitability", "null").limit(5000).execute().data
    except Exception as e:
        print(f"[signal_log] enrich skipped ({e})"); return 0
    n = 0
    for r in rows:
        st = profit_lookup(profit, r["date"], r["ticker"])
        if st is None:
            continue
        s.table("signal_log").update({"profitability": st, "profitable_ocf": profit_bool(st)}) \
         .eq("date", r["date"]).eq("ticker", r["ticker"]).execute()
        n += 1
    if rows:
        print(f"[signal_log] profitability filled on {n}/{len(rows)} rows that were null")
    return n


def enrich_lenses(s, snap):
    """Fill lens fields still null on logged rows. Latched columns: fill once, never change."""
    if not snap or not snap.get("cand"):
        return 0
    try:
        rows = (s.table("signal_log").select("date,ticker," + ",".join(CAND_FIELDS + MARKET_FIELDS))
                .is_("mcap_b", "null").limit(5000).execute().data)
    except Exception as e:
        print(f"[signal_log] lens enrich skipped ({e})"); return 0
    n = 0
    for r in rows:
        f = snapshot_fields(snap, r["date"], r["ticker"])
        upd = {k: v for k, v in f.items() if r.get(k) is None}   # never touch a value already there
        if not upd:
            continue
        s.table("signal_log").update(upd).eq("date", r["date"]).eq("ticker", r["ticker"]).execute()
        n += 1
    if rows:
        print(f"[signal_log] lens fields filled on {n}/{len(rows)} rows")
    return n


def cmd_append():
    s = sb()
    today = dt.date.today()
    trig = s.table("screener_triggers").select("*").execute().data
    since = (today - dt.timedelta(days=90)).isoformat()      # wide enough to date backfilled asofs
    prior = s.table("signal_log").select("date,ticker,age_days").gte("date", since).execute().data
    try:
        live = {r["ticker"]: r for r in
                s.table("prices_live").select("ticker,last,quote_ts").execute().data}
    except Exception as e:
        print(f"[signal_log] prices_live unavailable ({e}) — entry_ref will be null")
        live = {}

    profit = load_profitability()
    print(f"[signal_log] profitability: {len(profit['by_date'])} (date,ticker) states across snapshots, "
          f"{len(profit['latest'])} tickers latest"
          + ("  <-- EMPTY: outputs/history/signals_*.json not in the checkout" if not profit["latest"] else ""))
    snap = load_snapshots()
    print(f"[signal_log] snapshots: {len(snap['cand'])} candidate-days, "
          f"{sum(1 for m in snap['market'].values() if m.get('regime'))}/{len(snap['market'])} days with a regime")
    enrich_profitability(s, profit)
    enrich_lenses(s, snap)

    rows = build_rows(trig, prior, today, load_universe(), profit, live=live, snap=snap)
    if not rows:
        print(f"[signal_log] {today}: nothing new to append ({len(trig)} triggers, all already logged)")
        return
    s.table("signal_log").upsert(rows, on_conflict="date,ticker", ignore_duplicates=True).execute()
    by_day = {}
    for r in rows:
        by_day[r["date"]] = by_day.get(r["date"], 0) + 1
    print(f"[signal_log] appended {len(rows)} rows · new events {sum(r['is_new'] for r in rows)}"
          f" · dates {', '.join(f'{k}:{v}' for k, v in sorted(by_day.items()))}")


def walk_path(bars, entry, atr_pct, mult):
    """Bar-by-bar outcome under one stop multiple.

    bars: sequence of (high, low, close) AFTER the entry bar, in order.
    Returns hit / day / tp2 / tp3 / r, where tp2 and tp3 mean the target was touched BEFORE the
    stop. Within a single bar the stop is assumed first: pessimistic, and the only assumption that
    cannot flatter the result.
    """
    if not atr_pct or entry is None or entry <= 0 or not bars:
        return {}
    risk = entry * mult * atr_pct / 100.0
    if risk <= 0:
        return {}
    stop, tp2, tp3 = entry - risk, entry + 2 * risk, entry + 3 * risk
    hit_day = tp2_day = tp3_day = None
    for i, (hi, lo, _c) in enumerate(bars, start=1):
        if lo <= stop:
            hit_day = i
            break
        if tp3_day is None and hi >= tp3:
            tp3_day = i
        if tp2_day is None and hi >= tp2:
            tp2_day = i
    r = -1.0 if hit_day else (bars[-1][2] - entry) / risk
    k = mkey(mult)
    return {k + "_hit": hit_day is not None, k + "_day": hit_day,
            k + "_tp2": tp2_day is not None, k + "_tp2_day": tp2_day,
            k + "_tp3": tp3_day is not None, k + "_tp3_day": tp3_day,
            k + "_r": round(r, 4)}


def spx_state(spx, day):
    """SPX close and distance to its 50/200-day means on `day`, from a daily ^GSPC series.

    Objective and reproducible, so it may be latched onto rows whose snapshot lacked it (the six
    days before the engine fix). The engine's own regime label is NOT reconstructed this way.
    """
    if spx is None or not len(spx):
        return {}
    try:
        c = spx["Close"].astype(float)
        upto = c[c.index <= pd.Timestamp(day)]
        if not len(upto):
            return {}
        last = float(upto.iloc[-1])
        out = {"spx_close": round(last, 2)}
        if len(upto) >= 50:
            out["spx_vs_50d_pct"] = round((last / float(upto.iloc[-50:].mean()) - 1) * 100, 2)
        if len(upto) >= 200:
            out["spx_vs_200d_pct"] = round((last / float(upto.iloc[-200:].mean()) - 1) * 100, 2)
        return out
    except Exception:
        return {}


def mark_row(row, px, spx=None):
    """px: daily OHLC from the signal date INCLUSIVE (index = date).

    Bar 0 is the signal day; its close is the entry, which is why `close` no longer has to come
    from the screener (it never did). Bars 1..20 are the forward path.
    """
    if px is None or len(px) < 2:
        return None
    entry = row.get("close")
    if entry in (None, 0):
        entry = float(px["Close"].iloc[0])
    entry = float(entry)
    if entry <= 0:
        return None
    fwd = px.iloc[1:1 + HORIZON]
    if not len(fwd):
        return None
    bars = list(zip(fwd["High"].astype(float), fwd["Low"].astype(float), fwd["Close"].astype(float)))

    def r(n):
        return float(fwd["Close"].iloc[n - 1]) / entry - 1 if len(fwd) >= n else None

    out = {"close": round(entry, 6), "close_src": "daily", "bars_marked": len(fwd),
           "r_1d": r(1), "r_5d": r(5), "r_20d": r(HORIZON) if len(fwd) >= HORIZON else None,
           "mfe_20d": float(fwd["High"].max()) / entry - 1,
           "mae_20d": float(fwd["Low"].min()) / entry - 1,
           "marked_at": dt.datetime.utcnow().isoformat()}
    atr = row.get("atr_pct")
    for m in STOP_MULTS:
        out.update(walk_path(bars, entry, atr, m))
    # legacy column kept so anything already reading it does not break: 2.5x is hit_stop_20d
    out["hit_stop_20d"] = out.get(mkey(STOP_ATR) + "_hit")
    if row.get("spx_close") is None:                        # latch once; the guard refuses a change
        out.update(spx_state(spx, row.get("date")))
    return out


def cmd_mark():
    import yfinance as yf
    s = sb()
    today = dt.date.today()
    due = (s.table("signal_log").select("*").is_("r_20d", "null")
           .lte("date", (today - dt.timedelta(days=2)).isoformat())
           .order("date").limit(2000).execute().data)
    if not due:
        print("[signal_log] nothing to mark")
        return
    spx = None
    if any(r.get("spx_close") is None for r in due):
        try:
            spx = yf.download("^GSPC", start=(pd.Timestamp(due[0]["date"]) - pd.Timedelta(days=320)).date().isoformat(),
                              interval="1d", auto_adjust=True, progress=False)
            if isinstance(spx.columns, pd.MultiIndex):
                spx.columns = spx.columns.get_level_values(0)
        except Exception as e:
            print(f"[signal_log] ^GSPC unavailable ({e}); SPX state stays null this run")
    ok = skipped = 0
    for row in due:
        start = pd.Timestamp(row["date"])                    # INCLUSIVE: bar 0 is the entry bar
        end = start + pd.Timedelta(days=int(HORIZON * 1.9) + 7)
        try:
            px = yf.download(row["ticker"], start=start.date().isoformat(),
                             end=end.date().isoformat(), interval="1d",
                             auto_adjust=True, progress=False)
            if isinstance(px.columns, pd.MultiIndex):
                px.columns = px.columns.get_level_values(0)
        except Exception:
            skipped += 1
            continue
        m = mark_row(row, px, spx=spx)
        if not m:
            skipped += 1
            continue
        if row.get("close") is not None:
            m.pop("close", None)                             # already latched; the guard forbids a change
            m.pop("close_src", None)
        s.table("signal_log").update(m).eq("date", row["date"]).eq("ticker", row["ticker"]).execute()
        ok += 1
    print(f"[signal_log] marked {ok} rows, skipped {skipped} (no data)")


def _policy_R(row, m):
    """Realised R for one signal under one stop multiple.

    stop  : stop only, held to the horizon
    tp2   : exit at +2R if the target came before the stop
    tp3   : exit at +3R if the target came before the stop
    """
    k = mkey(m)
    r = row.get(k + "_r")
    if r is None:
        return None
    hit = bool(row.get(k + "_hit"))
    out = {"stop": float(r)}
    out["tp2"] = 2.0 if row.get(k + "_tp2") else (-1.0 if hit else float(r))
    out["tp3"] = 3.0 if row.get(k + "_tp3") else (-1.0 if hit else float(r))
    return out


def scorecard(rows):
    """Pre-registered kill criteria on NEW, MARKED signals, plus the three-ATR study.

    The verdict is computed on the 2.5x stop held to the horizon — the variable pre-registered in
    KILL_CRITERIA.md before any signal was marked. The other multiples and the target policies are
    reported side by side but cannot change the verdict.
    """
    marked = [r for r in rows if r.get("is_new") and r.get("r_20d") is not None]
    n = len(marked)
    if n < MIN_SAMPLE:
        return {"n": n, "verdict": "INSUFFICIENT", "need": MIN_SAMPLE - n,
                "decision_variable": f"{STOP_ATR}x ATR, stop only, {HORIZON} bars",
                "logged_new": sum(1 for r in rows if r.get("is_new")),
                "lenses": lenses(marked), "min_cell": MIN_CELL,
                "asof": dt.datetime.utcnow().isoformat(timespec="minutes")}
    df = pd.DataFrame(marked)
    grid = {}
    for m in STOP_MULTS:
        k = mkey(m)
        pol = [_policy_R(r, m) for r in marked]
        pol = [p for p in pol if p]
        if not pol:
            continue
        risk = df["atr_pct"].astype(float) * m / 100.0
        mfe_r = float((df["mfe_20d"].astype(float) / risk).median()) if len(risk) else None
        grid[f"{m}x"] = {
            "n": len(pol),
            "E_stop_R": round(sum(p["stop"] for p in pol) / len(pol), 3),
            "E_tp2_R": round(sum(p["tp2"] for p in pol) / len(pol), 3),
            "E_tp3_R": round(sum(p["tp3"] for p in pol) / len(pol), 3),
            "stop_hit": round(float(df[k + "_hit"].fillna(False).astype(bool).mean()), 3),
            "tp2_first": round(float(df[k + "_tp2"].fillna(False).astype(bool).mean()), 3),
            "tp3_first": round(float(df[k + "_tp3"].fillna(False).astype(bool).mean()), 3),
            "median_mfe_R": round(mfe_r, 2) if mfe_r is not None else None,
        }

    d = grid.get(f"{STOP_ATR}x", {})
    e_price = d.get("E_stop_R")
    hit = d.get("stop_hit")
    mfe_r = d.get("median_mfe_R")
    win = float((df["r_20d"].astype(float) > 0).mean())
    fails = []
    if e_price is not None and e_price < HURDLE_R:
        fails.append(f"E_price {e_price:.3f}R < {HURDLE_R}R")
    if hit is not None and hit > 0.60:
        fails.append(f"stop hit {hit:.0%} > 60%")
    if mfe_r is not None and mfe_r < 1.0:
        fails.append(f"median MFE {mfe_r:.2f}R < 1R (never reaches breakeven)")
    return {"n": n, "e_price_R": e_price, "stop_hit": hit, "median_mfe_R": mfe_r,
            "win_20d": round(win, 3), "fails": fails,
            "verdict": "STOP" if fails else "CONTINUE",
            "decision_variable": f"{STOP_ATR}x ATR, stop only, {HORIZON} bars",
            "atr_grid": grid,
            "lenses": lenses(marked), "min_cell": MIN_CELL,
            "logged_new": sum(1 for r in rows if r.get("is_new")),
            "asof": dt.datetime.utcnow().isoformat(timespec="minutes"),
            "note": "1.5x and 2.0x and the target policies are descriptive. The pre-registered "
                    "verdict is the 2.5x stop-only column; choosing the best cell after the fact "
                    "is not a result."}


MIN_CELL = 20            # a lens cell below this is shown greyed: not a finding, not even a hint


def _bucket_mcap(v):
    if v is None: return None
    v = float(v)
    return "<2B" if v < 2 else "2-10B" if v < 10 else "10-50B" if v < 50 else ">50B"


def _bucket_atr(v):
    if v is None: return None
    v = float(v)
    return "<2%" if v < 2 else "2-4%" if v < 4 else "4-7%" if v < 7 else ">7%"


def _bucket_tit(row):
    """Sessions in trade under the 2.5x stop-only rule: the stop day, else the horizon."""
    d = row.get(mkey(STOP_ATR) + "_day")
    if d is None:
        return f"held {HORIZON}" if row.get("r_20d") is not None else None
    return "stopped d1-3" if d <= 3 else "stopped d4-10" if d <= 10 else f"stopped d11-{HORIZON}"


LENSES = {
    "sector":        lambda r: r.get("sector"),
    "industry":      lambda r: r.get("industry"),
    "profitability": lambda r: r.get("profitability"),
    "mcap":          lambda r: _bucket_mcap(r.get("mcap_b")),
    "atr":           lambda r: _bucket_atr(r.get("atr_pct")),
    "regime":        lambda r: r.get("regime"),
    "gex_regime":    lambda r: r.get("gex_regime"),
    "spx_vs_200d":   lambda r: (None if r.get("spx_vs_200d_pct") is None
                                else ("above 200d" if float(r["spx_vs_200d_pct"]) >= 0 else "below 200d")),
    "breakout":      lambda r: (None if r.get("breakout") is None else ("breakout" if r["breakout"] else "no breakout")),
    "days_on_list":  lambda r: (None if r.get("days_on_list") is None
                                else ("day 1" if int(r["days_on_list"]) <= 1 else "2-5" if int(r["days_on_list"]) <= 5 else "6+")),
    "time_in_trade": _bucket_tit,
}


def lenses(marked):
    """Descriptive cuts of the marked, new signals on the 2.5x stop-only column.

    Every cell carries its n and a `thin` flag below MIN_CELL. Nine lenses over one sample means
    some cell will look brilliant by chance; a cell is a hypothesis, and becomes a rule only
    through a dated entry in KILL_CRITERIA.md and a fresh sample logged after that date.
    """
    k = mkey(STOP_ATR)
    out = {}
    for name, fn in LENSES.items():
        groups = {}
        for r in marked:
            g = fn(r)
            if g is None or r.get(k + "_r") is None:
                continue
            groups.setdefault(str(g), []).append(r)
        cells = []
        for g, rs in groups.items():
            n = len(rs)
            risk = [float(r["atr_pct"]) * STOP_ATR / 100 for r in rs if r.get("atr_pct")]
            mfe = sorted(float(r["mfe_20d"]) / rk for r, rk in zip(rs, risk) if r.get("mfe_20d") is not None and rk > 0)
            cells.append({"group": g, "n": n, "thin": n < MIN_CELL,
                          "E_stop_R": round(sum(float(r[k + "_r"]) for r in rs) / n, 3),
                          "stop_hit": round(sum(1 for r in rs if r.get(k + "_hit")) / n, 3),
                          "tp2_first": round(sum(1 for r in rs if r.get(k + "_tp2")) / n, 3),
                          "median_mfe_R": round(mfe[len(mfe) // 2], 2) if mfe else None})
        cells.sort(key=lambda c: -c["n"])
        out[name] = cells
    return out


def write_twin(rows, path="outputs/signal_log.jsonl"):
    """Git-tracked copy of the whole log, rewritten in full on every report. Complete, sorted,
    idempotent: a fresh checkout does not start it from zero, and a DB accident has a fallback."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = sorted(rows, key=lambda r: (str(r.get("date")), str(r.get("ticker"))))
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str, sort_keys=True) + "\n")
    return len(rows)


def cmd_report():
    rows = sb().table("signal_log").select("*").limit(20000).execute().data
    print(f"[signal_log] twin: {write_twin(rows)} rows -> outputs/signal_log.jsonl")
    sc = scorecard(rows)
    print(json.dumps(sc, indent=1))
    g = sc.get("atr_grid") or {}
    if g:
        print("\n  stop      n   E(stop)  E(2:1)  E(3:1)  stop-hit  2R-first  3R-first  medMFE")
        for k, v in g.items():
            print(f"  {k:<7}{v['n']:>4}{v['E_stop_R']:>9.3f}{v['E_tp2_R']:>8.3f}{v['E_tp3_R']:>8.3f}"
                  f"{v['stop_hit']:>10.0%}{v['tp2_first']:>10.0%}{v['tp3_first']:>10.0%}"
                  f"{(v['median_mfe_R'] or 0):>8.2f}")
    os.makedirs("outputs", exist_ok=True)
    json.dump(sc, open("outputs/signal_scorecard.json", "w"), indent=1)


def cmd_check():
    """Fail the job if the log is in a state that a green run must never hide.

    3 Sep 2026: the first nightly mark did not run (GitHub skipped the cron) and nothing said so.
    A workflow that exits 0 with zero rows marked is worse than one that fails: it looks like
    patience. Run after mark; exit 1 when rows are due and none carry a mark.
    """
    s = sb()
    today = dt.date.today()
    due_cut = (today - dt.timedelta(days=2)).isoformat()
    rows = s.table("signal_log").select("date,close,bars_marked,r_20d,is_new").lte("date", due_cut).limit(20000).execute().data
    due = len(rows)
    marked = sum(1 for r in rows if r.get("bars_marked"))
    closed = sum(1 for r in rows if r.get("close") is not None)
    at_h = sum(1 for r in rows if r.get("r_20d") is not None)
    print(f"[signal_log] check: {due} rows due (date <= {due_cut}) · {marked} marked · {closed} with close · {at_h} at horizon")
    if due and marked == 0:
        print("[signal_log] CHECK FAILED: rows are due and none is marked. Yahoo blocked, or the mark never ran.")
        sys.exit(1)
    if due and closed < marked:
        print("[signal_log] CHECK FAILED: marked rows without an entry close — the latch did not fill.")
        sys.exit(1)
    print("[signal_log] check ok")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "append"
    {"append": cmd_append, "mark": cmd_mark, "report": cmd_report, "check": cmd_check}[cmd]()
