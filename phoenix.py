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
        return {"error": "no strikes within OTM band", "asof": _now(),
                "confidence": {"levels": "INVALID", "regime_sign": "INVALID",
                               "note": "Empty option chain — data pull failed/throttled."}}

    # --- DATA QUALITY GUARD: detect a degenerate (throttled/empty) pull ---
    total_oi = sum(v["coi"] + v["poi"] for v in agg.values())
    total_put_oi = sum(v["poi"] for v in agg.values())
    n_strikes = len(strikes)
    # A healthy SPX/SPY chain has hundreds of strikes and millions of OI near spot.
    # If we have very few strikes, tiny OI, or zero puts, the pull is broken.
    bad_pull = (n_strikes < 20) or (total_oi < 50000) or (total_put_oi < 1000)
    if bad_pull:
        return {
            "asof": _now(),
            "source": GEX["source"],
            "error": "degenerate_chain",
            "overview": {"spx_spot": round(spot, 2), "net_gex_B": None, "regime": "UNKNOWN",
                         "gamma_flip": None, "dist_to_flip_pct": None,
                         "net_vanna_B_per_volpt": None, "net_charm_B_per_day": None},
            "levels": {"pin": None, "call_wall": None, "put_wall": None, "gamma_flip": None},
            "profile": [],
            "confidence": {"levels": "INVALID", "regime_sign": "INVALID",
                "note": f"Bad data pull: only {n_strikes} strikes, {int(total_oi)} total OI, "
                        f"{int(total_put_oi)} put OI. Yahoo likely throttled the options chain. "
                        f"NOT a valid reading — do not use."},
            "diagnostics": {"n_strikes": n_strikes, "total_oi": int(total_oi),
                            "total_put_oi": int(total_put_oi)},
        }

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

def compute_industry_performance(stock_data, universe, daily_ret=None):
    """
    Cap-weighted industry returns across 4 timeframes for the Screener industry tile.
    Returns list of {industry, n, mcap_B, d1, w1, m1, m3, above, rising} sorted by d1 desc.
    - d1: cap-weighted 1-day return (from daily_ret map, if available)
    - w1/m1/m3: cap-weighted 1-week / ~1-month / 3-month return from weekly index
    PURE except it reads the passed-in daily_ret dict.
    """
    from collections import defaultdict
    daily_ret = daily_ret or {}
    members = defaultdict(list)
    for tk, info in universe.items():
        if tk in stock_data and len(stock_data[tk]) >= 60:
            members[info["industry"]].append(tk)

    out = []
    for ind, tickers in members.items():
        series, weights, d1s, d1w = [], [], [], []
        for tk in tickers:
            closes = [x[1] for x in stock_data[tk]]
            mc = universe[tk]["market_cap"]
            if len(closes) < 60 or mc <= 0:
                continue
            series.append(closes[-60:]); weights.append(mc)
            # accumulate cap-weighted 1-day return where we have it
            if tk in daily_ret and daily_ret[tk] is not None:
                d1s.append(daily_ret[tk] * mc); d1w.append(mc)
        if len(series) < 2:
            continue
        W = sum(weights)
        idx = [sum((series[i][t] / series[i][0]) * weights[i] for i in range(len(series))) / W
               for t in range(60)]
        def ret(nbars):
            return round((idx[-1] / idx[-1 - nbars] - 1) * 100, 2) if len(idx) > nbars else None
        w1 = ret(1); m1 = ret(4); m3 = ret(13)
        d1 = round(sum(d1s) / sum(d1w), 2) if d1w else None
        ma10 = _sma(idx, 10); ma10_prev = _sma(idx[:-4], 10)
        above = ma10 is not None and idx[-1] > ma10
        rising = ma10 is not None and ma10_prev is not None and ma10 > ma10_prev
        out.append({"industry": ind, "n": len(series), "mcap_B": round(W / 1e9, 1),
                    "d1": d1, "w1": w1, "m1": m1, "m3": m3, "above": above, "rising": rising})
    out.sort(key=lambda r: (r["d1"] if r["d1"] is not None else -999), reverse=True)
    return out


