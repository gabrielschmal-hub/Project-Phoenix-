#!/usr/bin/env python3
"""
PROJECT PHOENIX — single-file edition (v0 flat).
Everything in one file so it runs on GitHub with zero folder structure.
We split this into the proper package layout later (Working Copy / a computer).

Run:
  python phoenix.py --full          full daily pipeline
  python phoenix.py --engine gex    one engine
  python phoenix.py --init-db       just create the database
"""
import argparse, json, os, sqlite3, sys
from contextlib import contextmanager
from datetime import datetime, timezone

# ============================================================
# CONFIG — every threshold in one place
# ============================================================
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")   # set as a GitHub secret

STOCK = {
    "min_market_cap": 300e6,
    "fundamentals_min_cap": 2e9,
    "high_lookback_weeks": 104,
    "breakout_vol_surge_pct": 70,
    "accum_surge_pct": 30,
    "trade_weights":  {"rs_mkt": .30, "vol_surge": .25, "tightness": .20, "rs12": .15, "base": .10},
    "invest_weights": {"long_rs": .35, "durability": .25, "fundamentals": .25, "rs12": .15},
}
GEX = {
    "source": "SPY_x10", "risk_free": 0.045, "div_yield": 0.013,
    "otm_band": 0.15, "max_expiries": 16,
    # per-greek calibration (SPY proxy reads low by different amounts per greek).
    # These get FIT from paired engine-vs-source readings. 1.0 = uncalibrated (raw).
    "calib_net_gex": 1.0,
    "calib_vanna": 1.0,
    "calib_charm": 1.0,
    "calibrated": False,   # flips True once we've fit real factors
}
RISK = {
    "risk_conservative": 0.01, "risk_aggressive": 0.02, "atr_stop_mult": 2.0,
    "max_position_pct": 0.35, "max_heat_R": 3, "cooloff_losses": 6, "cooloff_days": 5,
}
REGIME = {
    "policy_tightening_2y_3m_bp": 40, "cpi_goldilocks_ceiling": 3.0, "hy_spread_stress_bp": 500,
}
DB_PATH = "macroflow.db"
OUTPUTS_DIR = "outputs"

# ============================================================
# DATABASE — SQLite, the system's memory
# ============================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_daily (date TEXT PRIMARY KEY, spx REAL, ndx REAL, vix REAL,
  wti REAL, dxy REAL, gold REAL, rate_2y REAL, rate_10y REAL, real_10y REAL, hy_spread REAL,
  regime TEXT, regime_confidence REAL, source_flags TEXT);
CREATE TABLE IF NOT EXISTS stock_daily (date TEXT, ticker TEXT, close REAL, volume REAL,
  PRIMARY KEY (date, ticker));
CREATE TABLE IF NOT EXISTS fundamentals (ticker TEXT, quarter_end TEXT, revenue REAL,
  net_income REAL, fcf REAL, debt REAL, equity REAL, gross_profit REAL,
  PRIMARY KEY (ticker, quarter_end));
CREATE TABLE IF NOT EXISTS stock_scores (date TEXT, ticker TEXT, trade_score REAL,
  invest_score REAL, vol_state TEXT, breakout INTEGER, industry TEXT, PRIMARY KEY (date, ticker));
CREATE TABLE IF NOT EXISTS industry_scores (date TEXT, industry TEXT, cap_wtd_momentum REAL,
  rank INTEGER, above_ma INTEGER, rising INTEGER, PRIMARY KEY (date, industry));
CREATE TABLE IF NOT EXISTS gex_daily (date TEXT PRIMARY KEY, net_gex REAL, regime TEXT,
  gamma_flip REAL, call_wall REAL, put_wall REAL, vanna REAL, charm REAL, source TEXT);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT,
  ticker TEXT, setup TEXT, qty REAL, entry REAL, stop REAL, entry_date TEXT, exit_date TEXT,
  exit_price REAL, status TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS universe (ticker TEXT PRIMARY KEY, sector TEXT, industry TEXT,
  market_cap REAL, updated TEXT);
