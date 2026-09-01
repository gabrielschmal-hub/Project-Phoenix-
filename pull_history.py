#!/usr/bin/env python3
"""
PHOENIX — 10-YEAR HISTORY PULL  (run in Google Colab, or anywhere with network)

Pulls monthly adjusted closes for every ticker in universe.csv plus the
macro/ETF/crypto set, converts to EUR, computes monthly returns, and upserts
into Supabase `prices_history`. After this, the Portfolio Builder can price
ANY selection instantly and compute correlations across any mix.

Colab:
  !pip -q install yfinance supabase pandas
  import os; os.environ["SUPABASE_URL"]="https://tinribrtctphlmfytgqo.supabase.co"
  os.environ["SUPABASE_SERVICE_ROLE_KEY"]="..."      # service role, not anon
  !python pull_history.py --universe universe.csv --years 10

Re-runnable: upsert on (ticker, month). Run monthly to extend.
"""
import argparse, os, sys, time, math
import pandas as pd
import yfinance as yf

YEARS_DEFAULT = 10
BATCH = 150            # yfinance batch size — larger batches throttle more
RETRIES = 3
SLEEP_BETWEEN = 1.5

# Always-included non-universe lines. Yahoo symbol -> (display ticker, currency)
EXTRA = {
  "ACWI":"VWCE_PROXY", "^GSPC":"SPX", "GLD":"GOLD", "GC=F":"GOLD_FUT", "BTC-USD":"BTC",
  "IEF":"UST7_10", "AGG":"US_AGG", "TLT":"UST20", "^TNX":"US10Y", "DX-Y.NYB":"DXY",
  "BTCE.DE":"BTCE", "EUNA.DE":"AGGH", "VWCE.DE":"VWCE", "SGLD.MI":"SGLD", "CSSPX.MI":"CSSPX",
  "SWDA.MI":"SWDA", "IEGA.MI":"IEGA", "XEON.MI":"XEON",
}
SUFFIX_CCY = {".MI":"EUR", ".DE":"EUR", ".PA":"EUR", ".AS":"EUR", ".MC":"EUR", ".BR":"EUR", ".VI":"EUR",
              ".SW":"CHF", ".L":"SKIP"}   # .L skipped: LSE often quotes in pence — a 100x trap, not a conversion
def ccy_of(sym):
    for suf, c in SUFFIX_CCY.items():
        if sym.endswith(suf): return c
    return "USD"

def log(*a): print("[pull]", *a, flush=True)

def load_universe(path):
    u = pd.read_csv(path)
    col = next((c for c in u.columns if c.lower() in ("ticker","symbol","yahoo","ysym")), u.columns[0])
    syms = u[col].dropna().astype(str).str.strip().unique().tolist()
    log(f"universe: {len(syms)} symbols from column '{col}'")
    return syms

def fetch_batch(syms, years):
    last = None
    for attempt in range(RETRIES):
        try:
            df = yf.download(syms, period=f"{years}y", interval="1mo", auto_adjust=True,
                             group_by="ticker", threads=True, progress=False)
            return df
        except Exception as e:
            last = e; time.sleep(SLEEP_BETWEEN * (attempt+1) * 2)
    log(f"  batch FAILED after {RETRIES}: {last}")
    return None

def extract_close(df, sym):
    try:
        s = df[sym]["Close"] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
    except KeyError:
        return None
    s = s.dropna()
    return s if len(s) >= 12 else None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="universe.csv")
    ap.add_argument("--years", type=int, default=YEARS_DEFAULT)
    ap.add_argument("--dry", action="store_true", help="write parquet only, skip Supabase")
    ap.add_argument("--limit", type=int, default=0, help="first N symbols (testing)")
    a = ap.parse_args()

    syms = load_universe(a.universe) if os.path.exists(a.universe) else []
    if a.limit: syms = syms[:a.limit]
    allsyms = list(dict.fromkeys(syms + list(EXTRA)))

    # FX first — everything in USD gets converted
    FX = {}
    for c, pair in (("USD","EURUSD=X"), ("CHF","EURCHF=X")):      # units of c per 1 EUR
        f = yf.download(pair, period=f"{a.years+1}y", interval="1mo", auto_adjust=True, progress=False)["Close"]
        f = f.dropna(); f.index = f.index.to_period("M"); FX[c] = f
        log(f"fx {pair}: {len(f)} months")

    rows, failed = [], []
    for i in range(0, len(allsyms), BATCH):
        batch = allsyms[i:i+BATCH]
        log(f"batch {i//BATCH+1}/{math.ceil(len(allsyms)/BATCH)} ({len(batch)} syms)")
        df = fetch_batch(batch, a.years)
        if df is None: failed += batch; continue
        for sym in batch:
            s = extract_close(df, sym)
            if s is None: failed.append(sym); continue
            s.index = s.index.to_period("M")
            ccy = ccy_of(sym)
            if ccy == "SKIP":
                failed.append(sym); continue
            if ccy in FX:
                s = (s / FX[ccy].reindex(s.index)).dropna()   # native → EUR
            ret = s.pct_change()
            disp = EXTRA.get(sym, sym)
            for per, px in s.items():
                r = ret.get(per)
                rows.append({"ticker": disp, "month": per.to_timestamp().date().isoformat(),
                             "close_eur": round(float(px), 6),
                             "ret": None if (r is None or pd.isna(r)) else round(float(r), 6),
                             "ccy": ccy, "source": "yahoo"})
        time.sleep(SLEEP_BETWEEN)

    out = pd.DataFrame(rows)
    log(f"rows: {len(out):,}  tickers: {out.ticker.nunique()}  failed: {len(failed)}")
    out.to_parquet("prices_history.parquet", index=False)
    pd.Series(failed).to_csv("pull_failed.csv", index=False, header=["symbol"])
    log("wrote prices_history.parquet + pull_failed.csv")
    if a.dry: return

    from supabase import create_client
    url = os.environ["SUPABASE_URL"].strip().rstrip("/")            # .strip(): the %0a lesson
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip()
    sb = create_client(url, key)
    recs = out.to_dict("records"); CH = 2000
    for j in range(0, len(recs), CH):
        for attempt in range(RETRIES):
            try:
                sb.table("prices_history").upsert(recs[j:j+CH], on_conflict="ticker,month").execute(); break
            except Exception as e:
                if attempt == RETRIES-1: log(f"  upsert chunk {j} FAILED: {e}"); sys.exit(1)
                time.sleep(2*(attempt+1))
        if (j//CH) % 20 == 0: log(f"  upserted {min(j+CH,len(recs)):,}/{len(recs):,}")
    log("done")

if __name__ == "__main__":
    main()