def compute_breakout_levels(closes, vols, hi, ma10):
    """
    Real price levels for a breakout pick's thesis. PURE.
    Returns {last, resistance, support, base_high, swing_low, ma50, atr_pct}.
    - base_high / resistance: the level being broken (recent high the stock is clearing)
    - support / swing_low: most recent meaningful pullback low
    - ma50: dynamic support (10-week / ~50-day MA)
    - atr_pct: rough weekly volatility as % of price (for stop sizing)
    """
    if not closes:
        return None
    last = closes[-1]
    # base high: highest close in the ~26 weeks before the last 2 (the level being cleared)
    lookback = closes[-27:-1] if len(closes) >= 27 else closes[:-1] or closes
    base_high = max(lookback) if lookback else last
    # swing low: lowest close in the last ~8 weeks (recent pullback support)
    recent = closes[-8:] if len(closes) >= 8 else closes
    swing_low = min(recent)
    # rough weekly ATR% from last ~10 weeks of |week-over-week| moves
    diffs = [abs(closes[i] - closes[i-1]) for i in range(max(1, len(closes)-10), len(closes))]
    atr = (sum(diffs) / len(diffs)) if diffs else 0
    atr_pct = round(atr / last * 100, 1) if last else 0
    return {
        "last": round(last, 2),
        "resistance": round(base_high, 2),
        "support": round(swing_low, 2),
        "ma50": round(ma10, 2) if ma10 else None,
        "atr_pct": atr_pct,
    }


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

def stock_engine(stock_data, universe, fundamentals=None, daily_ret=None):
    daily_ret = daily_ret or {}
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
            # price levels for the thesis — only computed for breakout picks (keeps file light)
            "levels": compute_breakout_levels(closes, vols, hi, ma10) if breakout else None,
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
        # daily % change (from the 2-day daily pull); null if unavailable
        c["daily_pct"] = daily_ret.get(c["ticker"])
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


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))

def compute_regime_inputs(macro_weekly):
    """
    Compute the R.* regime input variables from weekly macro history.
    macro_weekly: list of dicts sorted oldest->newest, each with keys:
      date, spx, vix, wti, cpi_yoy, us02 (2Y yield), real10 (real 10Y), hy (HY spread bp),
      dxy, gold
    Lookbacks: w4=4wk, w13=13wk(~3m), w52=52wk(~12m), w104=104wk(~2yr).
    Returns a dict of the derived variables used by detect_regime.
    """
    m = macro_weekly
    n = len(m)
    def val(key, i):
        try: return float(m[i].get(key)) if m[i].get(key) is not None else None
        except: return None
    def ago(key, weeks):
        idx = n - 1 - weeks
        return val(key, idx) if idx >= 0 else None
    def cur(key):
        return val(key, n - 1)
    def pct(now, then):
        if now is None or then is None or then == 0: return None
        return (now / then - 1) * 100
    def avg(key, weeks):
        vals = [val(key, i) for i in range(max(0, n - weeks), n)]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    spx = cur("spx"); vix = cur("vix"); wti = cur("wti"); hy = cur("hy")
    cpi = cur("cpi_yoy")

    # WTI momentum
    wti_12m = pct(wti, ago("wti", 52))
    wti_3m = pct(wti, ago("wti", 13))
    wti_2yr_avg = avg("wti", 104)
    wti_vs_2yr = pct(wti, wti_2yr_avg) if wti_2yr_avg else None

    # CPI
    cpi_yoy = cpi
    cpi_3ago = ago("cpi_yoy", 13)
    cpi_chg_3m = (cpi - cpi_3ago) if (cpi is not None and cpi_3ago is not None) else None

    # SPX momentum + drawdowns
    spx_3m = pct(spx, ago("spx", 13))
    spx_1m = pct(spx, ago("spx", 4))
    # dd = SPX vs 13-week high
    win13 = [val("spx", i) for i in range(max(0, n - 13), n)]; win13 = [v for v in win13 if v is not None]
    hi13 = max(win13) if win13 else None
    dd = pct(spx, hi13) if hi13 else None
    # trail_dd = SPX vs 26-week high
    win26 = [val("spx", i) for i in range(max(0, n - 26), n)]; win26 = [v for v in win26 if v is not None]
    hi26 = max(win26) if win26 else None
    trail_dd = pct(spx, hi26) if hi26 else None
    # spx_off_low = SPX vs 4-week low
    win4 = [val("spx", i) for i in range(max(0, n - 4), n)]; win4 = [v for v in win4 if v is not None]
    lo4 = min(win4) if win4 else None
    spx_off_low = pct(spx, lo4) if lo4 else None
    # fresh_high = TRUE if 4-week high ~= 26-week high (dip-from-highs guard)
    hi4 = max(win4) if win4 else None
    fresh_high = (hi4 is not None and hi26 is not None and hi4 >= hi26 * 0.995)

    # Rates (basis points over 13wk)
    us02_now = cur("us02"); us02_3ago = ago("us02", 13)
    us02_3m = (us02_now - us02_3ago) * 100 if (us02_now is not None and us02_3ago is not None) else None
    real_now = cur("real10"); real_3ago = ago("real10", 13)
    real_3m = (real_now - real_3ago) * 100 if (real_now is not None and real_3ago is not None) else None

    # VIX
    vix_13ago = ago("vix", 13)
    vix_3m_chg = pct(vix, vix_13ago)  # % change

    return {
        "wti_12m": wti_12m, "wti_3m": wti_3m, "wti_vs_2yr": wti_vs_2yr,
        "cpi_yoy": cpi_yoy, "cpi_chg_3m": cpi_chg_3m,
        "spx_3m": spx_3m, "spx_1m": spx_1m,
        "dd": dd, "trail_dd": trail_dd, "spx_off_low": spx_off_low, "fresh_high": fresh_high,
        "us02_3m": us02_3m, "real_3m": real_3m,
        "vix": vix, "vix_3m_chg": vix_3m_chg, "hy": hy,
    }