"""

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    try:
        yield conn; conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
    print(f"Database ready at {DB_PATH}")

# ============================================================
# SOURCE REGISTRY — IBKR for accuracy, free for the rest, with fallbacks
# ============================================================
SOURCE_MAP = {
    "spx_price": "ibkr", "spx_options": "ibkr", "vix_term": "ibkr", "positions": "ibkr",
    "rates": "fred", "credit_spreads": "fred",
    "stock_universe": "yfinance", "sector_etfs": "yfinance", "gex_chain": "yfinance",
}
FALLBACK_MAP = {"spx_price": "yfinance", "spx_options": "yfinance", "vix_term": None, "positions": None}
def preferred(field): return SOURCE_MAP.get(field, "yfinance")
def fallback(field): return FALLBACK_MAP.get(field, None)

# ============================================================
# OUTPUTS — the JSON the frontend reads
# ============================================================
def write_json(name, data):
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    path = os.path.join(OUTPUTS_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=1)
    return path

def write_meta(source_flags=None, warnings=None):
    return write_json("meta", {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "source_flags": source_flags or {}, "warnings": warnings or [],
    })

# ============================================================
# ENGINES — pure functions (logic ported in Track 1 / Track 2)
# ============================================================
def compute_greeks(S, K, T, r, q, sigma):
    """Black-Scholes gamma, vanna, charm per share. Pure math."""
    import math
    if T <= 0 or sigma <= 0:
        return 0.0, 0.0, 0.0
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    eqT = math.exp(-q * T)
    gamma = eqT * pdf / (S * sigma * sqrtT)
    vanna = -eqT * pdf * d2 / sigma
    charm = -eqT * pdf * (2 * (r - q) * T - d2 * sigma * sqrtT) / (2 * T * sigma * sqrtT)
    return gamma, vanna, charm


def gex_engine(chain, spot, scale=1.0):
    """
    PURE FUNCTION. Compute dealer gamma/vanna/charm exposure from an options chain.

    Inputs:
      chain: list of dicts, each: {strike, T_years, kind('call'|'put'), open_interest, iv}
             (strike already in the SAME price space as `spot`)
      spot:  underlying price in SPX-equivalent terms
      scale: de-scaling factor for $ notional (10 if SPY x10 proxy, else 1)

    Returns the full GEX analysis dict (overview, levels, profile, confidence).
    No network, no file I/O — fully testable with synthetic chains.
    """
    r = GEX["risk_free"]
    q = GEX["div_yield"]
    otm = GEX["otm_band"]

    agg = {}  # strike -> exposures + OI
    for row in chain:
        K = row["strike"]
        if abs(K / spot - 1) > otm:
            continue
        oi = row.get("open_interest") or 0
        iv = row.get("iv") or 0
        # NaN-safe
        oi = 0.0 if oi != oi else float(oi)
        iv = 0.0 if iv != iv else float(iv)
        if oi <= 0 or iv <= 0:
            continue
        T = max(row.get("T_years", 0), 1e-6)
        g, vn, cm = compute_greeks(spot, K, T, r, q, iv)
        sign = 1.0 if row["kind"] == "call" else -1.0  # dealers long calls, short puts
        d = agg.setdefault(round(K), {"gex": 0.0, "vex": 0.0, "cex": 0.0, "coi": 0.0, "poi": 0.0})
        d["gex"] += g * oi * 100 * (spot ** 2) * 0.01 * sign / scale
        d["vex"] += vn * oi * 100 * spot * 0.01 * sign / scale
        d["cex"] += (cm * oi * 100 * spot * sign / scale) / 365.0
        if row["kind"] == "call":
            d["coi"] += oi
        else:
            d["poi"] += oi

    strikes = sorted(agg)
    if not strikes:
        return {"error": "no strikes within OTM band", "asof": _now()}

    net_gex = sum(v["gex"] for v in agg.values())
    net_vanna = sum(v["vex"] for v in agg.values())
    net_charm = sum(v["cex"] for v in agg.values())

    profile = []
    for K in strikes:
        v = agg[K]
        profile.append({
            "strike": K,
            "net_gex_B": round(v["gex"] / 1e9, 3),
            "coi": 0 if v["coi"] != v["coi"] else int(v["coi"]),
            "poi": 0 if v["poi"] != v["poi"] else int(v["poi"]),
        })

    # gamma flip: net-GEX sign change nearest spot
    flip = None
    for i in range(1, len(profile)):
        a, b = profile[i - 1], profile[i]
        if (a["net_gex_B"] <= 0 <= b["net_gex_B"]) or (a["net_gex_B"] >= 0 >= b["net_gex_B"]):
            mid = (a["strike"] + b["strike"]) / 2
            if flip is None or abs(mid - spot) < abs(flip - spot):
                flip = mid
    if flip is None:
        flip = spot

    above = [p for p in profile if p["strike"] > spot]
    below = [p for p in profile if p["strike"] < spot]
    call_wall = max(above, key=lambda p: p["coi"]) if above else None
    put_wall = max(below, key=lambda p: p["poi"]) if below else None
    pin = max(profile, key=lambda p: p["coi"] + p["poi"]) if profile else None

    return {
        "asof": _now(),
        "source": GEX["source"],
        "overview": {
            "spx_spot": round(spot, 2),
            "net_gex_B": round(net_gex / 1e9 * GEX["calib_net_gex"], 2),
            "regime": "Positive Gamma" if net_gex > 0 else "Negative Gamma",
            "gamma_flip": round(flip, 2),
            "dist_to_flip_pct": round((flip / spot - 1) * 100, 2),
            "net_vanna_B_per_volpt": round(net_vanna / 1e9 * GEX["calib_vanna"], 2),
            "net_charm_B_per_day": round(net_charm / 1e9 * GEX["calib_charm"], 2),
        },
        "raw": {
            # uncalibrated values — kept for transparency + refitting calibration
            "net_gex_B": round(net_gex / 1e9, 3),
            "net_vanna_B": round(net_vanna / 1e9, 3),
            "net_charm_B": round(net_charm / 1e9, 3),
            "calibrated": GEX["calibrated"],
        },
        "levels": {
            "pin": pin,
            "call_wall": call_wall,
            "put_wall": put_wall,
            "gamma_flip": round(flip, 2),
        },
        "profile": profile,
        "confidence": {
            "levels": "high",
            "regime_sign": "low_on_proxy" if GEX["source"].startswith("SPY") else "high",
            "note": "SPY proxy: flip/walls reliable, net-GEX sign approximate. True SPX (IBKR) fixes sign.",
        },
    }


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def _sma(vals, n):
    """Simple moving average of the last n values."""
    return sum(vals[-n:]) / n if len(vals) >= n else None

def _pct_rank(sorted_arr, v):
    """Cross-sectional percentile (0-100) of v within a sorted array."""
    if not sorted_arr:
        return 50.0
    return 100.0 * sum(1 for x in sorted_arr if x <= v) / len(sorted_arr)

def compute_industry_scores(stock_data, universe):
    """
    Cap-weighted industry momentum. PURE.
    stock_data: {ticker: [(date, close, volume), ...]} weekly bars (sorted)
    universe:   {ticker: {sector, industry, market_cap}}
    Returns: {industry: {momentum_3m, above_ma, rising}} and the set of 'passing' industries.
    """
    from collections import defaultdict
    members = defaultdict(list)
    for tk, info in universe.items():
        if tk in stock_data and len(stock_data[tk]) >= 60:
            members[info["industry"]].append(tk)

    scores, passing = {}, set()
    for ind, tickers in members.items():
        series, weights = [], []
        for tk in tickers:
            closes = [x[1] for x in stock_data[tk]]
            mc = universe[tk]["market_cap"]
            if len(closes) < 60 or mc <= 0:
                continue
            series.append(closes[-60:]); weights.append(mc)
        if len(series) < 2:
            continue
        W = sum(weights)
        # cap-weighted normalized index
        idx = [sum((series[i][t] / series[i][0]) * weights[i] for i in range(len(series))) / W
               for t in range(60)]
        ma10 = _sma(idx, 10)
        ma10_prev = _sma(idx[:-4], 10)
        mom_3m = round((idx[-1] / idx[-13] - 1) * 100, 1) if len(idx) >= 13 else 0
        above = ma10 is not None and idx[-1] > ma10
        rising = ma10 is not None and ma10_prev is not None and ma10 > ma10_prev
        scores[ind] = {"momentum_3m": mom_3m, "above_ma": above, "rising": rising}
        if above and rising:
            passing.add(ind)
    return scores, passing

def stock_engine(stock_data, universe, fundamentals=None):
    """
    PURE FUNCTION. The 6-gate + two-score (Trade/Investment) selection.
    Ports the validated MacroFlow selection logic.

    stock_data:   {ticker: [(date, close, volume), ...]} WEEKLY bars, sorted
    universe:     {ticker: {sector, industry, market_cap}}
    fundamentals: {ticker: {rev_yoy, net_margin, roe, fcf_positive}} or None

    Returns: {asof, meta, stocks: [ {...scored...} ]}
    No I/O, no network — testable with synthetic data.
    """
    fundamentals = fundamentals or {}
    ind_scores, passing = compute_industry_scores(stock_data, universe)

    candidates = []
    for tk, bars in stock_data.items():
        if tk not in universe or len(bars) < 40:
            continue
        closes = [x[1] for x in bars]
        vols = [x[2] for x in bars]
        last = closes[-1]
        mc = universe[tk]["market_cap"]
        ind = universe[tk]["industry"]
        if mc < STOCK["min_market_cap"]:
            continue

        ma40 = _sma(closes, 40)      # ~200-day
        ma10 = _sma(closes, 10)      # ~50-day
        ma30 = _sma(closes, 30)      # Weinstein 30-week
        if not (ma40 and ma10 and ma30):
            continue
        ma30_prev = _sma(closes[:-4], 30)
        stage2 = last > ma30 and ma30_prev is not None and ma30 > ma30_prev

        # GATES
        g1 = last > ma40
        g2 = last > ma10
        g3 = ind in passing               # industry above rising MA
        g6 = stage2                        # Weinstein stage 2
        win = closes[-STOCK["high_lookback_weeks"]:] if len(closes) >= STOCK["high_lookback_weeks"] else closes
        hi = max(win)
        pos_vs_high = (last / hi - 1) * 100
        at_high = pos_vs_high > -1.0
        near_high = -8 <= pos_vs_high <= -1
        g5 = at_high or near_high          # near or at 104wk high
        # g4 (market cap) already applied above
        if not (g1 and g2 and g3 and g5 and g6):
            continue

        # volume state
        rv = _sma(vols, 3)
        pv = _sma(vols[-13:-3], 10) if len(vols) >= 13 else None
        surge = (rv / pv - 1) * 100 if (rv and pv and pv > 0) else 0
        rising_px = last > closes[-4] if len(closes) >= 4 else False
        ma10_prev2 = _sma(closes[:-4], 10)
        ma10_rising = ma10_prev2 is not None and ma10 > ma10_prev2
        if surge >= STOCK["accum_surge_pct"] and rising_px:
            vstate = "ACCUM"
        elif surge >= STOCK["accum_surge_pct"]:
            vstate = "DISTRIB"
        else:
            vstate = "NEUTRAL"
        breakout = (at_high and surge >= STOCK["breakout_vol_surge_pct"]
                    and vstate == "ACCUM" and ma10_rising and surge <= 1000)

        # return features for scoring
        ret4 = (last / closes[-5] - 1) * 100 if len(closes) >= 5 else 0
        ret12 = (last / closes[-13] - 1) * 100 if len(closes) >= 13 else 0
        ret52 = (last / closes[-53] - 1) * 100 if len(closes) >= 53 else 0
        ext = (last / ma10 - 1) * 100          # extension above 50d
        weeks_in = 0
        for j in range(len(closes) - 1, max(29, len(closes) - 105), -1):
            m = _sma(closes[:j + 1], 30)
            if m and closes[j] > m:
                weeks_in += 1
            else:
                break

        candidates.append({
            "ticker": tk, "industry": ind, "mcap_B": round(mc / 1e9, 2),
            "surge": round(surge), "vol_state": vstate, "breakout": breakout,
            "pos_vs_high": round(pos_vs_high, 1), "industry_mom_3m": ind_scores.get(ind, {}).get("momentum_3m", 0),
            "_ret4": ret4, "_ret12": ret12, "_ret52": ret52, "_ext": ext, "_weeks_in": weeks_in,
            "_fund": fundamentals.get(tk),
        })

    # cross-sectional arrays for percentile scoring
    def arr(key):
        return sorted(c[key] for c in candidates)
    A = {k: arr(k) for k in ["_ret4", "_ret12", "_ret52", "_ext", "surge", "_weeks_in"]}
    from collections import defaultdict
    fvals = defaultdict(list)
    for c in candidates:
        f = c["_fund"]
        if f:
            for k in ["rev_yoy", "net_margin", "roe"]:
                if f.get(k) is not None:
                    fvals[k].append(f[k])
    for k in fvals:
        fvals[k] = sorted(fvals[k])

    def fund_score(f):
        if not f:
            return None
        parts, wts = [], []
        if f.get("rev_yoy") is not None:
            parts.append(_pct_rank(fvals["rev_yoy"], f["rev_yoy"])); wts.append(.35)
        if f.get("net_margin") is not None:
            parts.append(_pct_rank(fvals["net_margin"], f["net_margin"])); wts.append(.30)
        if f.get("roe") is not None:
            parts.append(_pct_rank(fvals["roe"], f["roe"])); wts.append(.25)
        parts.append(100 if f.get("fcf_positive") else 30); wts.append(.10)
        return sum(p * w for p, w in zip(parts, wts)) / sum(wts) if parts else None

    tw = STOCK["trade_weights"]
    iw = STOCK["invest_weights"]
    for c in candidates:
        rs_mkt = _pct_rank(A["_ret4"], c["_ret4"])
        rs12 = _pct_rank(A["_ret12"], c["_ret12"])
        long_rs = _pct_rank(A["_ret52"], c["_ret52"])
        tightness = 100 - _pct_rank(A["_ext"], c["_ext"])
        vol_p = _pct_rank(A["surge"], c["surge"])
        durability = _pct_rank(A["_weeks_in"], c["_weeks_in"])
        ext_pen = max(0, c["_ext"] - 15) * 0.8

        trade = (rs_mkt * tw["rs_mkt"] + vol_p * tw["vol_surge"] + tightness * tw["tightness"]
                 + rs12 * tw["rs12"] + 50 * tw["base"] + (3 if c["breakout"] else 0) - ext_pen)
        c["trade_score"] = round(max(0, min(100, trade)))

        fs = fund_score(c["_fund"])
        if fs is not None:
            invest = long_rs * iw["long_rs"] + durability * iw["durability"] + fs * iw["fundamentals"] + rs12 * iw["rs12"]
            c["fund_score"] = round(fs)
        else:
            # redistribute fundamentals weight when no data
            invest = long_rs * 0.47 + durability * 0.33 + rs12 * 0.20
            c["fund_score"] = None
        c["invest_score"] = round(max(0, min(100, invest)))

        t, i = c["trade_score"], c["invest_score"]
        c["label"] = "BOTH" if (t >= 70 and i >= 70) else ("TRADE" if t >= 70 else ("INVEST" if i >= 70 else "WATCH"))
        # clean up internal fields
        for k in list(c.keys()):
            if k.startswith("_"):
                del c[k]

    breakouts = [c for c in candidates if c["breakout"]]
    return {
        "asof": _now(),
        "meta": {
            "gate_passers": len(candidates),
            "breakouts": len(breakouts),
            "industries_passing": len(passing),
        },
        "stocks": sorted(candidates, key=lambda c: -c["invest_score"]),
    }


# ============================================================
# GEX DATA FETCH — the swappable source (SPY proxy now, IBKR later)
# Kept SEPARATE from gex_engine so we can change the source without touching the math.
# ============================================================
def fetch_gex_chain_yfinance():
    """Fetch SPX options chain via SPY x10 proxy. Returns (normalized_chain, spot, scale)."""
    import yfinance as yf
    from datetime import datetime
    scale = 10.0
    tk = yf.Ticker("SPY")
    spot = float(tk.history(period="1d")["Close"].iloc[-1]) * scale
    exps = tk.options[:GEX["max_expiries"]]
    now = datetime.now()
    chain = []
    for exp in exps:
        T = max((datetime.strptime(exp, "%Y-%m-%d") - now).days, 1) / 365.0
        try:
            oc = tk.option_chain(exp)
        except Exception:
            continue
        for df, kind in [(oc.calls, "call"), (oc.puts, "put")]:
            for _, row in df.iterrows():
                chain.append({
                    "strike": float(row["strike"]) * scale,
                    "T_years": T,
                    "kind": kind,
                    "open_interest": row.get("openInterest"),
                    "iv": row.get("impliedVolatility"),
                })
    return chain, spot, scale


def run_gex():
    """Fetch + compute + write gex.json. The full GEX vertical slice."""
    print("[gex] fetching chain (SPY proxy)...")
    chain, spot, scale = fetch_gex_chain_yfinance()
    print(f"[gex] {len(chain)} contracts, spot {spot:.2f}")
    result = gex_engine(chain, spot, scale)
    write_json("gex", result)
    ov = result.get("overview", {})
    print(f"[gex] Net {ov.get('net_gex_B')}B {ov.get('regime')}, flip {ov.get('gamma_flip')}")
    print("[gex] wrote outputs/gex.json")
    return result




# ============================================================
# STOCK DATA FETCH — the heavy one. Needs universe + weekly history + fundamentals.
# This is where the automated pipeline meets reality (see notes in run_stocks).
# ============================================================
def load_universe_from_csv(path="universe.csv"):
    """Load ticker -> {sector, industry, market_cap} from a committed CSV."""
    import csv, os
    if not os.path.exists(path):
        return {}
    uni = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                uni[row["ticker"]] = {
                    "sector": row.get("sector", ""),
                    "industry": row.get("industry", ""),
                    "market_cap": float(row.get("market_cap") or 0),
                }
            except Exception:
                continue
    return uni

def load_weekly_from_csv(path="stock_weekly.csv"):
    """Load {ticker: [(date, close, volume), ...]} from a committed CSV."""
    import csv, os
    from collections import defaultdict
    if not os.path.exists(path):
        return {}
    data = defaultdict(list)
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                data[row["ticker"]].append((row["date"], float(row["close"]), float(row["volume"])))
            except Exception:
                continue
    for tk in data:
        data[tk].sort()
    return dict(data)

def load_fundamentals_from_csv(path="fundamentals.csv"):
    """Load {ticker: {rev_yoy, net_margin, roe, fcf_positive}} — pre-computed latest quarter."""
    import csv, os
    if not os.path.exists(path):
        return {}
    f = {}
    with open(path) as fh:
        for row in csv.DictReader(fh):
            try:
                f[row["ticker"]] = {
                    "rev_yoy": float(row["rev_yoy"]) if row.get("rev_yoy") else None,
                    "net_margin": float(row["net_margin"]) if row.get("net_margin") else None,
                    "roe": float(row["roe"]) if row.get("roe") else None,
                    "fcf_positive": row.get("fcf_positive", "").lower() in ("1", "true", "yes"),
                }
            except Exception:
                continue
    return f

def _merge_daily_into_weekly(weekly, daily_bars):
    """
    Merge the newest daily bars onto the static weekly history IN MEMORY.
    weekly:      {ticker: [(date, close, vol), ...]} the 2-yr static history
    daily_bars:  {ticker: [(date, close, vol), ...]} recent daily (last few days)
    For each ticker, the current (partial) week's bar is updated to the latest daily close;
    daily volume for the week-so-far is summed and scaled to a full-week equivalent.
    Returns a NEW merged dict (does not mutate inputs).
    """
    from datetime import datetime, timedelta
    from collections import defaultdict

    def monday(dstr):
        dt = datetime.strptime(dstr, "%Y-%m-%d")
        return (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")

    merged = {tk: list(bars) for tk, bars in weekly.items()}
    for tk, dbars in daily_bars.items():
        if not dbars:
            continue
        # group daily by ISO week
        wk = defaultdict(list)
        for d, c, v in dbars:
            wk[monday(d)].append((d, c, v))
        for mon, days in wk.items():
            days.sort()
            close = days[-1][1]
            vol = sum(x[2] for x in days)
            nd = len(days)
            vol_full = vol * (5.0 / nd) if nd < 5 else vol   # scale partial week
            if tk not in merged:
                merged[tk] = []
            # replace existing bar for this week, else append
            replaced = False
            for i, row in enumerate(merged[tk]):
                if row[0] == mon:
                    merged[tk][i] = (mon, round(close, 4), vol_full)
                    replaced = True
                    break
            if not replaced:
                merged[tk].append((mon, round(close, 4), vol_full))
        merged[tk].sort()
    return merged


def fetch_daily_bars_yfinance(tickers, days=5):
    """
    Lighter pull: just the last few DAILY bars for the given tickers.
    Returns {ticker: [(date, close, vol), ...]} and a 'degraded' flag if the pull looks short.
    """
    import yfinance as yf
    import pandas as pd
    from collections import defaultdict
    out = defaultdict(list)
    got, failed = 0, 0
    CHUNK = 250
    for i in range(0, len(tickers), CHUNK):
        batch = tickers[i:i + CHUNK]
        try:
            df = yf.download(batch, period=f"{max(days,5)}d", interval="1d",
                             group_by="ticker", auto_adjust=True, threads=True, progress=False)
            for t in batch:
                try:
                    sub = df if len(batch) == 1 else df[t]
                    sub = sub[["Close", "Volume"]].dropna()
                    if len(sub) == 0:
                        failed += 1; continue
                    for dt, row in sub.iterrows():
                        out[t].append((dt.strftime("%Y-%m-%d"), round(float(row["Close"]), 4), float(row["Volume"])))
                    got += 1
                except Exception:
                    failed += 1
        except Exception:
            failed += len(batch)
    # degraded if we got materially fewer tickers than requested (Yahoo throttled)
    degraded = got < len(tickers) * 0.8
    return dict(out), degraded, got, failed


def run_stocks(auto_pull=True):
    """
    Run the stock engine and write stocks.json.

    DATA MODEL (the efficient one):
      - stock_weekly.csv  : the 2-YEAR history, uploaded ONCE, appended rarely (STATIC)
      - daily_recent.csv  : the newest few days (committed or auto-pulled)
      - universe.csv      : ticker -> sector/industry/mcap (refresh ~monthly)
      - fundamentals.csv  : pre-computed (refresh ~quarterly)
    We never re-pull the 2 years. We merge the newest day onto the stored history in memory.

    auto_pull: if True and yfinance is available, pull the latest daily bars for the
               universe (lighter than full history). Falls back to committed daily_recent.csv
               or to the static history alone if the pull is degraded.
    """
    import os
    universe = load_universe_from_csv()
    weekly = load_weekly_from_csv()
    if not universe or not weekly:
        print("[stocks] SKIPPED — universe.csv / stock_weekly.csv not found in repo")
        print("[stocks]   (upload these ONCE to enable the stock engine)")
        return None

    fundamentals = load_fundamentals_from_csv()

    # --- get the newest daily bars: auto-pull, else committed file ---
    daily = {}
    note = ""
    if auto_pull:
        try:
            tickers = list(universe.keys())
            print(f"[stocks] auto-pulling latest daily bars for {len(tickers)} tickers...")
            daily, degraded, got, failed = fetch_daily_bars_yfinance(tickers)
            print(f"[stocks] daily pull: {got} ok, {failed} failed")
            if degraded:
                note = f"daily auto-pull degraded ({got}/{len(tickers)}) — Yahoo may have throttled"
                print(f"[stocks] WARNING: {note}; trying committed daily_recent.csv")
                daily = {}   # fall through to committed file
        except Exception as e:
            note = f"auto-pull failed: {e}"
            print(f"[stocks] {note}; trying committed daily_recent.csv")

    if not daily:
        daily = load_weekly_from_csv("daily_recent.csv")  # same 3-col format
        if daily:
            print(f"[stocks] using committed daily_recent.csv ({len(daily)} tickers)")

    # --- merge newest day onto static history, then score ---
    if daily:
        weekly = _merge_daily_into_weekly(weekly, daily)
        print(f"[stocks] merged newest bars onto history")
    else:
        note = (note + "; " if note else "") + "no fresh daily data — scoring on committed history as-is"
        print(f"[stocks] {note}")

    print(f"[stocks] scoring {len(weekly)} tickers ({len(fundamentals)} with fundamentals)")
    result = stock_engine(weekly, universe, fundamentals)
    if note:
        result["meta"]["data_note"] = note
    write_json("stocks", result)
    m = result["meta"]
    print(f"[stocks] {m['gate_passers']} passers, {m['breakouts']} breakouts, "
          f"{m['industries_passing']} industries passing")
    print("[stocks] wrote outputs/stocks.json")
    return result

# ============================================================
# CALIBRATION — collect paired engine-vs-source readings, fit per-greek factors.
# The SPY proxy reads low by a (hopefully stable) ratio per greek. We log both,
# then set calib factors = median(source/engine_raw) once the ratios look stable.
# ============================================================
CALIB_LOG = "calibration_log.json"

def calib_log_add(date, source_net_gex, source_vanna, source_charm):
    """Log today's SOURCE values next to today's engine RAW values (from outputs/gex.json)."""
    import json, os
    # read today's engine raw output
    try:
        with open(os.path.join(OUTPUTS_DIR, "gex.json")) as f:
            gex = json.load(f)
        raw = gex.get("raw", {})
    except Exception as e:
        print(f"Could not read outputs/gex.json: {e}")
        return
    entry = {
        "date": date,
        "engine_net_gex": raw.get("net_gex_B"),
        "engine_vanna": raw.get("net_vanna_B"),
        "engine_charm": raw.get("net_charm_B"),
        "source_net_gex": source_net_gex,
        "source_vanna": source_vanna,
        "source_charm": source_charm,
    }
    log = []
    if os.path.exists(CALIB_LOG):
        with open(CALIB_LOG) as f:
            log = json.load(f)
    # replace same-date entry if present
    log = [e for e in log if e.get("date") != date]
    log.append(entry)
    with open(CALIB_LOG, "w") as f:
        json.dump(log, f, indent=1)
    print(f"Logged {date}. Total paired readings: {len(log)}")

def calib_analyze():
    """Show per-greek source/engine ratios across all logged days + suggested factors."""
    import json, os, statistics
    if not os.path.exists(CALIB_LOG):
        print("No calibration log yet. Run: python phoenix.py --calib-add ...")
        return
    with open(CALIB_LOG) as f:
        log = json.load(f)
    if not log:
        print("Calibration log is empty.")
        return
    print(f"=== Calibration analysis ({len(log)} paired readings) ===\n")
    for greek in ["net_gex", "vanna", "charm"]:
        ratios = []
        print(f"{greek.upper()}:")
        for e in sorted(log, key=lambda x: x["date"]):
            eng = e.get(f"engine_{greek}"); src = e.get(f"source_{greek}")
            if eng and src and eng != 0:
                r = src / eng
                ratios.append(r)
                print(f"  {e['date']}: engine {eng:+.2f}  source {src:+.2f}  ratio {r:+.2f}x")
        if ratios:
            med = statistics.median(ratios)
            spread = (max(ratios) - min(ratios))
            stable = "STABLE" if len(ratios) >= 3 and spread < abs(med) * 0.5 else \
                     ("need more data" if len(ratios) < 3 else "UNSTABLE (proxy relationship varies)")
            print(f"  -> median ratio {med:+.2f}x  [{stable}]")
            print(f"     suggested calib factor: {med:.2f}\n")
        else:
            print("  (no valid pairs)\n")
    print("Once ratios are STABLE, set them in the GEX config:")
    print('  "calib_net_gex": <median>, "calib_vanna": <median>, "calib_charm": <median>,')
    print('  "calibrated": True')


# ============================================================
# SCHEDULER — orchestrates the daily run
# ============================================================
def run_full():
    print("=== Phoenix full run ===")
    init_db()
    warnings, flags = [], {}
    print("[collect] collectors not yet wired")
    print("[store]   db writes not yet wired")
    try:
        run_gex()
    except Exception as e:
        warnings.append(f"GEX failed: {e}")
        print(f"[gex] FAILED (non-fatal): {e}")
    try:
        run_stocks()
    except Exception as e:
        warnings.append(f"stocks failed: {e}")
        print(f"[stocks] FAILED (non-fatal): {e}")
    write_meta(source_flags=flags, warnings=warnings)
    print("[write]   meta.json written")
    print("=== done — scaffold runs clean ===")

def run_engine(name):
    print(f"=== Phoenix engine: {name} ===")
    if name == "gex":
        run_gex()
    elif name == "stocks":
        run_stocks()
    else:
        print(f"[{name}] not yet wired — coming next")

# ============================================================
# ENTRYPOINT
# ============================================================
def main():
    p = argparse.ArgumentParser(description="Project Phoenix (flat v0)")
    p.add_argument("--full", action="store_true")
    p.add_argument("--engine", type=str)
    p.add_argument("--init-db", action="store_true")
    p.add_argument("--calib-add", nargs=4, metavar=("DATE","NET_GEX","VANNA","CHARM"),
                   help="log source values: DATE net_gex vanna charm (e.g. 2026-07-03 17.78 51.2 18.4)")
    p.add_argument("--calib-analyze", action="store_true", help="show calibration ratios + suggested factors")
    a = p.parse_args()
    if a.init_db: init_db()
    elif a.full: run_full()
    elif a.engine: run_engine(a.engine)
    elif a.calib_add:
        d, ng, vn, cm = a.calib_add
        calib_log_add(d, float(ng), float(vn), float(cm))
    elif a.calib_analyze: calib_analyze()
    else: p.print_help(); sys.exit(1)

if __name__ == "__main__":
    main()
