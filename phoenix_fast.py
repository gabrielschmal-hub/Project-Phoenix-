#!/usr/bin/env python3
"""
phoenix_fast.py — the 15-minute universe lane (27 Aug 2026).

WHAT IT IS
  The 5-minute Finnhub lane (Supabase edge function `quotes`) can carry ~130
  names per run and quotes what matters intraday: positions, macro, Elliott's
  list, the watchlist. This lane carries the OTHER ~7,000: every row of
  universe.csv gets a last price and a day change from Yahoo every 15 minutes
  during the US session, plus a cap-weighted intraday change per sector and
  per industry. Runs in GitHub Actions (free on a public repo), writes to
  Supabase over PostgREST, never touches phoenix.py or outputs/.

WHAT IT WRITES
  prices_universe   one row per ticker: last, prev_close, volume, currency,
                    source='yahoo_batch', quote_date, updated_at
  rotation_intraday one row per sector and per industry (US stocks, primary
                    lines): chg_pct (cap-weighted), rs (vs SPY), members
  heartbeats        lane='yahoo': ok, note

WHAT IT DOES NOT DO
  It does not decide anything. It does not replace prices_live (the app reads
  prices_live first, this table second, and shows which one it is looking at).
  A failed batch keeps the previous price on the row — last-good, never blank.

HONESTY RULES
  - coverage under 25%: nothing is written, heartbeat says why
  - the heartbeat carries the coverage every run; the app shows the timestamp
  - Actions cron is not punctual (5-15 min late under load, occasionally
    skipped): the timestamp on the row is the truth, not the schedule

INPUTS  universe.csv (repo root)   ENV  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
"""
import csv, json, os, sys, time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests

UNIVERSE = os.environ.get("PHOENIX_UNIVERSE", "universe.csv")
SUPA_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPA_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
FORCE = os.environ.get("PHOENIX_FORCE", "") == "1"
BATCH = 200            # yfinance symbols per request
CHUNK = 500            # rows per PostgREST upsert
MIN_COVERAGE = 0.25    # below this the run writes nothing


def log(msg):
    print(f"[fast] {msg}", flush=True)


# --------------------------------------------------------------------------- gates
def et_now():
    """(weekday 0=Mon, minutes since midnight ET, stamp). DST-proof via zoneinfo."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.now(timezone.utc) - timedelta(hours=4)
    return now.weekday(), now.hour * 60 + now.minute, now.strftime("%a %H:%M ET")


def market_open():
    dow, mins, stamp = et_now()
    # 09:25 -> 16:10 ET, weekdays: one run before the open catches the pre-market
    # last price, one after the close stamps the settle
    return (dow <= 4 and 9 * 60 + 25 <= mins <= 16 * 60 + 10), stamp


def supa(method, path, body=None, prefer=None):
    if not SUPA_URL or not SUPA_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
    h = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}", "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    r = requests.request(method, SUPA_URL + path, headers=h, data=json.dumps(body) if body is not None else None, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r


def frozen():
    try:
        rows = supa("GET", "/rest/v1/system_state?select=mode,reason&id=eq.1").json()
        return bool(rows) and rows[0].get("mode") == "frozen", (rows[0].get("reason") if rows else None)
    except Exception as e:
        log(f"system_state unreadable ({e}) — assuming live")
        return False, None


def beat(ok, note):
    try:
        supa("POST", "/rest/v1/heartbeats", [{"lane": "yahoo", "ts": datetime.now(timezone.utc).isoformat(), "ok": ok, "note": note[:300]}],
             prefer="resolution=merge-duplicates")
    except Exception as e:
        log(f"heartbeat failed: {e}")


# --------------------------------------------------------------------------- universe
def load_universe(path):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            tk = (r.get("ticker") or "").strip()
            if not tk:
                continue
            rows.append({
                "ticker": tk,
                "sector": (r.get("sector") or "").strip(),
                "industry": (r.get("industry") or "").strip(),
                "market_cap": float(r.get("market_cap") or 0),
                "market": (r.get("market") or "US").strip() or "US",
                "asset_type": (r.get("asset_type") or "stock").strip() or "stock",
                "currency": (r.get("currency") or "USD").strip() or "USD",
                "primary": str(r.get("cap_primary", "1") or "1").strip() != "0",
            })
    return rows


# --------------------------------------------------------------------------- yahoo
def pull_batch(symbols, out):
    """Two daily bars per symbol: yesterday's close and today's running bar."""
    import yfinance as yf
    try:
        df = yf.download(symbols, period="5d", interval="1d", group_by="ticker",
                         auto_adjust=False, threads=True, progress=False)
    except Exception as e:
        log(f"batch failed: {e}")
        return
    if df is None or len(df) == 0:
        return
    for s in symbols:
        try:
            sub = df if len(symbols) == 1 else df[s]
            sub = sub[["Close", "Volume"]].dropna(subset=["Close"])
            if len(sub) == 0:
                continue
            last = float(sub["Close"].iloc[-1])
            prev = float(sub["Close"].iloc[-2]) if len(sub) >= 2 else None
            vol = sub["Volume"].iloc[-1]
            vol = int(vol) if vol == vol else None
            qd = sub.index[-1]
            qd = qd.strftime("%Y-%m-%d") if hasattr(qd, "strftime") else str(qd)[:10]
            if last and last > 0:
                out[s] = {"last": round(last, 4), "prev_close": (round(prev, 4) if prev and prev > 0 else None),
                          "volume": vol, "quote_date": qd}
        except Exception:
            continue


def pull_all(symbols):
    out = {}
    remaining = list(symbols)
    for attempt, chunk in enumerate((BATCH, 80, 40), start=1):
        if not remaining:
            break
        if attempt > 1:
            log(f"retry pass {attempt}: {len(remaining)} missing, chunk={chunk}")
            time.sleep(3)
        before = len(out)
        for i in range(0, len(remaining), chunk):
            pull_batch(remaining[i:i + chunk], out)
        remaining = [s for s in remaining if s not in out]
        if len(out) == before:
            break
    return out


# --------------------------------------------------------------------------- rotation
def rotation_rows(universe, quotes, asof):
    """Cap-weighted intraday change per sector and per industry. US stocks,
    primary lines, names with both closes today. rs = change minus SPY's."""
    spy = quotes.get("SPY")
    spy_chg = ((spy["last"] / spy["prev_close"] - 1) * 100) if spy and spy.get("prev_close") else None
    groups = {"sector": defaultdict(list), "industry": defaultdict(list)}
    for r in universe:
        if r["asset_type"] != "stock" or r["market"] != "US" or not r["primary"]:
            continue
        q = quotes.get(r["ticker"])
        if not q or not q.get("prev_close") or r["market_cap"] <= 0:
            continue
        chg = (q["last"] / q["prev_close"] - 1) * 100
        if abs(chg) > 60:       # a bad print, not a move
            continue
        w = r["market_cap"]
        if r["sector"]:
            groups["sector"][r["sector"]].append((chg, w))
        if r["industry"]:
            groups["industry"][r["industry"]].append((chg, w))
    rows = []
    for scope, g in groups.items():
        for key, mem in g.items():
            if len(mem) < 3:
                continue
            W = sum(w for _c, w in mem)
            chg = sum(c * w for c, w in mem) / W if W else 0.0
            rows.append({"asof": asof, "scope": scope, "key": key, "chg_pct": round(chg, 3),
                         "rs": (round(chg - spy_chg, 3) if spy_chg is not None else None), "members": len(mem)})
    return rows