def detect_regime(R):
    """
    Exact port of detectRegime from layer1_v9.jsx. Scores each regime; highest wins.
    R: the dict from compute_regime_inputs. Returns {regime, confidence, scores, secondary_tag}.
    Missing inputs are treated as failing the gate (conservative).
    """
    def g(k, default=None):
        v = R.get(k)
        return v if v is not None else default

    scores = {}
    tag = None

    # ENERGY_GRIND: wti_12m>25 AND wti_vs_2yr>0 AND cpi_chg_3m>0.3
    if g("wti_12m", -1) > 25 and g("wti_vs_2yr", -1) > 0 and g("cpi_chg_3m", -1) > 0.3:
        scores["ENERGY_GRIND"] = 30 + 40*_clamp((R["wti_12m"]-25)/50) + 30*_clamp(R["cpi_chg_3m"]/2)

    # ENERGY_SPIKE: wti_3m>25 AND vix_3m_chg>15 AND cpi_yoy>3 AND wti_vs_2yr>0
    if g("wti_3m", -1) > 25 and g("vix_3m_chg", -1) > 15 and g("cpi_yoy", -1) > 3 and g("wti_vs_2yr", -1) > 0:
        scores["ENERGY_SPIKE"] = 40 + 30*_clamp((R["wti_3m"]-25)/40) + 30*_clamp(R["vix_3m_chg"]/60)

    # POLICY_TIGHTENING: dd>-12 AND (us02_3m>40 OR (real_3m>25 AND cpi_yoy>2.5))
    if g("dd", -99) > -12 and (g("us02_3m", -1) > 40 or (g("real_3m", -1) > 25 and g("cpi_yoy", -1) > 2.5)):
        scores["POLICY_TIGHTENING"] = 30 + 50*_clamp((g("us02_3m",0)-40)/80) + 20*_clamp((g("cpi_yoy",2)-2)/4)
        tag = "INFLATIONARY" if g("cpi_yoy", 0) > 3 else "NON-INFLATIONARY"

    # CRISIS_PEAK: dd<-15 AND vix>35
    if g("dd", 0) < -15 and g("vix", 0) > 35:
        scores["CRISIS_PEAK"] = 40 + 30*_clamp((-R["dd"]-15)/40) + 30*_clamp((R["vix"]-35)/45)

    # RECOVERY_EARLY: spx_off_low>5 AND dd<-12 AND vix>22 AND vix_3m_chg<0
    if g("spx_off_low", -1) > 5 and g("dd", 0) < -12 and g("vix", 0) > 22 and g("vix_3m_chg", 1) < 0:
        scores["RECOVERY_EARLY"] = 40 + 30*_clamp((R["spx_off_low"]-5)/20) + 30*_clamp((-R["dd"]-12)/20)

    # RECOVERY_LATE: spx_3m>5 AND trail_dd>-12 AND trail_dd<-2 AND vix<22 AND hy<450 AND NOT fresh_high
    if (g("spx_3m", -1) > 5 and g("trail_dd", -99) > -12 and g("trail_dd", 0) < -2
            and g("vix", 99) < 22 and g("hy", 999) < 450 and not R.get("fresh_high", False)):
        scores["RECOVERY_LATE"] = 30 + 40*_clamp((R["spx_3m"]-5)/15) + 30*_clamp((22-R["vix"])/10)

    # GOLDILOCKS: vix<16 AND dd>-5 AND spx_3m>2 AND spx_1m>-2 AND cpi_yoy<3 AND |cpi_chg_3m|<0.5 AND wti_3m<20 AND hy<400
    if (g("vix", 99) < 16 and g("dd", -99) > -5 and g("spx_3m", -99) > 2 and g("spx_1m", -99) > -2
            and g("cpi_yoy", 99) < 3 and abs(g("cpi_chg_3m", 99)) < 0.5 and g("wti_3m", 99) < 20 and g("hy", 999) < 400):
        scores["GOLDILOCKS"] = 40 + 30*_clamp((16-R["vix"])/8) + 30*_clamp(R["spx_3m"]/10)

    if scores:
        regime = max(scores, key=scores.get)
        confidence = round(scores[regime], 1)
    else:
        regime = "NO_CLEAR"
        confidence = 0.0

    return {
        "regime": regime,
        "confidence": confidence,
        "scores": {k: round(v, 1) for k, v in scores.items()},
        "secondary_tag": tag if regime == "POLICY_TIGHTENING" else None,
    }

