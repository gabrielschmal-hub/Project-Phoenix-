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


def _isnan(v):
    """
    True if v is NaN/None/non-numeric.

    NOTE: this was CALLED in several places but never defined — every call raised
    NameError, which the surrounding try/except swallowed. In the old per-field
    code that just nulled a field; in the bar parser it would silently drop EVERY
    bar and leave charts empty. Defining it properly is load-bearing.
    """
    if v is None:
        return True
    try:
        f = float(v)
    except (TypeError, ValueError):
        return True
    return f != f   # NaN is the only value not equal to itself


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
        # Count gates passed. Full passers clear all 5; near-misses clear exactly 4.
        gate_flags = {"trend200": g1, "trend50": g2, "industry": g3, "near_high": g5, "stage2": g6}
        gates_passed = sum(1 for v in gate_flags.values() if v)
        is_passer = gates_passed == 5
        is_near = gates_passed == 4
        if not (is_passer or is_near):
            continue
        missing_gate = None if is_passer else [k for k, v in gate_flags.items() if not v][0]

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
        breakout = (is_passer and at_high and surge >= STOCK["breakout_vol_surge_pct"]
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
            "ticker": tk, "name": universe[tk].get("name", ""),
            "industry": ind, "mcap_B": round(mc / 1e9, 2),
            "surge": round(surge), "vol_state": vstate, "breakout": breakout,
            "passer": is_passer, "gates_passed": gates_passed, "missing_gate": missing_gate,
            "pos_vs_high": round(pos_vs_high, 1), "industry_mom_3m": ind_scores.get(ind, {}).get("momentum_3m", 0),
            "_ret4": ret4, "_ret12": ret12, "_ret52": ret52, "_ext": ext, "_weeks_in": weeks_in,
            "_fund": fundamentals.get(tk),
            # Price levels for EVERY candidate — the plan panel needs entry/support/
            # resistance for anything you click, not just breakouts. ~5 floats each;
            # at ~550 candidates that's negligible file weight.
            "levels": compute_breakout_levels(closes, vols, hi, ma10),
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
        # opportunity score: blend of trade + invest (rebalanced after your call).
        # full passers get a small edge over near-misses so they rank first when scores tie.
        c["opp_score"] = round((t * 0.5 + i * 0.5) + (2 if c.get("passer") else 0), 1)
        # daily % change (from the 2-day daily pull); null if unavailable
        c["daily_pct"] = daily_ret.get(c["ticker"])
        # clean up internal fields
        for k in list(c.keys()):
            if k.startswith("_"):
                del c[k]

    # rank by opportunity score (best = rank 1)
    ranked = sorted(candidates, key=lambda c: -c["opp_score"])
    for idx, c in enumerate(ranked):
        c["rank"] = idx + 1

    breakouts = [c for c in candidates if c["breakout"]]
    passers = [c for c in candidates if c.get("passer")]
    near = [c for c in candidates if not c.get("passer")]
    return {
        "asof": _now(),
        "meta": {
            "gate_passers": len(passers),
            "near_misses": len(near),
            "total": len(candidates),
            "breakouts": len(breakouts),
            "industries_passing": len(passing),
        },
        "stocks": ranked,
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
# ============================================================
# RESEARCH DATA — rich per-ticker pull for the Research product pages.
# Only runs for watchlist tickers (research.json + trades.json), so the
# heavy per-ticker yfinance calls stay bounded. Writes research_data.json.
# Everything is best-effort: any field that fails is null, never fabricated.
# ============================================================
# Per-ticker fundamentals (info/financials/ratings) require per-ticker Yahoo calls,
# which throttle hard. Bound them to the best names. Charts are NOT bounded —
# they come from the bulk endpoint, so every candidate gets one.
RESEARCH_FUND_TOP_N = 150

# How much daily history the single pull fetches. This is ALSO the chart depth.
# "1y" costs the same HTTP calls as "2d" — yfinance returns a date range per
# batch — so there is no reason to ask for less.
CHART_PERIOD = "1y"


def _pinned_tickers():
    """Tickers you've explicitly committed (watchlist / trades log). Always included."""
    import json, os
    pinned = set()
    for name in ("research", "trades"):
        path = os.path.join(OUTPUTS_DIR, f"{name}.json")
        if os.path.exists(path):
            try:
                d = json.load(open(path))
                for row in d.get("tickers", []):
                    if row.get("ticker"):
                        pinned.add(row["ticker"])
            except Exception:
                continue
    tl = os.path.join(OUTPUTS_DIR, "trades_log.json")
    if os.path.exists(tl):
        try:
            d = json.load(open(tl))
            for row in d.get("trades", []):
                if row.get("ticker"):
                    pinned.add(row["ticker"])
        except Exception:
            pass
    return pinned


def _ranked_candidates():
    """
    Every screener candidate, PROPERLY ranked: breakouts first, then by
    opportunity score descending.

    (The previous version used raw file order and called it 'byrank', then
    truncated a set at 120 — so the chosen names were effectively alphabetical.
    This returns a real, ordered list.)
    """
    import json, os
    path = os.path.join(OUTPUTS_DIR, "stocks.json")
    if not os.path.exists(path):
        return []
    try:
        d = json.load(open(path))
    except Exception:
        return []
    stocks = [s for s in d.get("stocks", []) if s.get("ticker")]

    def score(s):
        return s.get("opp_score") or max(s.get("trade_score") or 0,
                                         s.get("invest_score") or 0)

    breakouts = sorted([s for s in stocks if s.get("breakout")], key=score, reverse=True)
    rest = sorted([s for s in stocks if not s.get("breakout")], key=score, reverse=True)
    ordered, seen = [], set()
    for s in breakouts + rest:
        t = s["ticker"]
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def _watchlist_tickers():
    """Full chart universe: every candidate + anything pinned."""
    pinned = _pinned_tickers()
    ranked = _ranked_candidates()
    out = list(ranked)
    for t in sorted(pinned):
        if t not in out:
            out.append(t)
    return out


def _resample(daily_ohlcv, rule):
    """
    Build weekly/monthly candles from daily bars. NO NETWORK — pure aggregation.
    rule: "W" (week starting Monday) or "M" (calendar month).
    daily_ohlcv: [(date, o, h, l, c, v), ...] ascending.
    Returns [{date,o,h,l,c,v}, ...] where each candle is a true OHLC roll-up:
      open = first open of the period, high = max high, low = min low,
      close = last close, volume = sum.
    """
    from datetime import datetime, timedelta
    from collections import OrderedDict
    buckets = OrderedDict()
    for (ds, o, h, l, c, v) in daily_ohlcv:
        try:
            dt = datetime.strptime(ds, "%Y-%m-%d")
        except Exception:
            continue
        if rule == "W":
            key = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
        else:
            key = dt.strftime("%Y-%m-01")
        b = buckets.get(key)
        if b is None:
            buckets[key] = {"date": key, "o": o, "h": h, "l": l, "c": c, "v": v or 0}
        else:
            b["h"] = max(b["h"], h)
            b["l"] = min(b["l"], l)
            b["c"] = c
            b["v"] = (b["v"] or 0) + (v or 0)
    out = []
    for k in sorted(buckets):
        b = buckets[k]
        out.append({"date": b["date"], "o": round(b["o"], 2), "h": round(b["h"], 2),
                    "l": round(b["l"], 2), "c": round(b["c"], 2),
                    "v": int(b["v"]) if b["v"] else None})
    return out


def _weekly_csv_to_bars(rows):
    """
    Turn committed stock_weekly.csv rows into candles for deep history.
    That file has close+volume ONLY — no open/high/low. We do NOT invent them:
    o=h=l=c makes a flat bar, which renders as a tick. Honest, not fabricated.
    rows: [(date, close, vol), ...]
    """
    out = []
    for (ds, c, v) in rows:
        out.append({"date": ds, "o": round(c, 2), "h": round(c, 2),
                    "l": round(c, 2), "c": round(c, 2),
                    "v": int(v) if v else None})
    return out


def _compact(bars):
    """
    Parallel-array encoding for OHLCV. ~40% smaller than a list of dicts.
    {"d":[dates], "o":[opens], "h":[highs], "l":[lows], "c":[closes], "v":[vols]}
    The frontend rehydrates this back into bar objects.
    """
    if not bars:
        return {"d": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    return {
        "d": [b["date"] for b in bars],
        "o": [b["o"] for b in bars],
        "h": [b["h"] for b in bars],
        "l": [b["l"] for b in bars],
        "c": [b["c"] for b in bars],
        "v": [b["v"] for b in bars],
    }


def write_charts(daily_ohlcv, weekly_csv=None, tickers=None, quarterly=None, universe=None):
    """
    Write outputs/charts/TK.json from bars we ALREADY have. ZERO network calls.

    daily_ohlcv: {ticker: [(date,o,h,l,c,v), ...]} straight from the pull
                 run_stocks already does.
    weekly_csv:  {ticker: [(date,close,vol), ...]} committed 2yr history, used
                 to extend weekly/monthly beyond the 1y of dailies. Close-only,
                 so those older bars are flat (o=h=l=c) — we don't fake OHLC.

    Daily  = the pulled bars (true candles).
    Weekly = resampled from dailies (true candles), back-extended with CSV.
    Monthly= resampled from dailies + CSV.
    """
    import os, json
    charts_dir = os.path.join(OUTPUTS_DIR, "charts")
    os.makedirs(charts_dir, exist_ok=True)

    tickers = tickers or list(daily_ohlcv.keys())
    written, skipped = 0, 0
    for tk in tickers:
        d = daily_ohlcv.get(tk) or []
        wk_rows = (weekly_csv or {}).get(tk) or []
        if not d and not wk_rows:
            skipped += 1
            continue

        daily_bars = [{"date": ds, "o": round(o, 2), "h": round(h, 2), "l": round(l, 2),
                       "c": round(c, 2), "v": int(v) if v else None}
                      for (ds, o, h, l, c, v) in d]

        # weekly/monthly: true candles from dailies, back-extended with CSV closes
        wk = _resample(d, "W") if d else []
        mo = _resample(d, "M") if d else []
        if wk_rows:
            oldest_daily = daily_bars[0]["date"] if daily_bars else "9999-99-99"
            hist = [r for r in wk_rows if r[0] < oldest_daily]
            if hist:
                wk = _weekly_csv_to_bars(hist) + wk
                # monthly from the CSV history: take the last close of each month
                from collections import OrderedDict
                mb = OrderedDict()
                for (ds, c, v) in hist:
                    mb[ds[:7] + "-01"] = (ds, c, v)
                hist_mo = _weekly_csv_to_bars([v for v in mb.values()])
                for i, k in enumerate(mb):
                    hist_mo[i]["date"] = k
                mo = hist_mo + mo

        last = daily_bars[-1]["c"] if daily_bars else (wk[-1]["c"] if wk else None)
        prev = daily_bars[-2]["c"] if len(daily_bars) > 1 else None
        hi52 = max((b["h"] for b in daily_bars), default=None)
        lo52 = min((b["l"] for b in daily_bars), default=None)

        payload = {
            "ticker": tk, "asof": _now(),
            "quote": {"last": last, "prev": prev},
            "range52": {"low": lo52, "high": hi52},
            "chart": {"daily": _compact(daily_bars), "weekly": _compact(wk), "monthly": _compact(mo)},
        }
        # Fundamentals ride along in the same file — one fetch on click gives the
        # page its chart AND its financials. Sourced from the committed CSV, so
        # this costs nothing and doesn't depend on the optional research pass.
        qs = (quarterly or {}).get(tk)
        if qs:
            payload["financials"] = qs[-6:]
        u = (universe or {}).get(tk) or {}
        if u:
            payload["profile"] = {
                "name": u.get("name"), "sector": u.get("sector"),
                "industry": u.get("industry"),
            }
            mc = u.get("market_cap")
            if mc:
                payload["quote"]["mcap_B"] = round(mc / 1e9, 2)
                # trailing P/E from committed data: mcap / sum(last 4 quarters NI)
                nis = [q.get("net_income_B") for q in (qs or [])[-4:] if q.get("net_income_B") is not None]
                if len(nis) == 4 and sum(nis) > 0:
                    payload["pe"] = round(mc / 1e9 / sum(nis), 1)
        safe = tk.replace("/", "-").replace(".", "-")
        with open(os.path.join(charts_dir, f"{safe}.json"), "w") as f:
            json.dump(payload, f, separators=(",", ":"))
        written += 1

    print(f"[charts] wrote {written} chart files (0 extra network calls){f', {skipped} had no bars' if skipped else ''}")
    return written


def run_research(tickers=None):
    """
    OPTIONAL garnish: company summary, analyst ratings, earnings date.

    This pass no longer supplies charts OR financials — both come from committed
    data via write_charts(), with zero network. What's left here is only what
    the CSVs genuinely don't contain:
      - longBusinessSummary (company description)
      - analyst ratings breakdown + mean target
      - next earnings date

    Per-ticker Yahoo endpoints rate-limit hard, so this stays bounded and paced.
    If it throttles, is skipped, or fails outright, the dashboard is unaffected —
    charts, prices, financials, and margins all still render.
    """
    try:
        import yfinance as yf
    except Exception as e:
        print(f"[research] yfinance unavailable: {e}")
        return None

    if tickers is None:
        pinned = _pinned_tickers()
        ranked = _ranked_candidates()
        tickers = ranked[:RESEARCH_FUND_TOP_N]
        for t in sorted(pinned):
            if t not in tickers:
                tickers.append(t)
    if not tickers:
        print("[research] nothing to enrich; skipping")
        write_json("research_data", {"asof": _now(), "tickers": {}})
        return {}

    import time
    print(f"[research] fundamentals for {len(tickers)} names (optional — charts already done)")
    out = {}
    ok = 0
    for idx, tk in enumerate(tickers, 1):
        entry = {"profile": {}, "quote": {}, "range52": {}, "pe": None,
                 "financials": [], "ratings": None, "earnings": {}}
        try:
            t = yf.Ticker(tk)
            info = {}
            try:
                info = t.info or {}
            except Exception:
                info = {}
            if info:
                ok += 1
            entry["profile"] = {
                "name": info.get("longName") or info.get("shortName") or tk,
                "exchange": info.get("exchange"),
                "sector": info.get("sector"), "industry": info.get("industry"),
                "summary": info.get("longBusinessSummary"),
                "country": info.get("country"), "employees": info.get("fullTimeEmployees"),
                "website": info.get("website"),
                "forward_pe": info.get("forwardPE"),
                "div_yield": info.get("dividendYield"),
                "recommendation": info.get("recommendationKey"),
                "target_mean": info.get("targetMeanPrice"),
            }
            entry["quote"] = {
                "last": info.get("currentPrice") or info.get("regularMarketPrice"),
                "prev": info.get("previousClose") or info.get("regularMarketPreviousClose"),
                "mcap_B": round(info.get("marketCap") / 1e9, 2) if info.get("marketCap") else None,
                "volume": info.get("volume") or info.get("regularMarketVolume"),
            }
            entry["range52"] = {"low": info.get("fiftyTwoWeekLow"), "high": info.get("fiftyTwoWeekHigh")}
            entry["pe"] = info.get("trailingPE")

            # NOTE: financials deliberately NOT pulled here. The committed
            # macroflow_fundamentals_quarterly.csv has 16 metrics per quarter vs
            # the 2 this endpoint returns, covers more tickers, and costs no
            # network. write_charts() embeds it into each chart file.

            try:
                rec = t.recommendations
                if rec is not None and len(rec) > 0:
                    r = rec.iloc[0]
                    entry["ratings"] = {
                        "strong_buy": int(r.get("strongBuy", 0) or 0),
                        "buy": int(r.get("buy", 0) or 0),
                        "hold": int(r.get("hold", 0) or 0),
                        "sell": int(r.get("sell", 0) or 0),
                        "strong_sell": int(r.get("strongSell", 0) or 0),
                    }
            except Exception:
                pass

            try:
                cal = t.calendar
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if isinstance(ed, list) and ed:
                        ed = ed[0]
                    if ed is not None:
                        entry["earnings"]["next_date"] = ed.strftime("%Y-%m-%d") if hasattr(ed, "strftime") else str(ed)[:10]
            except Exception:
                pass

            out[tk] = entry
            if idx % 25 == 0:
                print(f"[research]   {idx}/{len(tickers)} ({ok} with info)")
            time.sleep(0.6)
        except Exception:
            out[tk] = entry
            continue

    write_json("research_data", {"asof": _now(), "tickers": out,
                                 "meta": {"pulled": len(out), "with_info": ok}})
    print(f"[research] done: {len(out)} enriched ({ok} with info)")
    return out


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
                    "name": row.get("name", "") or row.get("longName", ""),
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

def load_quarterly_fundamentals(path="macroflow_fundamentals_quarterly.csv"):
    """
    Load the committed quarterly fundamentals. NO NETWORK.

    This file is richer than anything the Yahoo per-ticker endpoint gave us
    (16 metrics vs 2) and covers ~78% of screener candidates instantly, versus
    ~150 names via throttled per-ticker calls.

    Returns {ticker: [quarter_dict, ...]} sorted oldest -> newest, with margins
    and growth derived per quarter.
    """
    import csv, os
    from collections import defaultdict
    if not os.path.exists(path):
        print(f"[fundamentals] {path} not found — financial tiles will be empty")
        return {}

    raw = defaultdict(list)
    with open(path) as f:
        for row in csv.DictReader(f):
            tk = row.get("ticker")
            if tk:
                raw[tk].append(row)

    def num(row, key):
        v = row.get(key)
        if v in (None, "", "None", "nan"):
            return None
        try:
            f = float(v)
            return None if f != f else f
        except (TypeError, ValueError):
            return None

    def pct(n, d):
        if n is None or not d:
            return None
        try:
            return round(n / d * 100, 1)
        except ZeroDivisionError:
            return None

    out = {}
    for tk, rows in raw.items():
        rows.sort(key=lambda r: r.get("quarter_end") or "")
        qs = []
        for r in rows:
            rev = num(r, "revenue")
            ni = num(r, "net_income")
            gp = num(r, "gross_profit")
            oi = num(r, "operating_income")
            eb = num(r, "ebitda")
            fcf = num(r, "free_cash_flow")
            ocf = num(r, "operating_cash_flow")
            eq = num(r, "total_equity")
            debt = num(r, "total_debt")
            ca = num(r, "current_assets")
            cl = num(r, "current_liabilities")
            qs.append({
                "q": (r.get("quarter_end") or "")[:10],
                "revenue_B": round(rev / 1e9, 3) if rev is not None else None,
                "net_income_B": round(ni / 1e9, 3) if ni is not None else None,
                "ebitda_B": round(eb / 1e9, 3) if eb is not None else None,
                "fcf_B": round(fcf / 1e9, 3) if fcf is not None else None,
                "ocf_B": round(ocf / 1e9, 3) if ocf is not None else None,
                "net_margin": pct(ni, rev),
                "gross_margin": pct(gp, rev),
                "op_margin": pct(oi, rev),
                "ebitda_margin": pct(eb, rev),
                "fcf_margin": pct(fcf, rev),
                "roe": pct(ni, eq),
                "debt_equity": round(debt / eq, 2) if (debt is not None and eq) else None,
                "current_ratio": round(ca / cl, 2) if (ca is not None and cl) else None,
                "_rev": rev,
            })
        # growth: QoQ from the previous quarter, YoY from 4 quarters back
        for idx, q in enumerate(qs):
            rev = q.pop("_rev", None)
            if rev is None:
                q["rev_qoq"] = q["rev_yoy"] = None
                continue
            prev = qs[idx - 1].get("revenue_B") if idx >= 1 else None
            q["rev_qoq"] = round((rev / 1e9 / prev - 1) * 100, 1) if prev else None
            yr = qs[idx - 4].get("revenue_B") if idx >= 4 else None
            q["rev_yoy"] = round((rev / 1e9 / yr - 1) * 100, 1) if yr else None
        out[tk] = qs

    nq = sum(len(v) for v in out.values())
    print(f"[fundamentals] loaded {len(out)} tickers, {nq} quarters from {path} (no network)")
    return out


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


def _pull_daily_batch(batch, period, out):
    """
    One yf.download call. Appends FULL OHLCV to `out`. Returns tickers that came back.

    out[ticker] = [(date, open, high, low, close, volume), ...]

    We ask for a year rather than 2 days because it costs the SAME number of
    HTTP calls, and it gives us the chart data for free. The screener only
    needs the last couple of bars and just slices them off the end.
    """
    import yfinance as yf
    ok = set()
    try:
        df = yf.download(batch, period=period, interval="1d",
                         group_by="ticker", auto_adjust=True, threads=True, progress=False)
        if df is None or len(df) == 0:
            return ok
        for t in batch:
            try:
                sub = df if len(batch) == 1 else df[t]
                sub = sub[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")
                if len(sub) == 0:
                    continue
                rows = []
                for dt, row in sub.iterrows():
                    try:
                        c = float(row["Close"])
                        if _isnan(c):
                            continue
                        o = float(row["Open"]);  o = c if _isnan(o) else o
                        h = float(row["High"]);  h = c if _isnan(h) else h
                        lo = float(row["Low"]);  lo = c if _isnan(lo) else lo
                        v = float(row["Volume"]); v = 0.0 if _isnan(v) else v
                        rows.append((dt.strftime("%Y-%m-%d"), round(o, 4), round(h, 4),
                                     round(lo, 4), round(c, 4), v))
                    except Exception:
                        continue
                if rows:
                    out[t] = rows
                    ok.add(t)
            except Exception:
                continue
    except Exception:
        pass
    return ok


def fetch_daily_bars_yfinance(tickers, period="1y"):
    """
    ONE pull that serves BOTH the screener and the charts.

    Returns {ticker: [(date, o, h, l, c, v), ...]} for `period` of daily bars.

    Asking for 1y instead of 2d costs the same number of HTTP calls — Yahoo
    returns a date range per batch either way. The screener slices the last 2
    bars off the end; the chart writer keeps the whole series. This is why
    there's no separate chart-fetching pass any more.

    PARTIAL SUCCESS IS SUCCESS. Yahoo routinely drops a slice of a large batch.
    The original code threw away the whole pull when coverage fell under 80%,
    which silently froze the screener on a stale snapshot. We retry the missing
    names in smaller batches and keep everything we get.

    Returns (bars_dict, unusable_flag, got, failed).
      unusable_flag is True only if coverage is genuinely broken (<25%).
    """
    import time
    out = {}
    total = len(tickers)
    if not total:
        return {}, True, 0, 0

    remaining = list(tickers)
    for attempt, chunk in enumerate((200, 80, 40), start=1):
        if not remaining:
            break
        if attempt > 1:
            print(f"[stocks]   retry pass {attempt}: {len(remaining)} missing, chunk={chunk}")
            time.sleep(2)
        got_this_pass = set()
        for i in range(0, len(remaining), chunk):
            got_this_pass |= _pull_daily_batch(remaining[i:i + chunk], period, out)
        remaining = [t for t in remaining if t not in out]
        if not got_this_pass:
            break

    got = len(out)
    failed = total - got
    cov = got / total if total else 0
    unusable = cov < 0.25
    return out, unusable, got, failed


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
    weekly_raw = weekly  # keep the unmerged CSV history for deep chart bars
    if not universe or not weekly:
        print("[stocks] SKIPPED — universe.csv / stock_weekly.csv not found in repo")
        print("[stocks]   (upload these ONCE to enable the stock engine)")
        return None

    fundamentals = load_fundamentals_from_csv()
    quarterly = load_quarterly_fundamentals()   # committed, no network

    # --- ONE pull serves both the screener AND the charts ---
    # We ask for 1y of daily OHLC instead of 2 days. Same endpoint, same number
    # of HTTP calls (yfinance returns a date range per batch either way), but now
    # the chart data falls out of the pull we were already paying for. No second
    # pass, no separate research fetch, no waiting for a later run.
    ohlcv = {}          # {tk: [(date,o,h,l,c,v), ...]}  — full year, for charts
    daily = {}          # {tk: [(date,close,vol), ...]}   — what the scorer expects
    note = ""
    coverage = None
    if auto_pull:
        try:
            tickers = list(universe.keys())
            print(f"[stocks] pulling 1y daily OHLC for {len(tickers)} tickers "
                  f"(feeds the screener AND every chart, one pull)...")
            ohlcv, unusable, got, failed = fetch_daily_bars_yfinance(tickers, period=CHART_PERIOD)
            coverage = round(got / len(tickers) * 100, 1) if tickers else 0
            print(f"[stocks] pull: {got} ok, {failed} failed ({coverage}% coverage)")
            if unusable:
                note = f"daily pull unusable ({got}/{len(tickers)}, {coverage}%) — Yahoo likely blocking"
                print(f"[stocks] WARNING: {note}; trying committed daily_recent.csv")
                ohlcv = {}
            elif coverage < 90:
                # PARTIAL IS FINE. Use it, say so honestly, do NOT throw it away.
                note = f"partial daily coverage: {got}/{len(tickers)} tickers ({coverage}%) refreshed"
                print(f"[stocks] {note} — using it (the rest score on stored history)")
        except Exception as e:
            note = f"auto-pull failed: {e}"
            print(f"[stocks] {note}; trying committed daily_recent.csv")

    # collapse OHLCV -> (date, close, vol) for the scoring engine, which only
    # needs closes and volume. The full OHLC stays in `ohlcv` for the charts.
    if ohlcv:
        daily = {tk: [(d, c, v) for (d, _o, _h, _l, c, v) in bars] for tk, bars in ohlcv.items()}

    if not daily:
        daily = load_weekly_from_csv("daily_recent.csv")  # (ticker,date,close,volume)
        if daily:
            all_dates = sorted({d for bars in daily.values() for (d, c, v) in bars})
            rng = f"{all_dates[0]} -> {all_dates[-1]}" if all_dates else "?"
            print(f"[stocks] using committed daily_recent.csv ({len(daily)} tickers, {rng})")
        else:
            print("[stocks] no daily_recent.csv — scoring on stock_weekly.csv history as-is")

    # --- 1-day return per ticker, from the last two closes ---
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
    # Freshness telemetry — so the dashboard can SHOW whether it's looking at
    # fresh data or a stale snapshot, instead of silently implying it's live.
    result["meta"]["daily_coverage_pct"] = coverage
    result["meta"]["tickers_refreshed"] = len(daily) if daily else 0
    result["meta"]["latest_bar"] = max(
        (d for bars in daily.values() for (d, _c, _v) in bars), default=None
    ) if daily else None
    result["meta"]["data_is_fresh"] = bool(daily)
    write_json("stocks", result)
    m = result["meta"]
    print(f"[stocks] {m['gate_passers']} passers, {m['breakouts']} breakouts, "
          f"{m['industries_passing']} industries passing")

    # --- charts, from the bars already in memory. No network. ---
    # Only the candidates that made stocks.json, plus anything you've pinned via
    # a committed trades_log.json — so we don't write 2,893 files for names the
    # dashboard will never open.
    if ohlcv:
        try:
            keep = {c["ticker"] for c in result.get("stocks", [])}
            keep |= _pinned_tickers()
            keep &= set(ohlcv.keys())
            write_charts(ohlcv, weekly_csv=weekly_raw, tickers=sorted(keep),
                         quarterly=quarterly, universe=universe)
        except Exception as e:
            print(f"[charts] FAILED (non-fatal): {e}")
    else:
        print("[charts] skipped — no fresh OHLC this run (charts keep last run's files)")
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
    # Stocks: ONE pull -> the screener AND every ticker chart (outputs/charts/).
    # Must run before run_research, which ranks off stocks.json.
    try:
        run_stocks()
    except Exception as e:
        warnings.append(f"stocks failed: {e}")
        print(f"[stocks] FAILED (non-fatal): {e}")
    # OPTIONAL fundamentals enrichment (financials/ratings/company profile).
    # Charts are ALREADY written by run_stocks from the pull it does anyway —
    # nothing below this line is required for the screener or the ticker charts.
    try:
        run_research()
    except Exception as e:
        warnings.append(f"research failed: {e}")
        print(f"[research] FAILED (non-fatal): {e}")
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
    elif name == "research":
        run_research()
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