# --------------------------------------------------------------------------- main
def main():
    t0 = time.time()
    is_open, stamp = market_open()
    if not is_open and not FORCE:
        log(f"market closed ({stamp}) — nothing to do")
        return 0
    fz, why = frozen()
    if fz:
        log(f"frozen ({why}) — lane sleeps")
        return 0
    universe = load_universe(UNIVERSE)
    symbols = [r["ticker"] for r in universe if r["primary"]]
    log(f"{stamp} · pulling {len(symbols)} symbols from Yahoo in batches of {BATCH}")
    quotes = pull_all(symbols)
    cov = len(quotes) / len(symbols) if symbols else 0
    log(f"pull: {len(quotes)} ok, {len(symbols) - len(quotes)} missing ({cov*100:.1f}% coverage) in {time.time()-t0:.0f}s")
    if cov < MIN_COVERAGE:
        beat(False, f"pull unusable: {len(quotes)}/{len(symbols)} ({cov*100:.0f}%) — nothing written, previous prices kept")
        return 1

    now = datetime.now(timezone.utc).isoformat()
    ccy = {r["ticker"]: r["currency"] for r in universe}
    rows = []
    for s, q in quotes.items():
        c = ccy.get(s, "USD")
        scale = 0.01 if c == "GBX" else 1.0    # pence -> pounds, same convention as the engine
        rows.append({"ticker": s, "last": round(q["last"] * scale, 4),
                     "prev_close": (round(q["prev_close"] * scale, 4) if q["prev_close"] else None),
                     "volume": q["volume"], "currency": ("GBP" if c == "GBX" else c),
                     "source": "yahoo_batch", "quote_date": q["quote_date"], "updated_at": now})
    written = 0
    for i in range(0, len(rows), CHUNK):
        supa("POST", "/rest/v1/prices_universe?on_conflict=ticker", rows[i:i + CHUNK], prefer="resolution=merge-duplicates,return=minimal")
        written += len(rows[i:i + CHUNK])
    log(f"prices_universe: {written} rows upserted")

    rot = rotation_rows(universe, quotes, now)
    if rot:
        supa("POST", "/rest/v1/rotation_intraday", rot, prefer="return=minimal")
        # keep three days of the trail
        cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")   # no '+' in a query string
        try:
            supa("DELETE", f"/rest/v1/rotation_intraday?asof=lt.{cutoff}")
        except Exception as e:
            log(f"prune skipped: {e}")
        ns = sum(1 for r in rot if r["scope"] == "sector"); ni = len(rot) - ns
        log(f"rotation_intraday: {ns} sectors, {ni} industries")
    note = f"{len(quotes)}/{len(symbols)} ok ({cov*100:.0f}%) · {len(rot)} rotation rows · {time.time()-t0:.0f}s · {stamp}"
    beat(cov >= 0.8, note)
    log(note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