def macro_engine(macro_weekly):
    """
    PURE FUNCTION. The Layer-1 regime engine. Input weekly macro history -> regime call.
    Exact port of the layer1_v9 detectRegime logic.
    Returns {asof, regime, confidence, scores, inputs, secondary_tag}.
    """
    if not macro_weekly or len(macro_weekly) < 14:
        return {"asof": _now(), "regime": "UNKNOWN", "confidence": 0,
                "error": "insufficient macro history (need 14+ weeks)"}
    R = compute_regime_inputs(macro_weekly)
    det = detect_regime(R)
    return {
        "asof": _now(),
        "regime": det["regime"],
        "confidence": det["confidence"],
        "scores": det["scores"],
        "secondary_tag": det["secondary_tag"],
        "inputs": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in R.items()},
    }



# ============================================================
# MACRO AUTO-COLLECT — pulls FRED + indices fresh each run (light, reliable).
# FRED never throttles; only ~5 Yahoo index tickers. This makes macro fully
# automatic in the Action — no manual Colab needed. Falls back to committed
# macro_weekly.csv if the live pull has a problem.
# ============================================================
def fetch_macro_weekly_live(start="2024-01-01"):
    """Build weekly macro history live from FRED + Yahoo. Returns (rows, degraded, note)."""
    import requests
    try:
        import yfinance as yf
    except Exception as e:
        return [], True, f"yfinance unavailable: {e}"

    def fred(series_id, freq="w"):
        fp = f"&frequency={freq}" if freq else ""
        u = (f"https://api.stlouisfed.org/fred/series/observations?series_id={series_id}"
             f"&api_key={FRED_API_KEY}&file_type=json&observation_start={start}{fp}&sort_order=asc")
        try:
            obs = requests.get(u, timeout=30).json().get("observations", [])
            return {o["date"]: (float(o["value"]) if o["value"] not in (".", "") else None) for o in obs}
        except Exception:
            return {}

    if not FRED_API_KEY:
        return [], True, "FRED_API_KEY not set"

    us02 = fred("DGS2"); real10 = fred("DFII10"); hy_raw = fred("BAMLH0A0HYM2")
    cpi_level = fred("CPIAUCSL", freq="")  # monthly

    def _dstr(d):
        try: return d.strftime("%Y-%m-%d")
        except AttributeError: return str(d)[:10]

    def yf_weekly(sym):
        try:
            df = yf.download(sym, start=start, interval="1wk", auto_adjust=True, progress=False)
            if df is None or len(df) == 0: return {}
            close = df["Close"]
            if hasattr(close, "columns"): close = close.iloc[:, 0]
            out = {}
            for d, v in close.dropna().items():
                try: out[_dstr(d)] = float(v)
                except (ValueError, TypeError): continue
            return out
        except Exception:
            return {}

    spx = yf_weekly("^GSPC"); vix = yf_weekly("^VIX"); wti = yf_weekly("CL=F")
    gold = yf_weekly("GC=F"); dxy = yf_weekly("DX-Y.NYB")
    # extra assets for the Markets asset band (weekly, same cadence as the rest)
    ndx = yf_weekly("^IXIC")     # Nasdaq Composite
    dow = yf_weekly("^DJI")      # Dow Jones Industrial Average
    rut = yf_weekly("^RUT")      # Russell 2000
    tnx = yf_weekly("^TNX")      # US 10Y yield (index, x10 = pct)
    btc = yf_weekly("BTC-USD")   # Bitcoin

    if not spx:
        return [], True, "SPX weekly pull empty (Yahoo issue)"

    # CPI YoY from monthly index
    cpi_dates = sorted(cpi_level)
    cpi_yoy = {}
    for i, d in enumerate(cpi_dates):
        j = max(0, i - 12)
        old = cpi_level.get(cpi_dates[j]) if cpi_dates else None
        if cpi_level.get(d) and old:
            cpi_yoy[d] = round((cpi_level[d] / old - 1) * 100, 2)

    def nearest(dct, target):
        keys = [k for k in dct if k <= target]
        return dct[max(keys)] if keys else None

    rows = []
    for d in sorted(spx):
        hy_v = nearest(hy_raw, d)
        rows.append({
            "date": d, "spx": spx.get(d), "vix": nearest(vix, d), "wti": nearest(wti, d),
            "cpi_yoy": nearest(cpi_yoy, d), "us02": nearest(us02, d), "real10": nearest(real10, d),
            "hy": round(hy_v * 100, 1) if hy_v is not None else None,
            "dxy": nearest(dxy, d), "gold": nearest(gold, d),
            "ndx": nearest(ndx, d), "dow": nearest(dow, d), "rut": nearest(rut, d),
            "tnx": nearest(tnx, d), "btc": nearest(btc, d),
        })
    return rows, False, ""


# ============================================================
# MACRO DATA + RUN — feeds the regime engine (FRED + indices)
# Reliable: FRED doesn't throttle like Yahoo's options endpoint.
# ============================================================
def load_macro_weekly_from_csv(path="macro_weekly.csv"):
    """Load weekly macro history: date,spx,vix,wti,cpi_yoy,us02,real10,hy,dxy,gold"""
    import csv, os
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "date": r["date"],
                    "spx": float(r["spx"]) if r.get("spx") else None,
                    "vix": float(r["vix"]) if r.get("vix") else None,
                    "wti": float(r["wti"]) if r.get("wti") else None,
                    "cpi_yoy": float(r["cpi_yoy"]) if r.get("cpi_yoy") else None,
                    "us02": float(r["us02"]) if r.get("us02") else None,
                    "real10": float(r["real10"]) if r.get("real10") else None,
                    "hy": float(r["hy"]) if r.get("hy") else None,
                    "dxy": float(r["dxy"]) if r.get("dxy") else None,
                    "gold": float(r["gold"]) if r.get("gold") else None,
                })
            except Exception:
                continue
    rows.sort(key=lambda x: x["date"])
    return rows

def run_macro(auto_pull=True):
    """
    Run the regime engine, write macro.json.
    auto_pull=True: pull FRED + indices fresh (light + reliable). This keeps VIX/rates/
    credit current daily for briefs and analysis, even though the regime moves slowly.
    Falls back to committed macro_weekly.csv if the live pull fails.
    """
    macro_weekly, note = [], ""
    if auto_pull:
        print("[macro] pulling fresh FRED + indices (light, reliable)...")
        macro_weekly, degraded, note = fetch_macro_weekly_live()
        if degraded or not macro_weekly:
            print(f"[macro] live pull issue ({note or 'empty'}); falling back to committed CSV")
            macro_weekly = []
        else:
            print(f"[macro] live pull OK: {len(macro_weekly)} weeks")
            # also write the CSV so we have a committed snapshot / fallback
            try:
                import csv as _csv
                with open("macro_weekly.csv", "w", newline="") as f:
                    w = _csv.DictWriter(f, fieldnames=["date","spx","vix","wti","cpi_yoy","us02","real10","hy","dxy","gold"])
                    w.writeheader()
                    for r in macro_weekly: w.writerow(r)
            except Exception:
                pass
    if not macro_weekly:
        macro_weekly = load_macro_weekly_from_csv()
    if not macro_weekly:
        print("[macro] SKIPPED — no live data and no macro_weekly.csv")
        return None
    print(f"[macro] {len(macro_weekly)} weeks of history, latest {macro_weekly[-1]['date']}")
    result = macro_engine(macro_weekly)
    write_json("macro", result)

    # Also emit the weekly time-series the charts need. The engine already has
    # this in memory (macro_weekly); we were discarding it after computing the
    # scalar inputs. Trim to ~2yr (104 weeks) to keep the file light for the
    # dashboard fetch, and only keep fields the charts actually plot.
    try:
        _series_keep = ("date", "spx", "vix", "wti", "gold", "dxy",
                        "ndx", "dow", "rut", "tnx", "btc",
                        "cpi_yoy", "us02", "real10", "hy")
        _trimmed = [{k: r.get(k) for k in _series_keep} for r in macro_weekly[-104:]]
        write_json("macro_series", {
            "asof": result.get("asof"),
            "weeks": len(_trimmed),
            "series": _trimmed,
        })
        print(f"[macro] wrote outputs/macro_series.json ({len(_trimmed)} weeks)")
    except Exception as _e:
        print(f"[macro] macro_series.json skipped: {_e}")
    if result.get("error"):
        print(f"[macro] {result['error']}")
    else:
        print(f"[macro] REGIME: {result['regime']} (confidence {result['confidence']})")
        if result.get("secondary_tag"):
            print(f"[macro]   tag: {result['secondary_tag']}")
        if result.get("scores"):
            print(f"[macro]   scores: {result['scores']}")
    print("[macro] wrote outputs/macro.json")
    return result


# ============================================================
# SPX DAILY OHLCV — for the Markets candlestick tile.
# Single ticker (^GSPC), ~1yr of daily bars. One symbol = negligible
# throttle risk. Writes outputs/spx_daily.json. Non-fatal if it fails —
# the tile just shows an empty state until the next good run.
# ============================================================
def run_spx_daily(period="1y"):
    """Pull ~1yr of SPX daily OHLC+volume and write spx_daily.json."""
    try:
        import yfinance as yf
    except Exception as e:
        print(f"[spx_daily] yfinance unavailable: {e}")
        return None
    try:
        df = yf.download("^GSPC", period=period, interval="1d",
                         auto_adjust=False, progress=False)
        if df is None or len(df) == 0:
            print("[spx_daily] empty pull (Yahoo issue); skipping")
            return None
        # flatten possible multiindex columns
        def col(name):
            c = df[name]
            if hasattr(c, "columns"):
                c = c.iloc[:, 0]
            return c
        o, h, l, cl, v = (col("Open"), col("High"), col("Low"),
                          col("Close"), col("Volume"))
        bars = []
        for d in df.index:
            def g(series):
                try:
                    val = float(series.loc[d]); return round(val, 2)
                except Exception:
                    return None
            def gd(dt):
                try: return dt.strftime("%Y-%m-%d")
                except AttributeError: return str(dt)[:10]
            ov, hv, lv, cv = g(o), g(h), g(l), g(cl)
            try: vv = int(float(v.loc[d]))
            except Exception: vv = None
            if cv is None:
                continue
            bars.append({"date": gd(d), "o": ov, "h": hv, "l": lv, "c": cv, "v": vv})
        write_json("spx_daily", {"symbol": "SPX", "bars": bars,
                                 "asof": bars[-1]["date"] if bars else None})
        print(f"[spx_daily] wrote outputs/spx_daily.json ({len(bars)} daily bars)")
        return bars
    except Exception as e:
        print(f"[spx_daily] failed: {e}")
        return None


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
    if result.get("error") == "degenerate_chain":
        d = result.get("diagnostics", {})
        print(f"[gex] BAD PULL: only {d.get('n_strikes')} strikes, {d.get('total_oi')} OI "
              f"— Yahoo throttled the options chain. Flagged INVALID in gex.json.")
    elif result.get("error"):
        print(f"[gex] ERROR: {result['error']}")
    else:
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


def fetch_daily_bars_yfinance(tickers, days=2):
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
            df = yf.download(batch, period=f"{max(days,2)}d", interval="1d",
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

    auto_pull: DEFAULT TRUE, but pulls ONLY the last 2 days (never the 2yr history).
               The 2yr history lives in committed stock_weekly.csv (uploaded once).
               Each run: pull last 2 days -> merge onto history in memory -> score.
               If Yahoo throttles the 2-day pull, it falls back to committed
               daily_recent.csv, or scores on the committed history as-is. NEVER
               pulls the full history — that stays static in the repo.
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
            print(f"[stocks] pulling ONLY last 2 days for {len(tickers)} tickers (not the 2yr history)...")
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
        daily = load_weekly_from_csv("daily_recent.csv")  # same 3-col format (ticker,date,close,volume)
        if daily:
            # report the date range of the gap file so you can see what's being merged
            all_dates = sorted({d for bars in daily.values() for (d, c, v) in bars})
            rng = f"{all_dates[0]} -> {all_dates[-1]}" if all_dates else "?"
            print(f"[stocks] using committed daily_recent.csv ({len(daily)} tickers, {rng})")
        else:
            print("[stocks] no daily_recent.csv — scoring on stock_weekly.csv history as-is")
            print("[stocks]   (to add recent days: run the gap Colab, commit daily_recent.csv)")

    # --- compute a clean 1-day return per ticker from the 2-day daily pull ---
    # daily[tk] = [(date, close, vol), ...] with the last ~2 sessions. Last two
    # closes give a real daily % change for the top-performers tile.
    daily_ret = {}
    if daily:
        for tk, bars in daily.items():
            try:
                cs = [c for (_d, c, _v) in bars if c is not None]
                if len(cs) >= 2 and cs[-2]:
                    daily_ret[tk] = round((cs[-1] / cs[-2] - 1) * 100, 2)
            except Exception:
                continue

    # --- merge newest day onto static history, then score ---
    if daily:
        weekly = _merge_daily_into_weekly(weekly, daily)
        print(f"[stocks] merged newest bars onto history")
    else:
        note = (note + "; " if note else "") + "no fresh daily data — scoring on committed history as-is"
        print(f"[stocks] {note}")

    print(f"[stocks] scoring {len(weekly)} tickers ({len(fundamentals)} with fundamentals)")
    result = stock_engine(weekly, universe, fundamentals, daily_ret=daily_ret)
    if note:
        result["meta"]["data_note"] = note
    write_json("stocks", result)
    m = result["meta"]
    print(f"[stocks] {m['gate_passers']} passers, {m['breakouts']} breakouts, "
          f"{m['industries_passing']} industries passing")
    print("[stocks] wrote outputs/stocks.json")

    # Industry performance (cap-weighted, 4 timeframes) for the Screener industry tile.
    try:
        ind_perf = compute_industry_performance(weekly, universe, daily_ret=daily_ret)
        write_json("industry", {"asof": result.get("asof"), "count": len(ind_perf),
                                "industries": ind_perf})
        print(f"[stocks] wrote outputs/industry.json ({len(ind_perf)} industries)")
    except Exception as e:
        print(f"[stocks] industry.json skipped: {e}")

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
    # Layer 1: macro regime (runs on reliable FRED data, first in the funnel)
    try:
        run_macro()
    except Exception as e:
        warnings.append(f"macro failed: {e}")
        print(f"[macro] FAILED (non-fatal): {e}")
    # SPX daily OHLCV for the Markets candlestick tile (single ticker, non-fatal)
    try:
        run_spx_daily()
    except Exception as e:
        warnings.append(f"spx_daily failed: {e}")
        print(f"[spx_daily] FAILED (non-fatal): {e}")
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
    elif name == "macro":
        run_macro()
    elif name == "spx_daily":
        run_spx_daily()
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
