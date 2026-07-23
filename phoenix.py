#!/usr/bin/env python3
"""
PROJECT PHOENIX — single-file edition (v0 flat).
Everything in one file so it runs on GitHub with zero folder structure.
We split this into the proper package layout later (Working Copy / a computer).

Run:
  python phoenix.py --full          full daily pipeline
  python phoenix.py --engine gex    one engine
"""
import argparse, json, os, sys
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
# ============================================================
# SCORING SYSTEM v2 — TWO-BOOK CONFIG (Trade / Investment)
# Spec: PHOENIX_REVIEW.md Part 3. v2 runs IN PARALLEL with v1:
# stocks.json keeps its v1 "stocks" list untouched and gains
# "trade_ranked" / "invest_ranked" / "v2_meta".
# EVERY weight and threshold below is ASSERTED until the Part 3.6
# backtest flips "validated" — starting points, not conclusions.
# ============================================================
STOCK_V2 = {
    "trade_gates":  {"min_mcap": 1e9, "min_dollar_vol": 10e6,
                     "near_high_floor": -8.0, "ext_hard_cap": 25.0},
    "trade_weights": {"rs_mkt": .25, "vol_surge": .20, "tightness": .20,
                      "rs12": .15, "base_quality": .10, "trigger_prox": .10},
    "invest_gates": {"min_mcap": 2e9, "rev_pos_quarters": 3, "roe_floor": 10.0,
                     "margin_tolerance_pts": 2.0, "stage2_min_weeks": 26},
    "invest_weights": {"fundamentals": .40, "long_rs": .20,
                       "durability": .20, "dd_resilience": .10, "rs12": .10},
    "fund_composite": {"rev_yoy": .35, "margin_trend": .25, "roe": .25, "fcf_margin": .15},
    "promotion": {"min_r_multiple": 1.0, "invest_score_floor": 70,
                  "streak_weeks": 4, "stage2_min_weeks": 26},
    "validated": False,   # flips True only after the Part 3.6 backtest
}

GEX = {
    "source": "SPY_x10", "risk_free": 0.045, "div_yield": 0.013,
    "otm_band": 0.15, "max_expiries": 16,
    # a strike qualifies as a WALL if its side-OI is >= this fraction of the
    # largest side-OI on that side of spot. Tactical walls are then the
    # NEAREST qualifying strike to spot, not the largest (which can sit far
    # away and is kept separately as the "magnet"). ASSERTED — tune on use.
    "wall_threshold": 0.30,
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
OUTPUTS_DIR = "outputs"


# ============================================================
# OUTPUTS — the JSON the frontend reads
# ============================================================
def write_json(name, data):
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    path = os.path.join(OUTPUTS_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=1)
    return path

def write_json_guarded(name, data, validator, warnings=None):
    """
    C3 PUBLISH GATE: validate a payload BEFORE writing it. If validation fails
    AND a previous good file exists, KEEP the previous file instead of
    publishing a broken snapshot the dashboard would silently trust.
    validator(data) -> list of problem strings (empty = valid).
    Returns True if written, False if held back.
    """
    problems = validator(data) or []
    path = os.path.join(OUTPUTS_DIR, f"{name}.json")
    if problems and os.path.exists(path):
        msg = f"{name}.json HELD BACK (kept previous good file): " + "; ".join(problems)
        print(f"[guard] {msg}")
        if warnings is not None:
            warnings.append(msg)
        return False
    if problems:
        # no previous file to protect — write anyway, but say so loudly
        print(f"[guard] {name}.json written DESPITE problems (no previous file): "
              + "; ".join(problems))
        if warnings is not None:
            warnings.append(f"{name}.json written with problems: " + "; ".join(problems))
    write_json(name, data)
    return True


def _validate_stocks(result):
    p = []
    n = len((result or {}).get("stocks") or [])
    if n < 50:
        p.append(f"only {n} candidates (<50) — pull likely broken")
    return p


def _validate_macro(result):
    p = []
    r = (result or {}).get("regime")
    if r in (None, "", "UNKNOWN") or (result or {}).get("error"):
        p.append(f"regime={r!r} err={(result or {}).get('error')!r}")
    return p


def _validate_spx(payload):
    p = []
    n = len((payload or {}).get("bars") or [])
    if n < 50:
        p.append(f"only {n} SPX bars (<50)")
    return p


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
    # WALL SELECTION (fixed 2026-07-20): the old rule picked the LARGEST
    # put-OI/call-OI strike in the whole search window regardless of distance,
    # so "support" could print a far-away strike. Tactical walls are now the
    # NEAREST qualifying strike: primary support = highest strike BELOW spot
    # whose put OI clears the threshold; primary resistance = lowest strike
    # ABOVE spot whose call OI clears it. Multiple levels are ordered by price
    # distance from spot (support nearest-below -> deepest; resistance
    # nearest-above -> highest), NOT by GEX magnitude. The largest-OI strike
    # per side is kept separately as the "magnet" (deep support / deep target),
    # never as the tactical level. Selection only — the GEX math is untouched.
    thr = GEX.get("wall_threshold", 0.30)
    max_coi = max((p["coi"] for p in above), default=0)
    max_poi = max((p["poi"] for p in below), default=0)
    resistances = sorted([p for p in above if max_coi and p["coi"] >= thr * max_coi],
                         key=lambda p: p["strike"])           # nearest-above first
    supports = sorted([p for p in below if max_poi and p["poi"] >= thr * max_poi],
                      key=lambda p: -p["strike"])             # nearest-below first
    call_wall = resistances[0] if resistances else (max(above, key=lambda p: p["coi"]) if above else None)
    put_wall = supports[0] if supports else (max(below, key=lambda p: p["poi"]) if below else None)
    call_magnet = max(above, key=lambda p: p["coi"]) if above else None
    put_magnet = max(below, key=lambda p: p["poi"]) if below else None
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
            "call_wall": call_wall,      # TACTICAL: nearest qualifying above spot
            "put_wall": put_wall,        # TACTICAL: nearest qualifying below spot
            "gamma_flip": round(flip, 2),
            # ordered by distance from spot, not by GEX magnitude
            "supports": supports[:5],        # nearest-below -> deepest
            "resistances": resistances[:5],  # nearest-above -> highest
            # largest-OI strike per side: the deep magnet, not the tactical level
            "magnets": {"put": put_magnet, "call": call_magnet},
            "wall_threshold": thr,
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


# ============================================================
# SCORING SYSTEM v2 — TWO-BOOK ENGINE (pure functions)
# Spec: PHOENIX_REVIEW.md Part 3. Design decisions worth knowing:
#   - The blend is dead: each book has its own gates and its own score.
#   - Volume surge uses the last COMPLETE week only (B1 fix — the merged
#     current week's volume is scaled x5/n, which inflated Monday surges).
#   - Industry gate T3 adds member breadth (>50% above own 10wk MA) so one
#     mega-cap can't keep a dead industry "passing" (B3 fix).
#   - Hard extension cap: >25% above the 10wk MA is ineligible for the
#     trade book regardless of score. High scores can't justify chasing.
#   - Missing fundamental data FAILS an investment gate. Conservative, same
#     convention as the regime engine.
# ============================================================

def _max_drawdown_pct(closes):
    """Max peak-to-trough drawdown (%, negative) of a close series. PURE."""
    peak, mdd = None, 0.0
    for c in closes:
        if c is None:
            continue
        if peak is None or c > peak:
            peak = c
        elif peak > 0:
            dd = (c / peak - 1) * 100
            if dd < mdd:
                mdd = dd
    return mdd


def _weeks_in_stage2(closes):
    """Consecutive weeks (from the latest bar backwards) above the 30wk MA."""
    weeks_in = 0
    for j in range(len(closes) - 1, max(29, len(closes) - 105), -1):
        m = _sma(closes[:j + 1], 30)
        if m and closes[j] > m:
            weeks_in += 1
        else:
            break
    return weeks_in


def _complete_weeks(vals):
    """
    Drop the final weekly value — after the daily merge it's the in-progress
    week (volume scaled x5/n: the exact Monday-inflation defect, B1).
    Deterministic: always uses the last FULLY CLOSED week. On weekend runs
    this lags at most one week and never inflates.
    """
    return vals[:-1] if len(vals) > 1 else vals


def _surge_complete_week(vols):
    """v2 volume surge: 3wk avg vs prior 10wk avg, on COMPLETE weeks only."""
    cw = _complete_weeks(vols)
    rv = _sma(cw, 3)
    pv = _sma(cw[-13:-3], 10) if len(cw) >= 13 else None
    return (rv / pv - 1) * 100 if (rv and pv and pv > 0) else 0


def _base_quality_raw(closes):
    """
    Base quality (replaces v1's fixed-50 filler): consecutive COMPLETE weeks
    holding within 15% of the recent high, rewarded for tightness.
    raw = tight_weeks * (15 - depth_pct). Longer + tighter base = higher.
    ASSERTED formula — validate per Part 3.6.
    """
    cw = _complete_weeks(closes)
    if len(cw) < 6:
        return 0.0
    win = cw[-27:]
    hi = max(win)
    if hi <= 0:
        return 0.0
    tight = 0
    for c in reversed(win):
        if c >= hi * 0.85:
            tight += 1
        else:
            break
    if not tight:
        return 0.0
    seg = win[-tight:]
    depth = (max(seg) - min(seg)) / max(seg) * 100
    return tight * (15.0 - min(depth, 15.0))


def compute_industry_breadth(stock_data, universe):
    """
    v2 industry gate (T3): v1's cap-weighted condition (index above rising
    10wk MA) AND >50% of members above their OWN 10wk MA.
    Returns (scores, passing_v2, breadth_map). Reuses compute_industry_scores.
    """
    from collections import defaultdict
    scores, passing_v1 = compute_industry_scores(stock_data, universe)
    tot, above = defaultdict(int), defaultdict(int)
    for tk, info in universe.items():
        bars = stock_data.get(tk)
        if not bars or len(bars) < 60:
            continue
        closes = [x[1] for x in bars]
        ma10 = _sma(closes, 10)
        if ma10 is None:
            continue
        ind = info["industry"]
        tot[ind] += 1
        if closes[-1] > ma10:
            above[ind] += 1
    breadth = {ind: round(above[ind] / tot[ind] * 100, 1) for ind in tot if tot[ind]}
    passing_v2 = {ind for ind in passing_v1 if breadth.get(ind, 0) > 50}
    return scores, passing_v2, breadth


def _investment_gate_check(qs, mcap, weeks_in):
    """
    Gates I1–I6 (Part 3.4) from quarterly history. Missing data FAILS the
    gate — conservative by design.
      growth   I1: rev_yoy > 0 in >=3 of last 4 reported quarters
      cash     I2: FCF positive summed over trailing 4 quarters
      margins  I3: latest net margin >= margin 4 quarters ago - tolerance
      returns  I4: latest ROE >= floor
      trend_age I5: stage-2 age >= 26 weeks
      size     I6: mcap >= $2B
    """
    g = STOCK_V2["invest_gates"]
    qs = qs or []
    flags = {}
    # I1 GROWTH — ADAPTIVE to data depth. rev_yoy needs 4 quarters of lookback,
    # and the committed CSV starts at ~6 quarters/ticker, so early on only ~2
    # YoY readings exist ("3 of 4" was mathematically impossible — found by the
    # smoke test on real data). Rule: at least 2 known readings, and positives
    # >= min(rev_pos_quarters, known). Tightens to the full 3-of-4 automatically
    # as the earnings auto-updater deepens the CSV.
    # Data-depth reality (measured, not assumed): the committed CSV's first
    # quarter row lacks revenue for ~92% of tickers, so most names have exactly
    # ONE computable YoY reading today. Requiring 2+ empties the invest book
    # for ~2 more quarters. Rule: >=1 known reading, positives >= min(target,
    # known). Self-tightens to the full 3-of-4 as the earnings auto-updater
    # deepens history. The other I-gates (margins/cash/returns) still confirm
    # independently. yoy_readings is exposed per entry as a data-confidence
    # signal.
    yy_known = [q.get("rev_yoy") for q in qs[-4:] if q.get("rev_yoy") is not None]
    need = min(g["rev_pos_quarters"], len(yy_known))
    flags["growth"] = (len(yy_known) >= 1 and
                       sum(1 for v in yy_known if v > 0) >= need)
    fcf = [q.get("fcf_B") for q in qs[-4:]]
    fcf_known = [v for v in fcf if v is not None]
    flags["cash"] = len(fcf_known) >= 2 and sum(fcf_known) > 0
    if (len(qs) >= 5 and qs[-1].get("net_margin") is not None
            and qs[-5].get("net_margin") is not None):
        flags["margins"] = qs[-1]["net_margin"] >= qs[-5]["net_margin"] - g["margin_tolerance_pts"]
    else:
        flags["margins"] = False
    # I4 RETURNS — the CSV's roe is QUARTERLY (NI/equity per quarter). The floor
    # is an ANNUAL number, so annualize to TTM first: sum of 4 known quarterly
    # readings, or mean*4 when 2-3 are known. (Comparing quarterly vs annual
    # demanded ~40% annualized ROE — second smoke-test finding.)
    roe_q = [q.get("roe") for q in qs[-4:] if q.get("roe") is not None]
    if len(roe_q) >= 4:
        roe_ttm = sum(roe_q[-4:])
    elif len(roe_q) >= 2:
        roe_ttm = sum(roe_q) / len(roe_q) * 4
    else:
        roe_ttm = None
    flags["returns"] = roe_ttm is not None and roe_ttm >= g["roe_floor"]
    flags["trend_age"] = weeks_in >= g["stage2_min_weeks"]
    flags["size"] = (mcap or 0) >= g["min_mcap"]
    return flags


def stock_engine_v2(stock_data, universe, quarterly=None, daily_ret=None,
                    dollar_vol=None, atr14=None):
    """
    PURE FUNCTION. The two-book engine (PHOENIX_REVIEW.md Part 3).

    stock_data: {ticker: [(date, close, volume), ...]} weekly bars, merged
    universe:   {ticker: {sector, industry, market_cap, name}}
    quarterly:  {ticker: [quarter_dict, ...]} from load_quarterly_fundamentals
    dollar_vol: {ticker: avg daily $ volume} (from the daily OHLC pull);
                missing tickers estimated from weekly volume/5 * close
    atr14:      {ticker: true 14-day ATR%} from daily OHLC (B2 fix)

    Returns {"trade_ranked": [...], "invest_ranked": [...], "meta": {...}}.
    No I/O, no network — testable with synthetic data.
    """
    quarterly = quarterly or {}
    daily_ret = daily_ret or {}
    dollar_vol = dollar_vol or {}
    atr14 = atr14 or {}
    tg = STOCK_V2["trade_gates"]

    ind_scores, passing_v2, breadth = compute_industry_breadth(stock_data, universe)

    trade_pool, invest_pool = [], []
    ext_capped = 0

    for tk, bars in stock_data.items():
        if tk not in universe or len(bars) < 40:
            continue
        closes = [x[1] for x in bars]
        vols = [x[2] for x in bars]
        last = closes[-1]
        mc = universe[tk]["market_cap"]
        ind = universe[tk]["industry"]

        ma40 = _sma(closes, 40)
        ma10 = _sma(closes, 10)
        ma30 = _sma(closes, 30)
        if not (ma40 and ma10 and ma30 and last):
            continue
        ma30_prev = _sma(closes[:-4], 30)
        ma10_prev = _sma(closes[:-4], 10)
        stage2 = last > ma30 and ma30_prev is not None and ma30 > ma30_prev
        ma10_rising = ma10_prev is not None and ma10 > ma10_prev
        weeks_in = _weeks_in_stage2(closes)

        win = (closes[-STOCK["high_lookback_weeks"]:]
               if len(closes) >= STOCK["high_lookback_weeks"] else closes)
        hi = max(win)
        pos_vs_high = (last / hi - 1) * 100
        ext = (last / ma10 - 1) * 100

        # dollar volume: daily pull if we have it, else weekly estimate
        dv = dollar_vol.get(tk)
        if dv is None and vols:
            wk_v = [v for v in vols[-4:] if v]
            dv = (sum(wk_v) / len(wk_v) / 5.0) * last if wk_v else 0

        # shared features
        surge_cw = _surge_complete_week(vols)
        ret4 = (last / closes[-5] - 1) * 100 if len(closes) >= 5 else 0
        ret12 = (last / closes[-13] - 1) * 100 if len(closes) >= 13 else 0
        ret52 = (last / closes[-53] - 1) * 100 if len(closes) >= 53 else 0
        levels = compute_breakout_levels(closes, vols, hi, ma10)
        base = {
            "ticker": tk, "name": universe[tk].get("name", ""),
            "industry": ind, "mcap_B": round(mc / 1e9, 2),
            "daily_pct": daily_ret.get(tk),
            "atr14_pct": atr14.get(tk),
            "levels": levels, "weeks_in_stage2": weeks_in,
        }

        # ---------------- TRADE BOOK ----------------
        gates = {
            "trend_long": last > ma40,
            "trend_med": last > ma10 and ma10_rising,
            "industry": ind in passing_v2,
            "near_high": pos_vs_high >= tg["near_high_floor"],
            "stage2": stage2,
            "tradability": mc >= tg["min_mcap"] and (dv or 0) >= tg["min_dollar_vol"],
        }
        n_pass = sum(1 for v in gates.values() if v)
        over_ext = ext > tg["ext_hard_cap"]
        if n_pass == 6 and over_ext:
            ext_capped += 1   # would have qualified; blocked from chasing
        if n_pass >= 5 and not over_ext:
            at_high = pos_vs_high > -1.0
            rising_px = last > closes[-4] if len(closes) >= 4 else False
            breakout = (n_pass == 6 and at_high
                        and surge_cw >= STOCK["breakout_vol_surge_pct"]
                        and rising_px and ma10_rising and surge_cw <= 1000)
            e = dict(base)
            e.update({
                "passer": n_pass == 6, "gates_passed": n_pass,
                "missing_gate": (None if n_pass == 6 else
                                 [k for k, v in gates.items() if not v][0]),
                "breakout": breakout, "surge": round(surge_cw),
                "pos_vs_high": round(pos_vs_high, 1),
                "dollar_vol_M": round((dv or 0) / 1e6, 1),
                "_ret4": ret4, "_ret12": ret12, "_ext": ext,
                "_bq": _base_quality_raw(closes),
                "_trig": (max(0.0, (levels["resistance"] / last - 1) * 100)
                          if levels and levels.get("resistance") and last else 0.0),
            })
            trade_pool.append(e)

        # ---------------- INVESTMENT BOOK ----------------
        qs = quarterly.get(tk)
        iflags = _investment_gate_check(qs, mc, weeks_in)
        if all(iflags.values()):
            latest = qs[-1]
            e = dict(base)
            e.update({
                "rev_yoy": latest.get("rev_yoy"),
                "yoy_readings": sum(1 for x in qs[-4:]
                                    if x.get("rev_yoy") is not None),
                "net_margin": latest.get("net_margin"),
                "roe": latest.get("roe"),
                "fcf_margin": latest.get("fcf_margin"),
                "_margin_trend": ((latest.get("net_margin") or 0)
                                  - (qs[-5].get("net_margin") or 0)),
                "_ret12": ret12, "_ret52": ret52,
                "_mdd": abs(_max_drawdown_pct(
                    closes[-min(max(weeks_in, 13), 104):])),
            })
            invest_pool.append(e)

    # ---------- TRADE SCORING (cross-sectional within the pool) ----------
    tw = STOCK_V2["trade_weights"]
    if trade_pool:
        A = {k: sorted(c[k] for c in trade_pool)
             for k in ["_ret4", "_ret12", "_ext", "surge", "_bq", "_trig"]}
        for c in trade_pool:
            rs_mkt = _pct_rank(A["_ret4"], c["_ret4"])
            rs12 = _pct_rank(A["_ret12"], c["_ret12"])
            tightness = 100 - _pct_rank(A["_ext"], c["_ext"])
            vol_p = _pct_rank(A["surge"], c["surge"])
            bq = _pct_rank(A["_bq"], c["_bq"])
            trig = 100 - _pct_rank(A["_trig"], c["_trig"])
            ext_pen = max(0, c["_ext"] - 15) * 0.8
            score = (rs_mkt * tw["rs_mkt"] + vol_p * tw["vol_surge"]
                     + tightness * tw["tightness"] + rs12 * tw["rs12"]
                     + bq * tw["base_quality"] + trig * tw["trigger_prox"]
                     + (3 if c["breakout"] else 0) - ext_pen)
            c["trade_score"] = round(max(0, min(100, score)))
            c["ext_pct"] = round(c["_ext"], 1)
            for k in list(c.keys()):
                if k.startswith("_"):
                    del c[k]
    trade_ranked = sorted(trade_pool,
                          key=lambda c: (-int(c["passer"]), -c["trade_score"]))
    for i, c in enumerate(trade_ranked):
        c["rank"] = i + 1

    # ---------- INVEST SCORING ----------
    iw = STOCK_V2["invest_weights"]
    fw = STOCK_V2["fund_composite"]
    if invest_pool:
        F = {k: sorted(c[k] for c in invest_pool if c.get(k) is not None)
             for k in ["rev_yoy", "roe", "fcf_margin"]}
        F["_margin_trend"] = sorted(c["_margin_trend"] for c in invest_pool)
        A = {k: sorted(c[k] for c in invest_pool)
             for k in ["_ret12", "_ret52", "weeks_in_stage2", "_mdd"]}
        for c in invest_pool:
            fparts = [
                (_pct_rank(F["rev_yoy"], c["rev_yoy"]), fw["rev_yoy"]),
                (_pct_rank(F["_margin_trend"], c["_margin_trend"]), fw["margin_trend"]),
                (_pct_rank(F["roe"], c["roe"]), fw["roe"]),
                (_pct_rank(F["fcf_margin"], c["fcf_margin"] or 0), fw["fcf_margin"]),
            ]
            fund = sum(p * w for p, w in fparts) / sum(w for _p, w in fparts)
            long_rs = _pct_rank(A["_ret52"], c["_ret52"])
            rs12 = _pct_rank(A["_ret12"], c["_ret12"])
            dur = _pct_rank(A["weeks_in_stage2"], c["weeks_in_stage2"])
            ddr = 100 - _pct_rank(A["_mdd"], c["_mdd"])
            score = (fund * iw["fundamentals"] + long_rs * iw["long_rs"]
                     + dur * iw["durability"] + ddr * iw["dd_resilience"]
                     + rs12 * iw["rs12"])
            c["invest_score"] = round(max(0, min(100, score)))
            c["fund_score"] = round(fund)
            c["max_dd_pct"] = round(-c["_mdd"], 1)
            for k in list(c.keys()):
                if k.startswith("_"):
                    del c[k]
    invest_ranked = sorted(invest_pool, key=lambda c: -c["invest_score"])
    for i, c in enumerate(invest_ranked):
        c["rank"] = i + 1

    return {
        "trade_ranked": trade_ranked,
        "invest_ranked": invest_ranked,
        "meta": {
            "asof": _now(),
            "trade_candidates": sum(1 for c in trade_ranked if c["passer"]),
            "trade_near_misses": sum(1 for c in trade_ranked if not c["passer"]),
            "trade_breakouts": sum(1 for c in trade_ranked if c.get("breakout")),
            "ext_hard_capped": ext_capped,
            "invest_candidates": len(invest_ranked),
            "industries_passing_v2": sorted(passing_v2),
            "validated": STOCK_V2["validated"],
        },
    }


# ============================================================
# PROMOTION ENGINE — TRADE -> INVESTMENT_CORE eligibility (Part 3.5)
# Evaluates open trades in outputs/trades_log.json against P1–P5 daily and
# writes outputs/promotions.json. NEVER auto-promotes — it emits tickets.
# The P3 streak persists in outputs/promo_state.json (one ISO-week = one tick).
# The prospective record starts the day this ships; it cannot be built
# retroactively.
# ============================================================
def _promo_state_load():
    import os, json
    p = os.path.join(OUTPUTS_DIR, "promo_state.json")
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            pass
    return {"streaks": {}}


def _promo_state_save(state):
    import os, json
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    with open(os.path.join(OUTPUTS_DIR, "promo_state.json"), "w") as f:
        json.dump(state, f, separators=(",", ":"))


def evaluate_promotions(v2, stock_data, universe, quarterly):
    """
    Check every OPEN trade in outputs/trades_log.json against the promotion
    spec. Reads/updates the P3 streak state, writes outputs/promotions.json.
    Missing fields on a trade row mark the criterion False with a note —
    unknown never promotes.
    """
    import os, json
    from datetime import date

    pcfg = STOCK_V2["promotion"]
    tl_path = os.path.join(OUTPUTS_DIR, "trades_log.json")
    if not os.path.exists(tl_path):
        print("[promo] no trades_log.json — nothing to evaluate")
        return None
    try:
        trades = json.load(open(tl_path)).get("trades", [])
    except Exception as e:
        print(f"[promo] trades_log.json unreadable: {e}")
        return None

    open_trades = [t for t in trades if t.get("ticker")
                   and str(t.get("status", "open")).lower() not in
                   ("closed", "exited", "stopped", "cancelled")
                   and not t.get("exit_date")]

    invest_map = {c["ticker"]: c for c in v2.get("invest_ranked", [])}
    passing = set(v2.get("meta", {}).get("industries_passing_v2", []))
    state = _promo_state_load()
    streaks = state.setdefault("streaks", {})
    iso_week = date.today().strftime("%G-W%V")

    # advance P3 streaks for every invest-scored ticker (once per ISO week)
    for tk, c in invest_map.items():
        s = streaks.get(tk, {"n": 0, "week": ""})
        if s.get("week") == iso_week:
            continue
        if c.get("invest_score", 0) >= pcfg["invest_score_floor"]:
            s = {"n": s.get("n", 0) + 1, "week": iso_week}
        else:
            s = {"n": 0, "week": iso_week}
        streaks[tk] = s
    # reset streaks for tickers that dropped out of the invest book entirely
    for tk in list(streaks.keys()):
        if tk not in invest_map and streaks[tk].get("week") != iso_week:
            streaks[tk] = {"n": 0, "week": iso_week}
    _promo_state_save(state)

    results = []
    for t in open_trades:
        tk = t["ticker"]
        checks, notes = {}, []

        bars = stock_data.get(tk) or []
        last = bars[-1][1] if bars else None
        entry = t.get("entry")
        stop = t.get("stop")
        try:
            entry = float(entry) if entry is not None else None
            stop = float(stop) if stop is not None else None
        except (TypeError, ValueError):
            entry = stop = None

        # P1 — position >= +1R
        if entry and stop and last and entry > stop:
            r = (last - entry) / (entry - stop)
            checks["P1_plus_1R"] = r >= pcfg["min_r_multiple"]
            notes.append(f"R={r:+.2f}")
        else:
            checks["P1_plus_1R"] = False
            notes.append("P1 unknown: entry/stop/price missing")

        # P2 — new quarter since entry with rev_yoy above pre-entry rate,
        #      or margin expansion. Data that did not exist at entry.
        entry_date = str(t.get("entry_date") or t.get("date") or "")[:10]
        qs = (quarterly or {}).get(tk) or []
        p2 = False
        if entry_date and qs:
            pre = [q for q in qs if (q.get("q") or "") <= entry_date]
            post = [q for q in qs if (q.get("q") or "") > entry_date]
            if post:
                pre_yy = pre[-1].get("rev_yoy") if pre else None
                pre_nm = pre[-1].get("net_margin") if pre else None
                for q in post:
                    yy, nm = q.get("rev_yoy"), q.get("net_margin")
                    accel = (yy is not None and pre_yy is not None and yy > pre_yy)
                    margin_up = (nm is not None and pre_nm is not None and nm > pre_nm)
                    if accel or margin_up:
                        p2 = True
                        notes.append(f"new Q {q.get('q')}: "
                                     f"{'rev accel' if accel else 'margin up'}")
                        break
                if not p2:
                    notes.append("new quarter(s) reported, no acceleration")
            else:
                notes.append("no new quarter since entry yet")
        else:
            notes.append("P2 unknown: entry_date or fundamentals missing")
        checks["P2_fundamental_confirm"] = p2

        # P3 — invest_score >= 70 sustained for 4+ weekly runs
        n = streaks.get(tk, {}).get("n", 0)
        checks["P3_score_streak"] = n >= pcfg["streak_weeks"]
        notes.append(f"invest-score streak {n}/{pcfg['streak_weeks']}wk")

        # P4 — industry gate (T3 v2, incl. breadth) still passing
        ind = (universe.get(tk) or {}).get("industry", "")
        checks["P4_industry"] = ind in passing

        # P5 — stage 2 age > 26 weeks
        closes = [x[1] for x in bars]
        wk_in = _weeks_in_stage2(closes) if closes else 0
        checks["P5_stage2_age"] = wk_in > pcfg["stage2_min_weeks"]
        notes.append(f"stage2 {wk_in}wk")

        results.append({
            "ticker": tk, "trade_id": t.get("id"),
            "entry": entry, "stop": stop, "last": last,
            "checks": checks,
            "eligible": all(checks.values()),
            "invest_score": invest_map.get(tk, {}).get("invest_score"),
            "notes": "; ".join(notes),
        })

    eligible = [r["ticker"] for r in results if r["eligible"]]
    payload = {"asof": _now(), "open_trades": len(open_trades),
               "eligible": eligible, "evaluations": results}
    write_json("promotions", payload)
    print(f"[promo] evaluated {len(results)} open trades; "
          f"{len(eligible)} promotion-eligible" +
          (f": {', '.join(eligible)}" if eligible else ""))
    return payload


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
        # A4: this is the winning regime's raw score on its OWN scale — regimes
        # are NOT calibrated against each other, so it is not a probability.
        "confidence_note": "regime score, not a calibrated probability (A4)",
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
    write_json_guarded("macro", result, _validate_macro)

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

# --- earnings auto-update ---
# Per-ticker Yahoo endpoints throttle at roughly 150 sequential calls. Stay under
# it: check this many per run, rotating through the due queue so the universe is
# covered over several days. During earnings season the whole queue cycles in
# under a fortnight, and anything you actually hold is checked EVERY run.
EARNINGS_CHECK_PER_RUN = 140
# Start checking a ticker this many days after its next quarter-end.
# 10, not 25: big banks report ~2 weeks after quarter end (JPM/GS mid-July for
# Q2). A 25-day grace would miss them entirely. The cap+rotation absorbs the
# larger queue this creates.
EARNINGS_GRACE_DAYS = 10
FUND_CSV = "macroflow_fundamentals_quarterly.csv"


def _gex_universe_tickers():
    """Tickers in the GEX universe (eligible or not) — they get charted so their
    detail pages always show a price chart, not just the GEX histogram."""
    import json, os
    p = os.path.join(OUTPUTS_DIR, "gex_universe.json")
    out = set()
    if os.path.exists(p):
        try:
            for r in json.load(open(p)).get("universe", []):
                if r.get("ticker"):
                    out.add(r["ticker"])
        except Exception:
            pass
    return out


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


def _earnings_due_queue(quarterly, grace_days=None):
    """
    Tickers whose NEXT quarter should plausibly have reported by now.

    A company with a quarter ending 2026-03-31 has its next quarter end around
    2026-06-30, and reports it roughly 25-75 days later. So if 2026-06-30 + grace
    is in the past, there may be a new quarter waiting for us.

    Returns [(ticker, days_overdue)] sorted most-overdue first — the ones most
    likely to have something new.
    """
    from datetime import date, timedelta
    grace = EARNINGS_GRACE_DAYS if grace_days is None else grace_days
    today = date.today()
    out = []
    for tk, qs in (quarterly or {}).items():
        if not qs:
            continue
        last_q = qs[-1].get("q")
        if not last_q:
            continue
        try:
            q = date.fromisoformat(last_q[:10])
        except Exception:
            continue
        next_end = q + timedelta(days=92)
        expected = next_end + timedelta(days=grace)
        if today >= expected:
            out.append((tk, (today - expected).days))
    out.sort(key=lambda x: -x[1])
    return out


def _load_earnings_state():
    """Cursor + known next-earnings dates, so runs continue where the last stopped."""
    import os, json
    p = os.path.join(OUTPUTS_DIR, "earnings_state.json")
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            pass
    return {"cursor": 0, "next_dates": {}, "last_checked": {}}


def _save_earnings_state(state):
    import os, json
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    with open(os.path.join(OUTPUTS_DIR, "earnings_state.json"), "w") as f:
        json.dump(state, f, separators=(",", ":"))


def _append_quarters_to_csv(new_rows, path=None):
    """
    Append genuinely-new quarters to the source CSV, keeping it the single source
    of truth. Rewrites the whole file sorted, so re-exporting by hand still works.
    """
    import csv, os
    path = path or FUND_CSV
    if not new_rows or not os.path.exists(path):
        return 0
    with open(path) as f:
        rd = csv.DictReader(f)
        cols = rd.fieldnames
        existing = list(rd)
    have = {(r["ticker"], r["quarter_end"]) for r in existing}
    added = [r for r in new_rows if (r["ticker"], r["quarter_end"]) not in have]
    if not added:
        return 0
    allrows = existing + added
    allrows.sort(key=lambda r: (r["ticker"], r["quarter_end"]))
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in allrows:
            w.writerow({c: r.get(c, "") for c in cols})
    os.replace(tmp, path)
    return len(added)


def check_earnings_updates(quarterly, priority=None, limit=None):
    """
    Pull NEW quarterly results for tickers that are due, capped and rotating.

    Returns (new_rows, next_dates, checked_tickers, changed_tickers).

    This is the ONLY part of the system that needs per-ticker Yahoo calls. It's
    capped under the throttle ceiling and rotates via a saved cursor, so the whole
    universe gets covered across several runs while anything you hold is checked
    every time.
    """
    try:
        import yfinance as yf
    except Exception as e:
        print(f"[earnings] yfinance unavailable: {e}")
        return [], {}, [], set()

    import time
    from datetime import date

    limit = EARNINGS_CHECK_PER_RUN if limit is None else limit
    state = _load_earnings_state()
    due = _earnings_due_queue(quarterly)
    # priority tickers with NO quarterly history at all are always "due" —
    # they have everything to learn (new universe additions).
    extra = [t for t in (priority or []) if t not in (quarterly or {})]
    due = [(t, 9999) for t in extra] + [d for d in due if d[0] not in set(extra)]
    if not due:
        print("[earnings] nothing due — every ticker's next quarter is still ahead")
        return [], {}, [], set()

    due_tks = [t for t, _d in due]
    due_set = set(due_tks)
    priority = [t for t in (priority or []) if t in due_set]

    # JUST-REPORTED FAST LANE: if we already learned a ticker's earnings date and
    # it has passed (or is today), check it NOW rather than waiting up to 15 days
    # for its rotation slot. This is what makes "reported yesterday -> visible
    # today" actually work.
    known = state.get("next_dates", {}) or {}
    last_chk = state.get("last_checked", {}) or {}
    today_d = date.today()
    just_reported = []
    for tk, ds in known.items():
        if tk not in due_set or tk in priority:
            continue
        try:
            d = date.fromisoformat(str(ds)[:10])
        except Exception:
            continue
        # window: reported in the last 10 days, and we haven't checked since
        if 0 <= (today_d - d).days <= 10:
            lc = last_chk.get(tk)
            if not lc or lc < ds:
                just_reported.append(tk)
    if just_reported:
        print(f"[earnings] fast lane: {len(just_reported)} tickers reported in the last 10d")

    head = priority + [t for t in just_reported if t not in set(priority)]
    rest = [t for t in due_tks if t not in set(head)]
    cursor = int(state.get("cursor", 0)) % max(1, len(rest))
    rotated = rest[cursor:] + rest[:cursor]
    batch = head + rotated[:max(0, limit - len(head))]

    print(f"[earnings] {len(due)} due; checking {len(batch)} this run "
          f"({len(priority)} pinned + {len(just_reported)} just-reported + "
          f"{max(0,len(batch)-len(head))} rotating from #{cursor})")

    new_rows, next_dates, changed = [], {}, set()
    have = {tk: {q.get("q") for q in qs} for tk, qs in (quarterly or {}).items()}
    today_s = date.today().isoformat()
    fail = 0

    for idx, tk in enumerate(batch, 1):
        try:
            t = yf.Ticker(tk)

            # next earnings date — the thing you asked for
            try:
                cal = t.calendar
                ed = None
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if isinstance(ed, list) and ed:
                        ed = ed[0]
                if ed is not None:
                    next_dates[tk] = ed.strftime("%Y-%m-%d") if hasattr(ed, "strftime") else str(ed)[:10]
            except Exception:
                pass

            # new quarterly results?
            try:
                qf = t.quarterly_financials
                bs = None
                cf = None
                try:
                    bs = t.quarterly_balance_sheet
                except Exception:
                    pass
                try:
                    cf = t.quarterly_cashflow
                except Exception:
                    pass
                if qf is not None and qf.shape[1] > 0:
                    for col in list(qf.columns):
                        qend = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)[:10]
                        if qend in have.get(tk, set()):
                            continue   # already have this quarter

                        def g(df, key):
                            if df is None:
                                return ""
                            try:
                                v = df.loc[key, col]
                                return "" if (v is None or _isnan(v)) else float(v)
                            except Exception:
                                return ""

                        rev = g(qf, "Total Revenue")
                        if rev == "":
                            continue   # no revenue -> not a real quarter row
                        row = {
                            "ticker": tk, "quarter_end": qend,
                            "revenue": rev,
                            "gross_profit": g(qf, "Gross Profit"),
                            "operating_income": g(qf, "Operating Income"),
                            "net_income": g(qf, "Net Income"),
                            "ebitda": g(qf, "EBITDA"),
                            "cost_of_revenue": g(qf, "Cost Of Revenue"),
                            "operating_cash_flow": g(cf, "Operating Cash Flow"),
                            "free_cash_flow": g(cf, "Free Cash Flow"),
                            "capex": g(cf, "Capital Expenditure"),
                            "total_debt": g(bs, "Total Debt"),
                            "total_equity": g(bs, "Stockholders Equity"),
                            "total_assets": g(bs, "Total Assets"),
                            "cash": g(bs, "Cash And Cash Equivalents"),
                            "current_assets": g(bs, "Current Assets"),
                            "current_liabilities": g(bs, "Current Liabilities"),
                        }
                        new_rows.append(row)
                        changed.add(tk)
                        print(f"[earnings]   NEW: {tk} {qend} rev ${rev/1e9:.2f}B")
            except Exception:
                fail += 1

            state.setdefault("last_checked", {})[tk] = today_s
            if idx % 25 == 0:
                print(f"[earnings]   {idx}/{len(batch)} checked, {len(changed)} with new data")
            time.sleep(0.6)
        except Exception:
            fail += 1
            continue

    # advance the cursor for next run
    if rest:
        state["cursor"] = (cursor + max(0, len(batch) - len(head))) % len(rest)
    state.setdefault("next_dates", {}).update(next_dates)
    _save_earnings_state(state)

    print(f"[earnings] checked {len(batch)}, {len(changed)} had new quarters, "
          f"{len(next_dates)} earnings dates, {fail} failed")
    return new_rows, next_dates, batch, changed


def write_financials(quarterly, universe=None, source_csv=None, next_dates=None, force=None):
    """
    Write outputs/fin/TK.json — one small file per ticker, ~625 bytes.

    WHY SEPARATE FROM CHARTS:
      - Earnings land quarterly; prices move daily. Bundling them meant rewriting
        identical financial data into thousands of files every run.
      - Chart files are limited to screener gate-passers. Financials shouldn't be:
        you must be able to look up ASML or NVDA whether or not they pass a
        momentum gate today.

    HASH-GATED: we fingerprint the source CSV and skip the whole write if it
    hasn't changed. So a normal daily run touches zero financial files, and git
    sees zero churn. Re-export the CSV and the next run picks it up automatically.

    Covers every ticker in the CSV (~2,139), not just candidates.
    """
    import os, json, hashlib

    if not quarterly:
        print("[fin] no quarterly data — skipping")
        return 0

    source_csv = source_csv or FUND_CSV
    next_dates = next_dates or {}
    force = force or set()

    fin_dir = os.path.join(OUTPUTS_DIR, "fin")
    stamp_path = os.path.join(fin_dir, ".source_hash")

    # Fingerprint the source so unchanged data costs nothing. BUT never skip if
    # earnings just landed (force) or if we learned new earnings dates — during
    # earnings season the whole point is that this file MUST update.
    digest = None
    if os.path.exists(source_csv):
        h = hashlib.sha256()
        with open(source_csv, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
        if not force and not next_dates and os.path.exists(stamp_path):
            try:
                if open(stamp_path).read().strip() == digest:
                    have = len([f for f in os.listdir(fin_dir) if f.endswith(".json")])
                    expected = sum(1 for qs in quarterly.values() if qs)
                    # only skip if the directory is actually COMPLETE — otherwise
                    # a hash match on a run whose files were never written (or were
                    # pruned) would strand tickers like NVDA with no fin file.
                    if have >= expected:
                        print(f"[fin] source unchanged, {have}/{expected} files current, skipping")
                        return 0
                    print(f"[fin] hash matches but only {have}/{expected} files exist "
                          f"— regenerating the missing ones")
            except Exception:
                pass

    os.makedirs(fin_dir, exist_ok=True)
    written = 0
    for tk, qs in quarterly.items():
        if not qs:
            continue
        u = (universe or {}).get(tk) or {}
        mc = u.get("market_cap")
        payload = {
            "ticker": tk,
            "name": u.get("name") or "",
            "sector": u.get("sector") or "",
            "industry": u.get("industry") or "",
            "quarters": qs[-8:],
        }
        # Earnings calendar + a flag the UI uses to shout "new numbers".
        nd = next_dates.get(tk)
        if nd:
            payload["next_earnings"] = nd
        if tk in force:
            payload["fresh_earnings"] = qs[-1].get("q")   # the quarter that just landed
            payload["fresh_asof"] = _now()
        if mc:
            payload["mcap_B"] = round(mc / 1e9, 2)
            # trailing P/E: mcap / sum of last 4 quarters' net income
            nis = [q.get("net_income_B") for q in qs[-4:] if q.get("net_income_B") is not None]
            if len(nis) == 4 and sum(nis) > 0:
                payload["pe"] = round(mc / 1e9 / sum(nis), 1)
        safe = tk.replace("/", "-").replace(".", "-")
        with open(os.path.join(fin_dir, f"{safe}.json"), "w") as f:
            json.dump(payload, f, separators=(",", ":"))
        written += 1

    if digest:
        with open(stamp_path, "w") as f:
            f.write(digest)

    print(f"[fin] wrote {written} financial files (ALL tickers, gates ignored, no network)")

    # BUNDLE: a single file with financials for the names most likely to be
    # opened (GEX universe + pinned trades). The dashboard reads this as a
    # fallback so a detail page never shows empty financials just because one
    # per-ticker file failed to commit. Small + always regenerated.
    try:
        want = _gex_universe_tickers() | _pinned_tickers()
        bundle = {}
        for tk in want:
            qs = quarterly.get(tk)
            if qs:
                u = (universe or {}).get(tk) or {}
                mc = u.get("market_cap")
                entry = {"quarters": qs[-8:], "name": u.get("name") or "",
                         "sector": u.get("sector") or "", "industry": u.get("industry") or ""}
                if mc:
                    entry["mcap_B"] = round(mc / 1e9, 2)
                    nis = [q.get("net_income_B") for q in qs[-4:] if q.get("net_income_B") is not None]
                    if len(nis) == 4 and sum(nis) > 0:
                        entry["pe"] = round(mc / 1e9 / sum(nis), 1)
                bundle[tk] = entry
        write_json("fin_bundle", {"asof": _now(), "count": len(bundle), "tickers": bundle})
        print(f"[fin] wrote fin_bundle.json ({len(bundle)} priority tickers)")
    except Exception as e:
        print(f"[fin] bundle skipped: {e}")
    return written


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
        # Financials do NOT live here — they change quarterly, charts change daily.
        # Rewriting them in every chart file every day is pure churn, and it would
        # tie them to the gate-passer list (which is why mega-caps like ASML had
        # nothing). They're written separately by write_financials().
        u = (universe or {}).get(tk) or {}
        if u:
            payload["profile"] = {
                "name": u.get("name"), "sector": u.get("sector"),
                "industry": u.get("industry"),
            }
            mc = u.get("market_cap")
            if mc:
                payload["quote"]["mcap_B"] = round(mc / 1e9, 2)
        # EMBED FINANCIALS (fix 2026-07-20): the detail page reads the chart
        # file for any ticker it can open. Putting the last 8 quarters here means
        # financials can NEVER be missing on a page that renders — no dependency
        # on a separate fin/TK.json or fin_bundle.json committing in the same run.
        if quarterly:
            qs = quarterly.get(tk)
            if qs:
                payload["quarters"] = qs[-8:]
                mc2 = (universe or {}).get(tk, {}).get("market_cap")
                if mc2:
                    nis = [q.get("net_income_B") for q in qs[-4:]
                           if q.get("net_income_B") is not None]
                    if len(nis) == 4 and sum(nis) > 0:
                        payload["pe"] = round(mc2 / 1e9 / sum(nis), 1)
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
        write_json_guarded("spx_daily", {"symbol": "SPX", "bars": bars,
                                         "asof": bars[-1]["date"] if bars else None},
                           _validate_spx)
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
    """Load ticker -> {sector, industry, market_cap} from a committed CSV.
    PROVENANCE (B4, closed 2026-07-20): sector/industry labels are exported
    from TradingView and confirmed correct by Gabriel. The industry gate (T3)
    is load-bearing; if the universe is ever re-exported from a different
    source, re-confirm the label taxonomy before trusting Layer 2."""
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
    # --- SCORING v2: two-book engine (parallel with v1; PHOENIX_REVIEW Part 3) ---
    # v1's "stocks" list above is untouched — the dashboard keeps working.
    # v2 adds trade_ranked / invest_ranked / v2_meta to the same file, plus
    # true 14-day ATR% (B2 fix) and avg daily dollar volume from the OHLC
    # pull we already paid for.
    try:
        dollar_vol, atr14 = {}, {}
        for tk, bars in (ohlcv or {}).items():
            tail = bars[-20:]
            dv = [c * v for (_d, _o, _h, _l, c, v) in tail if c and v]
            if dv:
                dollar_vol[tk] = sum(dv) / len(dv)
            # true ATR: max(h-l, |h-prev_c|, |l-prev_c|), 14-day mean, % of last
            trs, prev_c = [], None
            for (_d, _o, h, l, c, _v) in bars[-15:]:
                if h is None or l is None or c is None:
                    prev_c = c if c is not None else prev_c
                    continue
                tr = (h - l) if prev_c is None else max(h - l, abs(h - prev_c), abs(l - prev_c))
                trs.append(tr)
                prev_c = c
            last_c = bars[-1][4] if bars and bars[-1][4] else None
            if trs and last_c:
                atr14[tk] = round(sum(trs[-14:]) / len(trs[-14:]) / last_c * 100, 2)
        v2 = stock_engine_v2(weekly, universe, quarterly=quarterly,
                             daily_ret=daily_ret, dollar_vol=dollar_vol, atr14=atr14)
        result["trade_ranked"] = v2["trade_ranked"]
        result["invest_ranked"] = v2["invest_ranked"]
        result["v2_meta"] = v2["meta"]
        vm = v2["meta"]
        print(f"[v2] trade book: {vm['trade_candidates']} candidates, "
              f"{vm['trade_near_misses']} near-misses, {vm['trade_breakouts']} breakouts, "
              f"{vm['ext_hard_capped']} blocked by ext cap")
        print(f"[v2] invest book: {vm['invest_candidates']} candidates, "
              f"{len(vm['industries_passing_v2'])} industries passing (with breadth)")
        # Promotion eligibility (Part 3.5) — the prospective record starts now.
        try:
            evaluate_promotions(v2, weekly, universe, quarterly)
        except Exception as e:
            print(f"[promo] FAILED (non-fatal): {e}")
    except Exception as e:
        result["v2_meta"] = {"error": str(e)}
        print(f"[v2] FAILED (non-fatal): {e}")

    write_json_guarded("stocks", result, _validate_stocks)
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
            keep |= _gex_universe_tickers()   # every Phoenix ticker gets a chart
            keep &= set(ohlcv.keys())
            write_charts(ohlcv, weekly_csv=weekly_raw, tickers=sorted(keep),
                         universe=universe, quarterly=quarterly)
        except Exception as e:
            print(f"[charts] FAILED (non-fatal): {e}")
    else:
        print("[charts] skipped — no fresh OHLC this run (charts keep last run's files)")

    # --- EARNINGS AUTO-UPDATE ---
    # The CSV is a snapshot; during earnings season it goes stale in days. Each
    # run we check a capped, rotating slice of the tickers whose next quarter is
    # plausibly out, append anything new to the CSV, and re-derive. Your open
    # positions and plans jump the queue and are checked every single run.
    new_dates, changed = {}, set()
    if auto_pull:
        try:
            pinned = _pinned_tickers()
            ranked = _ranked_candidates()
            # NEW-TICKER BACKFILL (2026-07-20): tickers added to universe.csv
            # after the fundamentals export (e.g. PLTR/MSTR/SNOW/UBER/BRK-B)
            # have NO quarterly rows, and the due-queue only iterates quarterly
            # — so they would never be checked. Seed the biggest missing names
            # into the priority head, capped so they can't flood the rotation.
            never_seen = sorted((t for t in universe if t not in quarterly),
                                key=lambda t: -universe[t]["market_cap"])[:10]
            # priority: what you hold/plan first, then the best screener names
            prio = [t for t in sorted(pinned)] + [t for t in never_seen if t not in pinned]
            for t in ranked[:60]:
                if t not in prio:
                    prio.append(t)
            rows, new_dates, checked, changed = check_earnings_updates(quarterly, priority=prio)
            if rows:
                added = _append_quarters_to_csv(rows)
                print(f"[earnings] appended {added} new quarters to {FUND_CSV}")
                if added:
                    quarterly = load_quarterly_fundamentals()   # re-derive margins/growth
        except Exception as e:
            print(f"[earnings] FAILED (non-fatal): {e}")

    # Financials for EVERY ticker in the CSV — no gate filter, no network.
    # Hash-gated, but never skipped when earnings just landed.
    try:
        write_financials(quarterly, universe=universe,
                         next_dates=new_dates, force=changed)
    except Exception as e:
        print(f"[fin] FAILED (non-fatal): {e}")
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

def calib_log_add(date, source_net_gex, source_vanna, source_charm, source_flip=None):
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
        # flip offset tracking (added 2026-07-20 after the coach-briefing
        # divergence: engine ~7,450 vs source 7,495.40 on 2026-07-17 — the SPY
        # proxy under-weights deep SPX institutional put OI, e.g. 1.81M
        # contracts at 7,000, pulling the computed flip toward spot).
        "engine_flip": (gex.get("overview") or {}).get("gamma_flip"),
        "engine_spot": (gex.get("overview") or {}).get("spx_spot"),
        "source_flip": source_flip,
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


def _pair_calib_entries(source_rows, engine_by_date):
    """
    PURE. Pair briefing readings with engine git-history readings by date.
    source_rows: [{date, net_gex, vanna, charm, flip, spot}]
    engine_by_date: {date: {net_gex_B, net_vanna_B, net_charm_B, flip, spot}}
    Returns (entries, missing_dates).
    """
    entries, missing = [], []
    for r in source_rows:
        d = r["date"]
        e = engine_by_date.get(d)
        if not e:
            missing.append(d)
            continue
        entries.append({
            "date": d,
            "engine_net_gex": e.get("net_gex_B"),
            "engine_vanna": e.get("net_vanna_B"),
            "engine_charm": e.get("net_charm_B"),
            "engine_flip": e.get("flip"),
            "engine_spot": e.get("spot"),
            "source_net_gex": r.get("net_gex"),
            "source_vanna": r.get("vanna"),
            "source_charm": r.get("charm"),
            "source_flip": r.get("flip"),
            "source_spot": r.get("spot"),
        })
    return entries, missing


def _flip_side_agreement(log):
    """
    PURE. THE metric that matters: does the engine put the gamma flip on the
    same SIDE of spot as the source? Side = the regime call itself.
    Returns (n_comparable, n_agree, disagreements[dates]).
    """
    n, agree, bad = 0, 0, []
    for e in log:
        ef, es = e.get("engine_flip"), e.get("engine_spot")
        sf, ss = e.get("source_flip"), e.get("source_spot")
        if None in (ef, es, sf, ss):
            continue
        n += 1
        if ((ef - es) >= 0) == ((sf - ss) >= 0):
            agree += 1
        else:
            bad.append(e.get("date"))
    return n, agree, bad


def calib_backfill(source_csv="calib_source.csv"):
    """
    Pair the ENTIRE briefing archive against the ENTIRE engine git history.

    Source side: calib_source.csv (date,net_gex,vanna,charm,flip,spot) —
    one line per archived coach briefing.
    Engine side: git history of outputs/gex.json — the Action commits it
    daily and the engine keeps raw uncalibrated values precisely for this.

    Runs inside the Action (calibrate.yml, fetch-depth:0 — a shallow
    checkout has no history and this will find zero commits).
    Merges pairs into calibration_log.json, then runs the analysis.
    """
    import csv, subprocess, os
    if not os.path.exists(source_csv):
        print(f"[calib] {source_csv} not found — add one line per briefing:")
        print("        date,net_gex,vanna,charm,flip,spot")
        return None
    source_rows = []
    with open(source_csv) as f:
        for r in csv.DictReader(f):
            try:
                source_rows.append({
                    "date": r["date"].strip(),
                    "net_gex": float(r["net_gex"]),
                    "vanna": float(r["vanna"]) if r.get("vanna") else None,
                    "charm": float(r["charm"]) if r.get("charm") else None,
                    "flip": float(r["flip"]) if r.get("flip") else None,
                    "spot": float(r["spot"]) if r.get("spot") else None,
                })
            except Exception as e:
                print(f"[calib] bad row skipped: {r} ({e})")
    print(f"[calib] {len(source_rows)} source readings from {source_csv}")

    # engine readings from git history (last commit per calendar date)
    engine_by_date = {}
    try:
        out = subprocess.run(
            ["git", "log", "--format=%H %cs", "--", "outputs/gex.json"],
            capture_output=True, text=True, timeout=120).stdout
        commits = [l.split() for l in out.strip().splitlines() if l.strip()]
        if not commits:
            print("[calib] git history empty — shallow checkout? "
                  "calibrate.yml must use fetch-depth: 0")
        seen_dates = set()
        for h, d in commits:          # newest first; keep last commit per date
            if d in seen_dates:
                continue
            seen_dates.add(d)
            try:
                blob = subprocess.run(["git", "show", f"{h}:outputs/gex.json"],
                                      capture_output=True, text=True,
                                      timeout=60).stdout
                g = json.loads(blob)
                raw, ov = g.get("raw") or {}, g.get("overview") or {}
                if raw.get("net_gex_B") is not None:
                    engine_by_date[d] = {
                        "net_gex_B": raw.get("net_gex_B"),
                        "net_vanna_B": raw.get("net_vanna_B"),
                        "net_charm_B": raw.get("net_charm_B"),
                        "flip": ov.get("gamma_flip"),
                        "spot": ov.get("spx_spot"),
                    }
            except Exception:
                continue
        print(f"[calib] engine readings recovered from git: {len(engine_by_date)} dates")
    except FileNotFoundError:
        print("[calib] git not available — cannot backfill engine side here")
        return None

    entries, missing = _pair_calib_entries(source_rows, engine_by_date)
    if missing:
        print(f"[calib] {len(missing)} briefing dates with no engine commit: "
              f"{', '.join(missing[:8])}{'...' if len(missing) > 8 else ''}")
    # merge into calibration_log.json (replace same-date)
    log = []
    if os.path.exists(CALIB_LOG):
        try:
            log = json.load(open(CALIB_LOG))
        except Exception:
            log = []
    have = {e["date"] for e in entries}
    log = [e for e in log if e.get("date") not in have] + entries
    log.sort(key=lambda e: e.get("date", ""))
    with open(CALIB_LOG, "w") as f:
        json.dump(log, f, indent=1)
    print(f"[calib] calibration_log.json now has {len(log)} paired readings")
    calib_analyze()
    return log


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
    flips = [(e["date"], e["source_flip"] - e["engine_flip"]) for e in log
             if e.get("source_flip") is not None and e.get("engine_flip") is not None]
    if flips:
        offs = [o for _d, o in flips]
        import statistics as _st
        print("GAMMA FLIP offset (source - engine):")
        for d, o in sorted(flips):
            print(f"  {d}: {o:+.1f} pts")
        print(f"  -> median offset {_st.median(offs):+.1f} pts "
              f"({'STABLE' if len(offs) >= 3 and (max(offs)-min(offs)) < 30 else 'need more data'})")
    n, agree, bad = _flip_side_agreement(log)
    if n:
        pct = agree / n * 100
        print(f"REGIME-SIDE AGREEMENT (flip on same side of spot as source): "
              f"{agree}/{n} ({pct:.0f}%)")
        if bad:
            print(f"  disagreement dates: {', '.join(bad)}")
        print("  This is THE metric: below ~90%, the proxy's regime call is not")
        print("  trustworthy and the flip offset (above) should be applied, or the")
        print("  briefing treated as the SPX regime source of record.\n")
    else:
        print()
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
# C1 — CIO THESES IN THE PIPELINE (Layer 4, done right)
# The dashboard's keyless api.anthropic.com call can never work on GitHub
# Pages (CORS/auth — it only works inside the claude.ai artifact sandbox).
# The correct architecture per the Phoenix doc: Claude joins the DAILY BATCH.
# This generates theses for the top trade-book names + breakouts using
# ANTHROPIC_API_KEY from GitHub Secrets and writes outputs/theses.json;
# the dashboard reads the file first and only falls back to a live call.
# ============================================================
THESES_TOP_N = 8

def _thesis_prompt(s, regime):
    lv = s.get("levels") or {}
    return (
        "You are a senior hedge fund PM writing a concise trade thesis for "
        + s.get("ticker", "") + (" (" + s["name"] + ")" if s.get("name") else "") + ". "
        + "Data: industry " + str(s.get("industry")) + ", mcap $" + str(s.get("mcap_B")) + "B, "
        + "trade score " + str(s.get("trade_score")) + "/100, "
        + ("BREAKOUT flagged, " if s.get("breakout") else "")
        + "volume surge " + str(s.get("surge")) + "%, "
        + str(s.get("pos_vs_high")) + "% vs 2yr high, ATR14 " + str(s.get("atr14_pct")) + "%. "
        + "Levels: last " + str(lv.get("last")) + ", resistance " + str(lv.get("resistance"))
        + ", support " + str(lv.get("support")) + ", 50d MA " + str(lv.get("ma50")) + ". "
        + "Macro regime: " + str(regime) + ". "
        + "Write 4 short sections labeled THESIS:, ENTRY:, EXITS:, SIZING: — "
        + "max 120 words total, concrete price levels, no hedging boilerplate."
    )


def run_theses(top_n=None):
    """Generate CIO theses in the batch. Skips cleanly without the API key."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        print("[theses] ANTHROPIC_API_KEY not set — skipping (add it as a GitHub secret to enable)")
        return None
    import requests, time
    top_n = top_n or THESES_TOP_N
    path = os.path.join(OUTPUTS_DIR, "stocks.json")
    if not os.path.exists(path):
        print("[theses] no stocks.json — skipping")
        return None
    try:
        d = json.load(open(path))
    except Exception as e:
        print(f"[theses] stocks.json unreadable: {e}")
        return None
    regime = None
    try:
        regime = json.load(open(os.path.join(OUTPUTS_DIR, "macro.json"))).get("regime")
    except Exception:
        pass
    ranked = d.get("trade_ranked") or d.get("stocks") or []
    picks = [s for s in ranked if s.get("breakout")] + [s for s in ranked if not s.get("breakout")]
    seen, todo = set(), []
    for s in picks:
        if s["ticker"] not in seen:
            seen.add(s["ticker"])
            todo.append(s)
        if len(todo) >= top_n:
            break
    out, ok = {}, 0
    for s in todo:
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 400,
                      "messages": [{"role": "user", "content": _thesis_prompt(s, regime)}]},
                timeout=60)
            data = r.json()
            text = "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text").strip()
            if text:
                out[s["ticker"]] = {"text": text, "score": s.get("trade_score"),
                                    "breakout": bool(s.get("breakout"))}
                ok += 1
            else:
                print(f"[theses] {s['ticker']}: empty response "
                      f"({(data.get('error') or {}).get('message', 'no error field')})")
            time.sleep(0.5)
        except Exception as e:
            print(f"[theses] {s['ticker']} failed: {e}")
    write_json("theses", {"asof": _now(), "regime": regime, "theses": out})
    print(f"[theses] wrote outputs/theses.json ({ok}/{len(todo)} generated)")
    return out


# ============================================================
# E2 — PUSH ALERTS via ntfy.sh (free iPhone push, zero infra)
# Set NTFY_TOPIC as a GitHub secret (any hard-to-guess string), then
# subscribe to that topic in the ntfy app. Alerts fire only on CHANGES:
# new breakouts vs the previous run, regime flips, promotion eligibility.
# State lives in outputs/alert_state.json so re-runs don't re-alert.
# ============================================================
def _notify(title, msg, priority="default", tags=None):
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        return False
    import requests
    try:
        requests.post("https://ntfy.sh/" + topic, data=msg.encode("utf-8"),
                      headers={"Title": title, "Priority": priority,
                               "Tags": tags or "chart_with_upwards_trend"},
                      timeout=15)
        return True
    except Exception as e:
        print(f"[alerts] ntfy post failed: {e}")
        return False


def run_alerts():
    """Diff-based alerts: only what CHANGED since the last run (E9 doctrine)."""
    if not os.environ.get("NTFY_TOPIC", ""):
        print("[alerts] NTFY_TOPIC not set — skipping (add it as a GitHub secret to enable)")
        return None
    sp = os.path.join(OUTPUTS_DIR, "alert_state.json")
    state = {}
    if os.path.exists(sp):
        try:
            state = json.load(open(sp))
        except Exception:
            state = {}
    sent = 0

    # new breakouts (prefer the v2 trade book)
    try:
        d = json.load(open(os.path.join(OUTPUTS_DIR, "stocks.json")))
        ranked = d.get("trade_ranked") or d.get("stocks") or []
        brk = sorted(s["ticker"] for s in ranked if s.get("breakout"))
        new = [t for t in brk if t not in set(state.get("breakouts", []))]
        if new:
            if _notify("Phoenix: new breakout" + ("s" if len(new) > 1 else ""),
                       ", ".join(new), tags="rotating_light"):
                sent += 1
        state["breakouts"] = brk
    except Exception as e:
        print(f"[alerts] breakout diff failed: {e}")

    # regime change
    try:
        m = json.load(open(os.path.join(OUTPUTS_DIR, "macro.json")))
        reg = m.get("regime")
        if reg and state.get("regime") and reg != state["regime"]:
            if _notify("Phoenix: regime change",
                       f"{state['regime']} -> {reg} (score {m.get('confidence')})",
                       priority="high", tags="warning"):
                sent += 1
        if reg:
            state["regime"] = reg
    except Exception as e:
        print(f"[alerts] regime diff failed: {e}")

    # promotion eligibility (newly eligible only)
    try:
        p = json.load(open(os.path.join(OUTPUTS_DIR, "promotions.json")))
        elig = sorted(p.get("eligible", []))
        new = [t for t in elig if t not in set(state.get("promo_eligible", []))]
        if new:
            if _notify("Phoenix: promotion ticket",
                       ", ".join(new) + " passed P1-P5 — review for INVESTMENT_CORE",
                       priority="high", tags="arrow_up"):
                sent += 1
        state["promo_eligible"] = elig
    except Exception as e:
        print(f"[alerts] promotion diff failed: {e}")

    with open(sp, "w") as f:
        json.dump(state, f, separators=(",", ":"))
    print(f"[alerts] {sent} notification(s) sent")
    return sent


# ============================================================
# E3b STAGE 0 — GEX UNIVERSE ELIGIBILITY via OCC (keyless, free)
# The Coach's five rules decide which tickers are valid GEX subjects.
# OCC (Options Clearing Corporation) is the clearinghouse for every US listed
# option — its numbers are ground truth, published free with no credentials,
# which is why it (and never IBKR) feeds the GitHub Action.
#
# HONESTY NOTE: OCC's script endpoints are documented, but their exact
# query-parameter grammar could not be fully exercised before shipping (the
# dev sandbox has no network). The fetcher therefore tries several documented
# parameter patterns, logs which one worked, and on total failure logs the
# response head so the Action log itself becomes the debugging tool. All
# failures are non-fatal; the committed gex_universe.json (seeded from
# in-chat IBKR measurements on 2026-07-20) is never overwritten with an
# empty result thanks to the publish gate.
# ============================================================
GEX_UNIVERSE = {
    # Seed list: only ~30-80 names in the whole market can pass Rule 2's
    # 100k-contracts floor, so scanning 2,898 tickers is pointless.
    # IBKR-verified 2026-07-20: TSLA NVDA TSM MU AMD pass Rules 1-2; WDC fails.
    "seed": ["TSLA", "NVDA", "AAPL", "AMD", "META", "MSFT", "AMZN", "GOOGL",
             "PLTR", "MU", "COIN", "MSTR", "NFLX", "AVGO", "SMCI", "INTC",
             "HOOD", "UBER", "TSM", "ORCL", "QCOM", "BA", "SNOW", "BABA",
             "SPY", "QQQ", "IWM"],
    "rule1_min_ratio_pct": 10.0,     # options share-equivalents / shares volume
    "rule2_min_contracts": 100_000,  # avg daily options contracts
    "rule3_min_agg_oi": 500_000,     # aggregate OI, chains within 90d
    "trailing_sessions": 20,
    "provisional_min_samples": 8,    # adaptive depth, same pattern as gate I1
    "confirmed_min_samples": 20,
    "max_state_sessions": 30,        # prune history beyond this
}

_OCC_BASE = "https://marketdata.theocc.com"


def _occ_get(url, timeout=30):
    """GET an OCC endpoint. Returns (text, ok). Keyless by design."""
    import requests
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "phoenix-gex-universe/1.0"})
        if r.status_code == 200 and r.content:
            return r.content.decode("utf-8", errors="replace"), True
        return f"HTTP {r.status_code}", False
    except Exception as e:
        return f"error: {e}", False


def _parse_occ_volume_csv(text, symbol):
    """
    Parse an OCC volume-query CSV for one symbol's total options volume.
    Format is defensive: find numeric columns on rows mentioning the symbol
    or on total rows; sum call+put where identifiable, else take the largest
    plausible total. Returns int volume or None.
    """
    import csv as _csv
    import io
    best = None
    try:
        rows = list(_csv.reader(io.StringIO(text)))
    except Exception:
        return None
    for row in rows:
        joined = ",".join(row).upper()
        if symbol.upper() not in joined and "TOTAL" not in joined:
            continue
        nums = []
        for cell in row:
            c = cell.strip().replace(",", "")
            if c.replace(".", "").isdigit():
                try:
                    nums.append(int(float(c)))
                except ValueError:
                    pass
        if nums:
            cand = max(nums)
            if best is None or cand > best:
                best = cand
    return best


def fetch_occ_symbol_volume(symbol, report_date):
    """
    Try the documented OCC volume-query parameter patterns for one symbol/day.
    Returns (volume_int_or_None, pattern_used_or_error_head).
    """
    patterns = [
        # documented legacy grammar migrated to marketdata host
        (f"{_OCC_BASE}/volume-query?reportDate={report_date}&format=csv"
         f"&volumeQueryType=O&symbolType=O&symbol={symbol}&reportType=D"
         f"&accountType=ALL&productKind=ALL&porc=BOTH"),
        (f"{_OCC_BASE}/volume-query?reportDate={report_date}&format=csv"
         f"&volumeQueryType=O&symbolType=O&symbol={symbol}&reportType=D"
         f"&accountType=C&productKind=OSTK&porc=C"),
        (f"{_OCC_BASE}/volume-query?reportDate={report_date}&format=csv"
         f"&symbol={symbol}"),
    ]
    for u in patterns:
        text, ok = _occ_get(u)
        if not ok:
            continue
        vol = _parse_occ_volume_csv(text, symbol)
        if vol is not None and vol > 0:
            return vol, u.split("?")[1][:60]
    return None, (text[:150] if 'text' in dir() else "no response")


def _gexu_state_load():
    import os, json
    p = os.path.join(OUTPUTS_DIR, "gex_universe_state.json")
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            pass
    return {"samples": {}}   # {symbol: {date: opt_volume}}


def _gexu_state_save(state):
    import os, json
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    with open(os.path.join(OUTPUTS_DIR, "gex_universe_state.json"), "w") as f:
        json.dump(state, f, separators=(",", ":"))


def evaluate_gex_universe(samples, shares_avg, cfg=None, ibkr_seed=None):
    """
    PURE FUNCTION. Apply Rules 1-2 (5 pending per-run data; 3 pending OI feed)
    with adaptive data depth: provisional at >=8 samples, confirmed at >=20.
    samples:    {symbol: {date: opt_volume}} accumulated OCC readings
    shares_avg: {symbol: avg daily SHARES volume} from the committed weekly CSV
    ibkr_seed:  {symbol: avg_opt_contracts} manual in-chat IBKR measurements —
                used as the reading while OCC history is still shallow.
    Returns ranked list of dicts.
    """
    cfg = cfg or GEX_UNIVERSE
    out = []
    for sym in cfg["seed"]:
        s = samples.get(sym, {})
        vals = [v for _d, v in sorted(s.items())[-cfg["trailing_sessions"]:]
                if v is not None]
        n = len(vals)
        occ_avg = round(sum(vals) / n) if vals else None
        seed_avg = (ibkr_seed or {}).get(sym)
        # prefer OCC once it has enough depth; fall back to the IBKR seed
        if n >= cfg["provisional_min_samples"]:
            avg, src = occ_avg, f"OCC ({n} sessions)"
        elif seed_avg is not None:
            avg, src = seed_avg, "IBKR seed (in-chat 2026-07-20)"
        else:
            avg, src = occ_avg, f"OCC ({n} sessions, below provisional floor)"
        sh = shares_avg.get(sym)
        ratio = round(avg * 100 / sh * 100, 1) if (avg and sh) else None
        r1 = ratio is not None and ratio >= cfg["rule1_min_ratio_pct"]
        r2 = avg is not None and avg >= cfg["rule2_min_contracts"]
        depth = ("confirmed" if n >= cfg["confirmed_min_samples"] else
                 "provisional" if n >= cfg["provisional_min_samples"] else
                 "seed" if seed_avg is not None else "insufficient")
        out.append({
            "ticker": sym,
            "avg_opt_contracts_day": avg,
            "avg_daily_shares": round(sh) if sh else None,
            "opt_to_shares_ratio_pct": ratio,
            "rule1_ratio": bool(r1), "rule2_abs_volume": bool(r2),
            "rule3_agg_oi": "pending_oi_feed",
            "rule5_weeklies": "pending_check",
            "eligible_provisional": bool(r1 and r2),
            "occ_sessions": n, "data_depth": depth, "source": src,
        })
    out.sort(key=lambda r: -(r["opt_to_shares_ratio_pct"] or 0))
    for i, r in enumerate(out):
        r["rank"] = i + 1
    return out


def run_gex_universe():
    """
    Stage-0 daily accumulator + evaluator. Samples today's OCC volume for the
    seed list, appends to state, evaluates rules, writes gex_universe.json
    (guarded — a broken OCC day never blanks the file).
    """
    from datetime import date, timedelta
    cfg = GEX_UNIVERSE
    # report date: OCC publishes T+0 evening / T+1; ask for the last weekday
    d = date.today()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    rd = d.strftime("%Y%m%d")

    state = _gexu_state_load()
    samples = state.setdefault("samples", {})
    got, failed, pattern_note = 0, 0, None
    import time
    for sym in cfg["seed"]:
        if samples.get(sym, {}).get(rd) is not None:
            continue   # already sampled today
        vol, note = fetch_occ_symbol_volume(sym, rd)
        if vol is not None:
            samples.setdefault(sym, {})[rd] = vol
            got += 1
            if pattern_note is None:
                pattern_note = note
        else:
            failed += 1
            if failed == 1:
                print(f"[gexu] OCC parse failed for {sym}; response head: {note}")
        time.sleep(0.4)
    # prune
    for sym in list(samples):
        keep = sorted(samples[sym])[-cfg["max_state_sessions"]:]
        samples[sym] = {k: samples[sym][k] for k in keep}
    _gexu_state_save(state)
    if pattern_note:
        print(f"[gexu] OCC pattern OK: ...{pattern_note}")
    print(f"[gexu] sampled {got} symbols, {failed} failed ({rd})")

    # shares volume from committed weekly history (last 4 complete weeks / 5)
    weekly = load_weekly_from_csv()
    shares_avg = {}
    for sym in cfg["seed"]:
        bars = weekly.get(sym) or []
        wv = [v for (_d, _c, v) in bars[-5:-1] if v]
        if wv:
            shares_avg[sym] = sum(wv) / len(wv) / 5.0

    # IBKR seed readings from the committed file (in-chat measurements)
    ibkr_seed = {}
    import os, json
    p = os.path.join(OUTPUTS_DIR, "gex_universe.json")
    if os.path.exists(p):
        try:
            for r in json.load(open(p)).get("universe", []):
                if "IBKR" in str(r.get("source", "")) and r.get("avg_opt_contracts_day"):
                    ibkr_seed[r["ticker"]] = r["avg_opt_contracts_day"]
        except Exception:
            pass

    universe = evaluate_gex_universe(samples, shares_avg, cfg, ibkr_seed)
    payload = {
        "asof": _now(),
        "spec": "PHOENIX_REVIEW.md Part 4 E3b — Stage 0 (Coach's rules)",
        "notes": [
            "Rules 1-2 live (OCC accumulator with IBKR in-chat seed fallback)",
            "Rule 3 (agg 90d OI >= 500k) pending OCC OI-report integration",
            "Rule 4 (0DTE exclusion) pending — current volumes include 0DTE",
            "Rule 5 (weeklies) pending per-ticker check",
            "eligible_provisional = Rules 1+2 only",
        ],
        "universe": universe,
    }
    def _validate_gexu(pl):
        n = sum(1 for r in pl.get("universe", [])
                if r.get("avg_opt_contracts_day"))
        return [] if n >= 3 else [f"only {n} symbols with volume readings"]
    write_json_guarded("gex_universe", payload, _validate_gexu)
    elig = [r["ticker"] for r in universe if r["eligible_provisional"]]
    print(f"[gexu] {len(elig)} provisionally eligible: {', '.join(elig) or 'none'}")
    return payload



# ============================================================
# E3b STAGE 1 — PER-STOCK GEX for eligibility passers only
# Direct single-stock chains (scale=1, no proxy — the SPY x10 problem is
# SPX-specific; single-stock yfinance chains return real OI). Bounded to the
# Stage-0 eligible list so we never draw confident walls on thin chains.
# Same wall-selection rules as the index engine: tactical walls = NEAREST
# qualifying strike to spot, magnets separate, levels ordered by distance.
# Output: outputs/gex_stocks/TK.json (same schema as gex.json).
# ============================================================
GEX_STOCKS = {
    "max_expiries": 8,        # chains within ~90d; single names need fewer
    "otm_band": 0.20,         # wider than index: single stocks move more
    "min_strikes": 10,        # per-ticker degenerate floor (Rule-3 spirit)
    "min_total_oi": 20000,
    "min_put_oi": 500,
}


def fetch_stock_chain_yfinance(symbol):
    """Fetch a single stock's option chain (<=90d). Returns (chain, spot)."""
    import yfinance as yf
    from datetime import datetime, timedelta
    tk = yf.Ticker(symbol)
    spot = float(tk.history(period="1d")["Close"].iloc[-1])
    now = datetime.now()
    horizon = now + timedelta(days=90)
    exps = [e for e in tk.options
            if datetime.strptime(e, "%Y-%m-%d") <= horizon][:GEX_STOCKS["max_expiries"]]
    chain = []
    for exp in exps:
        T = max((datetime.strptime(exp, "%Y-%m-%d") - now).days, 1) / 365.0
        try:
            oc = tk.option_chain(exp)
        except Exception:
            continue
        for df, kind in [(oc.calls, "call"), (oc.puts, "put")]:
            for _, row in df.iterrows():
                chain.append({"strike": float(row["strike"]), "T_years": T,
                              "kind": kind,
                              "open_interest": row.get("openInterest"),
                              "iv": row.get("impliedVolatility")})
    return chain, spot


def run_gex_stocks(tickers=None):
    """Compute per-stock GEX for Stage-0 eligible names. Non-fatal per ticker."""
    import os, json, time
    if tickers is None:
        p = os.path.join(OUTPUTS_DIR, "gex_universe.json")
        if not os.path.exists(p):
            # fall back to the raw seed list so per-stock GEX works even before
            # the first Stage-0 accumulation run.
            uni = [{"ticker": t} for t in GEX_UNIVERSE["seed"]]
            print(f"[gexs] no gex_universe.json yet — using raw seed list "
                  f"({len(uni)} names)")
            p = None
        if p is not None:
            try:
                uni = json.load(open(p)).get("universe", [])
            except Exception as e:
                print(f"[gexs] gex_universe.json unreadable: {e}")
                return None
        # ALL seed names (2026-07-20): the histogram is a planning tool, so
        # compute it for every GEX-universe ticker with a fetchable chain, not
        # only the Rules 1-2 passers. Indices keep their own SPX/proxy path.
        tickers = [r["ticker"] for r in uni
                   if r["ticker"] not in ("SPY", "QQQ", "IWM")]
    if not tickers:
        print("[gexs] no GEX-universe tickers to compute")
        return None
    out_dir = os.path.join(OUTPUTS_DIR, "gex_stocks")
    os.makedirs(out_dir, exist_ok=True)
    # temporarily tighten engine guards to single-stock scale
    ok = 0
    cfg = GEX_STOCKS
    for sym in tickers:
        try:
            chain, spot = fetch_stock_chain_yfinance(sym)
            band = cfg["otm_band"]
            old_band = GEX["otm_band"]
            GEX["otm_band"] = band
            try:
                res = gex_engine(chain, spot, scale=1.0)
            finally:
                GEX["otm_band"] = old_band
            # per-ticker degenerate floors (thinner than index, but real)
            diag = res.get("diagnostics") or {}
            n_strikes = len(res.get("profile") or [])
            total_oi = sum((p.get("coi", 0) + p.get("poi", 0))
                           for p in (res.get("profile") or []))
            if res.get("error") or n_strikes < cfg["min_strikes"] or total_oi < cfg["min_total_oi"]:
                res["stock_validity"] = "INVALID_THIN_CHAIN"
                print(f"[gexs] {sym}: thin chain ({n_strikes} strikes, {int(total_oi)} OI) — flagged")
            else:
                res["stock_validity"] = "ok"
                ok += 1
            res["ticker"] = sym
            res["source"] = f"yfinance direct chain (scale=1, {cfg['max_expiries']} exp <=90d)"
            safe = sym.replace("/", "-").replace(".", "-")
            with open(os.path.join(out_dir, f"{safe}.json"), "w") as f:
                json.dump(res, f, separators=(",", ":"))
            time.sleep(0.6)
        except Exception as e:
            print(f"[gexs] {sym} failed: {e}")
    print(f"[gexs] wrote {ok} valid per-stock GEX files to outputs/gex_stocks/")
    return ok



# ============================================================
# VIX TERM STRUCTURE — automated (replaces the manual paste workflow)
# Tier 1: Yahoo's VIX index family — ^VIX9D (9-day), ^VIX (30-day),
# ^VIX3M, ^VIX6M — the same reliable index endpoint the macro pull uses.
# Not the full VX futures ladder, but a real 4-point term structure that
# carries the signal the tile exists for: contango vs backwardation and
# the front/back spread. Writes the EXACT schema the dashboard already
# reads ({spot, futures:[{label,value}]}), guarded so a bad pull keeps
# the last good file (or a manually pasted one). A manually uploaded
# vix_term.json with MORE points (true futures curve) is still valid —
# this only overwrites when it has fresh data of its own.
# Tier 2 (future): CBOE settlement CSVs for the true VX futures curve.
# ============================================================
def run_vix_term():
    try:
        import yfinance as yf
    except Exception as e:
        print(f"[vixterm] yfinance unavailable: {e}")
        return None
    series = [("^VIX9D", "9D"), ("^VIX", "30D"), ("^VIX3M", "3M"), ("^VIX6M", "6M")]
    vals = {}
    for sym, label in series:
        try:
            h = yf.Ticker(sym).history(period="5d")["Close"].dropna()
            if len(h):
                vals[label] = round(float(h.iloc[-1]), 2)
        except Exception:
            continue
    if "30D" not in vals or len(vals) < 3:
        print(f"[vixterm] insufficient pull ({list(vals)}) — keeping previous file")
        return None
    futures = [{"label": lb, "value": vals[lb]} for _s, lb in series if lb in vals]
    payload = {
        "asof": _now(),
        "source": "yahoo_vix_indices",
        "note": "4-point index term structure (9D/30D/3M/6M), auto-generated. "
                "A manually uploaded VX futures ladder can overwrite this file "
                "and survives failed pulls (guarded writes).",
        "spot": vals["30D"],
        "futures": futures,
    }
    def _validate(pl):
        return [] if len(pl.get("futures") or []) >= 3 else ["fewer than 3 curve points"]
    write_json_guarded("vix_term", payload, _validate)
    front, back = futures[0]["value"], futures[-1]["value"]
    shape = "CONTANGO" if back > front else ("BACKWARDATION" if back < front else "FLAT")
    pts = ", ".join(str(f["label"]) + " " + str(f["value"]) for f in futures)
    print(f"[vixterm] wrote vix_term.json: {pts} -> {shape}")
    return payload


# ============================================================
# SCHEDULER — orchestrates the daily run
# ============================================================

def run_detail_bundle():
    """
    THE guaranteed detail-page data source (2026-07-20 rebuild).

    Writes ONE file — outputs/detail_bundle.json — containing, for every
    GEX-universe + pinned ticker, everything the ticker detail page needs:
      - financials: last 8 quarters straight from the committed CSV (NO network)
      - ratings + earnings + profile: from Yahoo, but only for this small set
        (~24 seed names), so it never hits the per-ticker throttle that limits
        the full run_research pass.

    This runs UNCONDITIONALLY in the pipeline — it does not depend on the OHLCV
    pull (which can be skipped when the market is closed, stranding chart-file
    embeds), and it is not behind the write_financials hash gate. If Yahoo
    throttles, financials still populate from the CSV. This is the file the
    dashboard reads FIRST for financials/ratings/earnings.
    """
    import os, json
    # who to cover: GEX universe seed + pinned trades (the names actually opened)
    want = set(GEX_UNIVERSE["seed"]) | _pinned_tickers()
    want.discard("SPY"); want.discard("QQQ"); want.discard("IWM")
    want = sorted(want)

    # financials from the committed CSV — always available, no network
    quarterly = load_quarterly_fundamentals(FUND_CSV) if os.path.exists(FUND_CSV) else {}
    universe = load_universe_from_csv()

    bundle = {}
    for tk in want:
        entry = {"quarters": [], "pe": None, "ratings": None,
                 "earnings": {}, "profile": {}}
        qs = quarterly.get(tk)
        if qs:
            entry["quarters"] = qs[-8:]
            u = universe.get(tk) or {}
            mc = u.get("market_cap")
            if u:
                entry["profile"] = {"name": u.get("name"), "sector": u.get("sector"),
                                    "industry": u.get("industry")}
            if mc:
                nis = [q.get("net_income_B") for q in qs[-4:]
                       if q.get("net_income_B") is not None]
                if len(nis) == 4 and sum(nis) > 0:
                    entry["pe"] = round(mc / 1e9 / sum(nis), 1)
        bundle[tk] = entry

    # ratings/earnings/profile from Yahoo — small set, paced
    try:
        import yfinance as yf
        import time
        got = 0
        for tk in want:
            try:
                t = yf.Ticker(tk)
                info = {}
                try:
                    info = t.info or {}
                except Exception:
                    info = {}
                if info:
                    prof = bundle[tk]["profile"]
                    prof["name"] = info.get("longName") or info.get("shortName") or prof.get("name") or tk
                    prof["exchange"] = info.get("exchange")
                    prof["summary"] = info.get("longBusinessSummary")
                    prof["employees"] = info.get("fullTimeEmployees")
                    prof["div_yield"] = info.get("dividendYield")
                    prof["recommendation"] = info.get("recommendationKey")
                    prof["target_mean"] = info.get("targetMeanPrice")
                    prof["forward_pe"] = info.get("forwardPE")
                    bundle[tk]["quote"] = {
                        "last": info.get("currentPrice") or info.get("regularMarketPrice"),
                        "prev": info.get("previousClose") or info.get("regularMarketPreviousClose"),
                        "mcap_B": round(info.get("marketCap") / 1e9, 2) if info.get("marketCap") else None,
                    }
                    if info.get("trailingPE"):
                        bundle[tk]["pe"] = round(info["trailingPE"], 1)
                    got += 1
                try:
                    rec = t.recommendations
                    if rec is not None and len(rec) > 0:
                        r = rec.iloc[0]
                        bundle[tk]["ratings"] = {
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
                            bundle[tk]["earnings"]["next_date"] = ed.strftime("%Y-%m-%d") if hasattr(ed, "strftime") else str(ed)[:10]
                except Exception:
                    pass
                time.sleep(0.5)
            except Exception:
                continue
        print(f"[detail] Yahoo enrich: {got}/{len(want)} with info")
    except Exception as e:
        print(f"[detail] Yahoo enrich skipped ({e}) — financials still from CSV")

    n_fin = sum(1 for v in bundle.values() if v["quarters"])
    n_rat = sum(1 for v in bundle.values() if v["ratings"])
    write_json("detail_bundle", {"asof": _now(), "count": len(bundle),
                                 "tickers": bundle})
    print(f"[detail] detail_bundle.json: {len(bundle)} tickers, "
          f"{n_fin} with financials, {n_rat} with ratings")
    return bundle



def run_financials_all():
    """
    Write outputs/financials_all.json — EVERY ticker's financials + estimated
    next-earnings, straight from the committed CSV. ZERO network. This is the
    universe-wide source the detail page reads first, so financials + earnings
    work for ANY ticker, not just GEX names. Regenerated only when the CSV
    changes (quarterly), so it's cheap to run every pipeline.
    """
    import os, json
    from datetime import datetime, timedelta
    if not os.path.exists(FUND_CSV):
        print("[finall] no fundamentals CSV — skipping")
        return None
    quarterly = load_quarterly_fundamentals(FUND_CSV)
    universe = load_universe_from_csv()

    def est_ne(qs):
        if not qs:
            return None
        try:
            d = datetime.strptime(qs[-1]["q"], "%Y-%m-%d") + timedelta(days=118)
            while d < datetime.now():
                d += timedelta(days=91)
            return d.strftime("%Y-%m-%d")
        except Exception:
            return None

    allfin = {}
    for tk, qs in quarterly.items():
        if not qs:
            continue
        u = universe.get(tk) or {}
        mc = u.get("market_cap")
        e = {"quarters": qs[-8:], "pe": None, "earnings": {},
             "profile": {"name": u.get("name") or tk, "sector": u.get("sector"),
                         "industry": u.get("industry")}}
        if mc:
            nis = [x.get("net_income_B") for x in qs[-4:]
                   if x.get("net_income_B") is not None]
            if len(nis) == 4 and sum(nis) > 0:
                e["pe"] = round(mc / 1e9 / sum(nis), 1)
        ne = est_ne(qs)
        if ne:
            e["earnings"] = {"next_date": ne, "estimated": True}
        allfin[tk] = e
    write_json("financials_all", {"asof": _now(), "count": len(allfin),
                                  "tickers": allfin})
    print(f"[finall] financials_all.json: {len(allfin)} tickers (CSV, no network)")
    return allfin


def run_ratings_all(limit=None):
    """
    Write outputs/ratings_all.json — analyst ratings across the universe from
    Yahoo, largest market caps first (the names most likely to be viewed), so
    partial/throttled runs still cover the most-viewed tickers. Accumulates:
    keeps any ticker already fetched, adds/updates as it goes. Runs weekly.
    """
    import os, json, time
    try:
        import yfinance as yf
    except Exception as e:
        print(f"[ratall] yfinance unavailable: {e}")
        return None
    from datetime import datetime, timedelta
    universe = load_universe_from_csv()
    # accumulate: keep prior ratings + per-ticker fetch dates
    out, fetched = {}, {}
    p = os.path.join(OUTPUTS_DIR, "ratings_all.json")
    if os.path.exists(p):
        try:
            prev = json.load(open(p))
            out = prev.get("tickers", {})
            fetched = prev.get("_fetched", {})
        except Exception:
            out, fetched = {}, {}
    # INCREMENTAL (fix 2026-07-22): mcap-ordered, but SKIP names fetched within
    # RATINGS_FRESH_DAYS. Each run advances to new/stale names instead of
    # re-hitting the same top caps, so the whole universe gets covered over a
    # few runs and daily Yahoo load stays low afterward.
    today = datetime.utcnow().strftime("%Y-%m-%d")
    fresh_cutoff = (datetime.utcnow() - timedelta(days=RATINGS_FRESH_DAYS)).strftime("%Y-%m-%d")
    ranked = sorted(universe.keys(), key=lambda t: -(universe[t].get("market_cap") or 0))
    stale = [t for t in ranked if fetched.get(t, "0000-00-00") < fresh_cutoff]
    order = stale[:limit] if limit else stale
    if not order:
        print(f"[ratall] all {len(ranked)} tickers fresh (<{RATINGS_FRESH_DAYS}d) — nothing to fetch")
        return out
    fresh_n = len(ranked) - len(stale)
    deferred = len(stale) - len(order)
    print(f"[ratall] fetching {len(order)} this run | {fresh_n} fresh (skipped) | "
          f"{deferred} deferred to next run")
    got = 0
    for idx, tk in enumerate(order, 1):
        try:
            t = yf.Ticker(tk)
            info = {}
            try:
                info = t.info or {}
            except Exception:
                info = {}
            rec = None
            try:
                rr = t.recommendations
                if rr is not None and len(rr) > 0:
                    r0 = rr.iloc[0]
                    sb = int(r0.get("strongBuy", 0) or 0)
                    bu = int(r0.get("buy", 0) or 0)
                    ho = int(r0.get("hold", 0) or 0)
                    se = int(r0.get("sell", 0) or 0)
                    ss = int(r0.get("strongSell", 0) or 0)
                    tot = sb + bu + ho + se + ss
                    if tot > 0:
                        rec = {"strong_buy": sb, "buy": bu, "hold": ho,
                               "sell": se, "strong_sell": ss}
            except Exception:
                pass
            tgt = info.get("targetMeanPrice")
            if rec is not None:
                if tgt:
                    rec["mean_target"] = tgt
                out[tk] = rec
                got += 1
            elif info.get("numberOfAnalystOpinions"):
                # aggregate fallback
                out[tk] = {"n_analysts": info.get("numberOfAnalystOpinions"),
                           "buy_pct": None, "mean_target": tgt}
                got += 1
            fetched[tk] = today   # stamp so we don't re-fetch this name daily
            if idx % 50 == 0:
                print(f"[ratall]   {idx}/{len(order)} scanned, {got} new/updated")
                write_json("ratings_all", {"asof": _now(), "count": len(out),
                                           "tickers": out, "_fetched": fetched})
            time.sleep(0.5)
        except Exception:
            continue
    write_json("ratings_all", {"asof": _now(), "count": len(out),
                               "tickers": out, "_fetched": fetched})
    print(f"[ratall] ratings_all.json: {len(out)} tickers total, {got} this run")
    return out



def parse_gex_briefing(path):
    """
    Parse a Market Maker Edge GEX Daily Briefing (PDF or the zip-of-pages
    variant) into the real SPX GEX numbers. The briefing is built on REAL SPX
    option chains, so its flip / net-GEX sign / walls are ground truth — unlike
    the SPY x10 proxy, which structurally underweights institutional put
    hedging and pushes the computed flip below spot (wrong regime).

    Returns a dict matching the gex.json schema, or None if parsing fails.
    """
    import subprocess, zipfile, re, os
    if not os.path.exists(path):
        print(f"[gexbrief] not found: {path}")
        return None
    # extract text (pages 1-4 carry the overview + strength tables)
    txt = ""
    r = subprocess.run(["pdftotext", "-layout", "-f", "1", "-l", "4", path, "-"],
                       capture_output=True, text=True)
    if r.returncode == 0 and len(r.stdout) > 200:
        txt = r.stdout
    else:
        try:
            z = zipfile.ZipFile(path)
            for n in ["1.txt", "2.txt", "3.txt", "4.txt"]:
                if n in z.namelist():
                    txt += z.read(n).decode(errors="replace") + "\n"
        except Exception as e:
            print(f"[gexbrief] cannot read {path}: {e}")
            return None

    def num(pat, cast=float):
        m = re.search(pat, txt)
        return cast(m.group(1).replace(",", "")) if m else None

    spot = num(r"Gamma\s+\$([\d,]+\.\d+)") or num(r"SPX SPOT[\s\S]{0,120}?\$([\d,]+\.\d+)")
    m = re.search(r"Gamma\s+\$([\d,]+\.\d+)\s+\$([+-][\d.]+)B", txt)
    net_gex = (float(m.group(2)) if m else num(r"NET GEX[\s\S]{0,250}?\$([+-][\d.]+)B") or num(r"\$([+-]\d+\.\d+)B"))
    flip = num(r"GAMMA FLIP[\s\S]{0,80}?\$([\d,]+\.\d+)") or num(r"\$([\d,]+\.\d+)\s+[+-]?[\d.]+%")
    vix = num(r"VIX[\s\S]{0,90}?[+-]?[\d.]+%\s+([\d.]+)") or num(r"VIX[\s\S]{0,40}?([\d]{1,2}\.[\d])")
    gm = re.search(r"\$([\d.]+)B\s+(?:BUY|SELL)[^$]*\$([\d.]+)B\s+(?:BUY|SELL)[^$]*\$([\d.]+)B", txt)
    vanna = float(gm.group(2)) if gm else None
    charm = float(gm.group(3)) if gm else None

    if spot is None or net_gex is None or flip is None:
        print(f"[gexbrief] missing key fields (spot={spot} net={net_gex} flip={flip})")
        return None

    # strength tables: rows of  TIER  STRIKE  $±X.XXB  OI  ... 
    def parse_rows(section_txt):
        rows = []
        for mm in re.finditer(r"(?:T\d)\s+([\d,]+)\s+\$([+-][\d.]+)B[^\n]*?([\d,]{4,})", section_txt):
            K = float(mm.group(1).replace(",", ""))
            g = float(mm.group(2))
            oi = int(mm.group(3).replace(",", ""))
            rows.append((K, g, oi))
        return rows

    sup_txt = ""
    res_txt = ""
    ms = re.search(r"OI SUPPORT([\s\S]*?)OI RESISTANCE", txt)
    if ms:
        sup_txt = ms.group(1)
    mr = re.search(r"OI RESISTANCE \+ OVERHEAD([\s\S]*?)(?:Market Maker|DEALER FLOW|$)", txt)
    if mr:
        res_txt = mr.group(1)
    support = sorted(parse_rows(sup_txt), key=lambda x: -x[0])   # nearest-below first later
    resist = sorted(parse_rows(res_txt), key=lambda x: x[0])

    def lvl(K, g, oi, below):
        return {"strike": float(K), "net_gex_B": round(g, 3),
                "coi": 0 if below else int(oi), "poi": int(oi) if below else 0}

    supports = [lvl(K, g, oi, True) for K, g, oi in
                sorted(support, key=lambda x: -(x[0]))]  # highest (nearest) -> lowest
    resistances = [lvl(K, g, oi, False) for K, g, oi in
                   sorted(resist, key=lambda x: x[0])]     # lowest (nearest) -> highest
    prof = sorted(
        [lvl(K, g, oi, True) for K, g, oi in support] +
        [lvl(K, g, oi, False) for K, g, oi in resist],
        key=lambda p: p["strike"])

    put_wall = supports[0] if supports else None
    call_wall = resistances[0] if resistances else None
    put_mag = max(support, key=lambda x: x[2]) if support else None
    call_mag = max(resist, key=lambda x: x[2]) if resist else None

    return {
        "asof": _now(),
        "source": f"Market Maker Edge briefing (real SPX chains) — {os.path.basename(path)}",
        "overview": {
            "spx_spot": round(spot, 2), "net_gex_B": round(net_gex, 2),
            "regime": "Positive Gamma" if net_gex > 0 else "Negative Gamma",
            "gamma_flip": round(flip, 2),
            "dist_to_flip_pct": round((flip / spot - 1) * 100, 2),
            "net_vanna_B_per_volpt": vanna, "net_charm_B_per_day": charm, "vix": vix,
        },
        "raw": {"net_gex_B": round(net_gex, 2), "net_vanna_B": vanna,
                "net_charm_B": charm, "calibrated": True},
        "levels": {
            "pin": put_wall, "call_wall": call_wall, "put_wall": put_wall,
            "gamma_flip": round(flip, 2), "supports": supports, "resistances": resistances,
            "magnets": {"put": lvl(*put_mag, True) if put_mag else None,
                        "call": lvl(*call_mag, False) if call_mag else None},
            "wall_threshold": 0.30,
        },
        "profile": prof,
        "confidence": {"levels": "high", "regime_sign": "high",
            "note": "Real SPX chains via professional briefing (not the SPY proxy). "
                    "Flip, net-GEX sign, and walls are ground truth."},
    }


def run_gex_from_briefing(path=None):
    """
    Write gex.json from a briefing PDF instead of the SPY proxy. If no path is
    given, use the newest PDF in ./briefings/. This is the SPX GEX source of
    record; the SPY proxy (run_gex) is a fallback only.
    """
    import os, glob, re
    from datetime import datetime
    if path is None:
        cands = glob.glob("briefings/*.pdf") + glob.glob("briefings/*.PDF")
        if not cands:
            print("[gexbrief] no briefing in ./briefings/ — falling back to proxy")
            return run_gex()
        # pick the LATEST by date parsed from the filename (mtime is unreliable
        # after actions/checkout resets it). Handles "..July_21_2026.pdf" and
        # ISO "..2026-07-21.pdf"; falls back to mtime if neither parses.
        MONTHS = {m: i for i, m in enumerate(
            ["january","february","march","april","may","june","july","august",
             "september","october","november","december"], 1)}
        def file_date(fp):
            name = os.path.basename(fp).lower()
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", name)
            if m:
                return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            m = re.search(r"(" + "|".join(MONTHS) + r")[_\s-]+(\d{1,2})[_\s-]+(\d{4})", name)
            if m:
                return (int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))
            return (0, 0, 0)
        dated = [(file_date(f), f) for f in cands]
        # if no filename parsed a date, fall back to mtime ordering
        if all(d == (0, 0, 0) for d, _ in dated):
            path = sorted(cands, key=os.path.getmtime, reverse=True)[0]
        else:
            path = sorted(dated, reverse=True)[0][1]
        print(f"[gexbrief] {len(cands)} briefing(s) found; using {os.path.basename(path)}")
    data = parse_gex_briefing(path)
    if not data:
        print("[gexbrief] parse failed — keeping existing gex.json")
        return None
    write_json("gex", data)
    ov = data["overview"]
    print(f"[gexbrief] gex.json from briefing: {ov['regime']}, net {ov['net_gex_B']}B, "
          f"flip {ov['gamma_flip']} ({ov['dist_to_flip_pct']}% from spot)")
    return data


# ============================================================
# SMART MONEY — congressional (PTR) + institutional (13F) activity
# Two public, legal disclosure regimes that both answer "who with an edge is
# positioned in this ticker, and when":
#   - Congress: Periodic Transaction Reports (STOCK Act 2012), 45-day lag,
#     amounts as dollar RANGES. Individual conviction.
#   - Institutions: Form 13F-HR (managers >$100M), quarterly HOLDINGS snapshots
#     (45 days after quarter-end), so buys/sells = quarter-over-quarter deltas.
#     Big flows.
# Both are inherently STALE by design — this is a pattern/context layer, never a
# real-time signal (and legally cannot be).
# ============================================================
SMART_MONEY = {
    # Free aggregated PTR feeds (static JSON). Tried in order; first that parses
    # wins. These mirror the official House Clerk + Senate eFD data.
    "congress_feeds": [
        "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json",
        "https://raw.githubusercontent.com/timothycarambat/house-stock-watcher-data/master/data/all_transactions.json",
    ],
    # Curated institutional managers (name -> SEC CIK). Edit freely. CIKs are the
    # stable SEC identifier; edgartools resolves the latest 13F-HR from them.
    "managers": {
        "Berkshire Hathaway (Buffett)": 1067983,
        "Scion Asset Mgmt (Burry)": 1649339,
        "Pershing Square (Ackman)": 1336528,
        "Bridgewater Associates": 1350694,
        "Citadel Advisors": 1423053,
        "Renaissance Technologies": 1037389,
        "Third Point (Loeb)": 1040273,
        "Greenlight Capital (Einhorn)": 1079114,
        "Icahn Capital": 921669,
        "Tiger Global": 1167483,
    },
    "congress_lookback_days": 400,   # how far back to keep PTR trades
    "min_trade_value_hint": 1000,    # STOCK Act threshold
}

# Per-daily-run cap on the Yahoo ratings fetch (largest caps first). Names are
# skipped for RATINGS_FRESH_DAYS after a successful fetch, so the run advances
# through the universe and daily load drops once coverage is built.
RATINGS_DAILY_CAP = 400
RATINGS_FRESH_DAYS = 10


def _norm_name(s):
    import re
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def run_congress_trades():
    """
    Fetch congressional PTR trades from a free aggregated feed, keep those whose
    ticker is in our universe (and recent), and write congress_trades.json
    grouped by ticker. Non-fatal on any network/parse failure.
    """
    import json, os, requests
    from datetime import datetime, timedelta

    universe = load_universe_from_csv()
    uni_tickers = set(universe.keys())
    cutoff = datetime.now() - timedelta(days=SMART_MONEY["congress_lookback_days"])

    raw = None
    for url in SMART_MONEY["congress_feeds"]:
        try:
            r = requests.get(url, timeout=45,
                             headers={"User-Agent": "phoenix-smartmoney/1.0"})
            if r.status_code == 200 and r.content:
                raw = r.json()
                print(f"[congress] fetched {len(raw)} filer-records from {url.split('/')[-1]}")
                break
        except Exception as e:
            print(f"[congress] feed failed ({url.split('/')[-1]}): {e}")
    if raw is None:
        print("[congress] no feed reachable — keeping existing file")
        return None

    def parse_date(s):
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
        return None

    by_ticker = {}
    n_trades = 0
    for rec in raw:
        member = (rec.get("office") or
                  f"{rec.get('first_name','')} {rec.get('last_name','')}").strip()
        chamber = "Senate" if "senator" in member.lower() else "House"
        for tx in (rec.get("transactions") or []):
            tk = (tx.get("ticker") or "").strip().upper()
            if not tk or tk in ("--", "N/A") or tk not in uni_tickers:
                continue
            td = parse_date(tx.get("transaction_date") or "")
            if td and td < cutoff:
                continue
            typ = (tx.get("type") or "").lower()
            side = ("buy" if "purchase" in typ or typ == "buy" else
                    "sell" if "sale" in typ or typ == "sell" else "exchange")
            by_ticker.setdefault(tk, []).append({
                "member": member, "chamber": chamber,
                "date": td.strftime("%Y-%m-%d") if td else (tx.get("transaction_date") or ""),
                "reported": rec.get("date_recieved") or "",
                "side": side, "amount": tx.get("amount") or "",
                "owner": tx.get("owner") or "",
                "asset_type": tx.get("asset_type") or "Stock",
            })
            n_trades += 1
    # newest first per ticker
    for tk in by_ticker:
        by_ticker[tk].sort(key=lambda x: x["date"], reverse=True)
    payload = {"asof": _now(), "source": "STOCK Act PTR (aggregated public feed)",
               "note": "45-day disclosure lag; amounts are ranges as filed.",
               "ticker_count": len(by_ticker), "trade_count": n_trades,
               "tickers": by_ticker}
    write_json("congress_trades", payload)
    print(f"[congress] congress_trades.json: {n_trades} trades across {len(by_ticker)} universe tickers")
    return payload


def _cusip_ticker_from_universe(issuer_name, universe_by_name):
    """Best-effort issuer-name -> ticker match against our universe."""
    key = _norm_name(issuer_name)
    if not key:
        return None
    # exact, then prefix
    if key in universe_by_name:
        return universe_by_name[key]
    for nm, tk in universe_by_name.items():
        if nm and (nm.startswith(key) or key.startswith(nm)) and len(nm) > 4:
            return tk
    return None


def run_institutional_13f(identity=None):
    """
    Pull the latest two 13F-HR filings per curated manager via edgartools,
    diff them per holding to get quarter-over-quarter position CHANGES (new /
    added / trimmed / exited), map to tickers, and write
    institutional_holdings.json grouped by ticker.

    edgartools is a free, no-API-key SEC EDGAR client. SEC's fair-access policy
    requires a User-Agent identity (a real contact). Set the SEC_IDENTITY env
    var to your email (e.g. in the workflow) — otherwise a generic default is
    used, which SEC may throttle. Non-fatal on any failure.
    """
    import json, os
    identity = identity or os.environ.get("SEC_IDENTITY") or "Phoenix Research phoenix@example.com"
    try:
        from edgar import Company, set_identity
        set_identity(identity)
    except Exception as e:
        print(f"[13f] edgartools unavailable ({e}) — keeping existing file. "
              f"Add 'edgartools' to the workflow's pip install.")
        return None

    universe = load_universe_from_csv()
    universe_by_name = {_norm_name(v.get("name")): tk
                        for tk, v in universe.items() if v.get("name")}

    by_ticker = {}
    managers_done = 0
    for mgr, cik in SMART_MONEY["managers"].items():
        try:
            filings = Company(cik).get_filings(form="13F-HR")
            objs = []
            for f in list(filings)[:2]:      # latest two quarters
                try:
                    objs.append(f.obj())
                except Exception:
                    continue
            if not objs:
                continue

            def holdings_map(obj):
                """{ticker_or_cusip: {shares, value, ticker, name}} for one 13F."""
                out = {}
                df = getattr(obj, "holdings", None)
                if df is None:
                    return out
                rows = df.to_dict("records") if hasattr(df, "to_dict") else df
                for row in rows:
                    cusip = str(row.get("Cusip") or row.get("cusip") or "").strip()
                    name = row.get("Issuer") or row.get("issuer") or row.get("nameOfIssuer") or ""
                    tk = (row.get("Ticker") or row.get("ticker") or "").strip().upper()
                    if not tk:
                        tk = _cusip_ticker_from_universe(name, universe_by_name) or ""
                    shares = row.get("Shares") or row.get("shares") or row.get("sshPrnamt") or 0
                    value = row.get("Value") or row.get("value") or 0
                    try:
                        shares = float(shares); value = float(value)
                    except Exception:
                        shares, value = 0, 0
                    keyid = tk or cusip
                    if not keyid:
                        continue
                    cur = out.setdefault(keyid, {"shares": 0, "value": 0,
                                                 "ticker": tk, "name": name})
                    cur["shares"] += shares
                    cur["value"] += value
                return out

            cur = holdings_map(objs[0])
            prev = holdings_map(objs[1]) if len(objs) > 1 else {}
            # filing/report date for timeline placement
            qdate = None
            try:
                per = getattr(objs[0], "report_period", None) or getattr(objs[0], "period_of_report", None)
                qdate = str(per)[:10] if per else None
            except Exception:
                qdate = None
            for keyid, h in cur.items():
                tk = h["ticker"]
                if not tk or tk not in universe:
                    continue
                p = prev.get(keyid, {"shares": 0})
                dsh = h["shares"] - p["shares"]
                if p["shares"] == 0 and h["shares"] > 0:
                    action = "NEW"
                elif h["shares"] > p["shares"]:
                    action = "ADD"
                elif h["shares"] < p["shares"]:
                    action = "TRIM"
                else:
                    action = "HOLD"
                by_ticker.setdefault(tk, []).append({
                    "manager": mgr, "action": action,
                    "shares": int(h["shares"]), "value_usd": int(h["value"]),
                    "shares_delta": int(dsh),
                    "pct_change": (round(dsh / p["shares"] * 100, 1)
                                   if p["shares"] else None),
                    "quarter": qdate,
                })
            # exits: in prev, gone in cur
            for keyid, p in prev.items():
                if keyid not in cur and p.get("ticker") and p["ticker"] in universe:
                    by_ticker.setdefault(p["ticker"], []).append({
                        "manager": mgr, "action": "EXIT",
                        "shares": 0, "value_usd": 0,
                        "shares_delta": -int(p["shares"]), "pct_change": -100.0,
                        "quarter": qdate,
                    })
            managers_done += 1
        except Exception as e:
            print(f"[13f] {mgr} (CIK {cik}) failed: {e}")
            continue

    # sort each ticker's managers by |value| then action priority
    prio = {"NEW": 0, "ADD": 1, "TRIM": 2, "EXIT": 3, "HOLD": 4}
    for tk in by_ticker:
        by_ticker[tk].sort(key=lambda x: (prio.get(x["action"], 9), -x["value_usd"]))
    payload = {"asof": _now(),
               "source": "SEC Form 13F-HR (quarterly institutional holdings)",
               "note": "Quarter-over-quarter deltas; ~45-day lag after quarter-end.",
               "managers_tracked": len(SMART_MONEY["managers"]),
               "managers_fetched": managers_done,
               "ticker_count": len(by_ticker), "tickers": by_ticker}
    write_json("institutional_holdings", payload)
    print(f"[13f] institutional_holdings.json: {len(by_ticker)} tickers "
          f"from {managers_done}/{len(SMART_MONEY['managers'])} managers")
    return payload



def backtest_smart_money(windows=(30, 90, 180)):
    """
    Measure whether disclosed smart-money BUYING precedes gains — the honest way:
    forward return vs an equal-weight market benchmark (alpha), not raw "did it
    go up" (in a bull market everything does). Reads congress_trades.json +
    institutional_holdings.json + the committed weekly price CSV. Writes
    smart_money_backtest.json and prints a summary. Runs on the Action (all
    committed data, no network).

    Reports per window and per actor bucket: n, avg raw return, avg ALPHA,
    % positive, % that beat the market. Congressional buys use real trade
    dates; institutional adds use the filing quarter date (coarser).
    """
    import csv, json, os
    from datetime import datetime, timedelta
    from collections import defaultdict

    # prices
    price_csv = None
    for c in ("macroflow_prices_weekly.csv", "stock_weekly.csv", "stock_weekly_2.csv"):
        if os.path.exists(c):
            price_csv = c; break
    if not price_csv:
        print("[backtest] no weekly price CSV found"); return None
    prices = defaultdict(list)
    with open(price_csv) as f:
        for r in csv.DictReader(f):
            try:
                prices[r["ticker"]].append((r["date"], float(r["close"])))
            except Exception:
                pass
    for t in prices:
        prices[t].sort()

    def px_on_after(tk, ds):
        try:
            d0 = datetime.strptime(ds, "%Y-%m-%d")
        except Exception:
            return None
        for dd, c in prices.get(tk, []):
            if datetime.strptime(dd, "%Y-%m-%d") >= d0:
                return (dd, c)
        return None

    def fwd(tk, ds, days):
        p0 = px_on_after(tk, ds)
        if not p0:
            return None
        tgt = (datetime.strptime(p0[0], "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
        p1 = px_on_after(tk, tgt)
        return (p1[1] / p0[1] - 1) * 100 if p1 else None

    bench = [t for t in prices if len(prices[t]) >= 200]
    _mkt_cache = {}
    def mkt(ds, days):
        k = (ds, days)
        if k in _mkt_cache:
            return _mkt_cache[k]
        rs = [fwd(t, ds, days) for t in bench]
        rs = [x for x in rs if x is not None]
        v = sum(rs) / len(rs) if rs else None
        _mkt_cache[k] = v
        return v

    # collect events: (bucket, ticker, date, is_buy, actor)
    events = []
    p = os.path.join(OUTPUTS_DIR, "congress_trades.json")
    if os.path.exists(p):
        for tk, lst in json.load(open(p)).get("tickers", {}).items():
            for t in lst:
                if t.get("side") in ("buy", "sell") and t.get("date"):
                    events.append(("congress_" + t["side"], tk, t["date"],
                                   t["side"] == "buy", t.get("member", "?")))
    p = os.path.join(OUTPUTS_DIR, "institutional_holdings.json")
    if os.path.exists(p):
        for tk, lst in json.load(open(p)).get("tickers", {}).items():
            for h in lst:
                if h.get("quarter"):
                    buy = h.get("action") in ("NEW", "ADD")
                    events.append(("inst_" + ("add" if buy else "trim"), tk,
                                   h["quarter"], buy, h.get("manager", "?")))

    # index benchmarks: use SPY / QQQ from the price CSV if present (add those
    # rows to the weekly CSV to get true SPX/Nasdaq), else the equal-weight
    # universe is the market proxy. Absolute return is reported either way.
    def index_fwd(sym, ds, days):
        return fwd(sym, ds, days) if sym in prices else None

    # per-actor breakout (buys only — "who actually drove the performance")
    actor_buys = defaultdict(lambda: {w: [] for w in windows})
    for bucket, tk, ds, is_buy, actor in events:
        if is_buy:
            for w in windows:
                r = fwd(tk, ds, w)
                if r is not None:
                    actor_buys[actor][w].append(r)

    buckets = defaultdict(lambda: {w: {"ret": [], "alpha": []} for w in windows})
    for bucket, tk, ds, is_buy, actor in events:
        for w in windows:
            r = fwd(tk, ds, w); m = mkt(ds, w)
            if r is not None and m is not None:
                buckets[bucket][w]["ret"].append(r)
                buckets[bucket][w]["alpha"].append(r - m)

    report = {"asof": _now(), "price_source": price_csv, "n_events": len(events),
              "windows": list(windows), "buckets": {}}
    print(f"\n=== SMART MONEY BACKTEST ({len(events)} events, benchmark = equal-weight universe) ===")
    for bucket in sorted(buckets):
        report["buckets"][bucket] = {}
        print(f"\n{bucket}:")
        for w in windows:
            ret = buckets[bucket][w]["ret"]; al = buckets[bucket][w]["alpha"]
            if not ret:
                continue
            avg = sum(ret) / len(ret); ava = sum(al) / len(al)
            hit = sum(1 for x in ret if x > 0) / len(ret) * 100
            beat = sum(1 for x in al if x > 0) / len(al) * 100
            report["buckets"][bucket][f"{w}d"] = {
                "n": len(ret), "avg_return": round(avg, 2), "avg_alpha": round(ava, 2),
                "pct_positive": round(hit, 1), "pct_beat_market": round(beat, 1)}
            flag = " [n<30: not significant]" if len(ret) < 30 else ""
            print(f"  +{w:3d}d: n={len(ret):4d} | avg {avg:+.1f}% | ALPHA {ava:+.1f}% | "
                  f"{hit:.0f}% up, {beat:.0f}% beat mkt{flag}")
    # per-actor buy performance (absolute return first — what you asked for)
    report["by_actor"] = {}
    actor_rows = []
    for actor, wd in actor_buys.items():
        n_any = max((len(wd[w]) for w in windows), default=0)
        if n_any < 2:
            continue
        row = {"actor": actor, "n_buys": n_any}
        for w in windows:
            v = wd[w]
            if v:
                row[f"avg_{w}d"] = round(sum(v) / len(v), 1)
                row[f"n_{w}d"] = len(v)
        actor_rows.append(row)
        report["by_actor"][actor] = row
    actor_rows.sort(key=lambda r: -(r.get(f"avg_{windows[-1]}d", r.get(f"avg_{windows[0]}d", -999)) or -999))
    if actor_rows:
        print("\n=== PER-ACTOR BUY PERFORMANCE (absolute avg return) ===")
        hdr = "  " + "actor".ljust(26) + "buys  " + "  ".join(f"+{w}d".rjust(7) for w in windows)
        print(hdr)
        for r in actor_rows:
            line = "  " + str(r["actor"])[:24].ljust(26) + str(r["n_buys"]).rjust(4) + "  "
            line += "  ".join((f"{r.get(f'avg_{w}d'):+.0f}%".rjust(7) if r.get(f'avg_{w}d') is not None else "n/a".rjust(7)) for w in windows)
            print(line)

    # note which benchmarks were available
    report["benchmarks"] = {"SPY": "SPY" in prices, "QQQ": "QQQ" in prices,
                            "equal_weight_universe": True}
    write_json("smart_money_backtest", report)
    return report


def run_full():
    print("=== Phoenix full run ===")
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
    # VIX term structure — automated (was the last manual-paste dependency)
    try:
        run_vix_term()
    except Exception as e:
        warnings.append(f"vix_term failed: {e}")
        print(f"[vixterm] FAILED (non-fatal): {e}")
    try:
        # briefing-aware: parses briefings/*.pdf (real SPX chains) if present,
        # otherwise falls back to the SPY x10 proxy. Keeps gex.json correct even
        # when the daily run happens after a briefing was committed.
        run_gex_from_briefing()
    except Exception as e:
        warnings.append(f"GEX failed: {e}")
        print(f"[gex] FAILED (non-fatal): {e}")
    # E3b Stage 0: GEX universe eligibility accumulator (OCC, keyless)
    try:
        run_gex_universe()
    except Exception as e:
        warnings.append(f"gex_universe failed: {e}")
        print(f"[gexu] FAILED (non-fatal): {e}")
    # E3b Stage 1: per-stock GEX walls for eligible names only
    try:
        run_gex_stocks()
    except Exception as e:
        warnings.append(f"gex_stocks failed: {e}")
        print(f"[gexs] FAILED (non-fatal): {e}")
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
    # universe-wide financials + earnings (CSV, no network) — EVERY ticker
    try:
        run_financials_all()
    except Exception as e:
        warnings.append(f"financials_all failed: {e}")
        print(f"[finall] FAILED (non-fatal): {e}")
    try:
        run_research()
    except Exception as e:
        warnings.append(f"research failed: {e}")
        print(f"[research] FAILED (non-fatal): {e}")
    # GUARANTEED detail-page bundle (financials always from CSV; ratings/earnings
    # for the small GEX+pinned set). Runs unconditionally — this is what the
    # dashboard reads first for the three detail sections.
    try:
        run_detail_bundle()
    except Exception as e:
        warnings.append(f"detail_bundle failed: {e}")
        print(f"[detail] FAILED (non-fatal): {e}")
    # --- Smart Money + universe ratings (folded into the daily run so there's
    # nothing to trigger by hand). Each is non-fatal and self-accumulating. ---
    try:
        run_congress_trades()
    except Exception as e:
        warnings.append(f"congress failed: {e}")
        print(f"[congress] FAILED (non-fatal): {e}")
    try:
        run_institutional_13f()
    except Exception as e:
        warnings.append(f"institutional_13f failed: {e}")
        print(f"[13f] FAILED (non-fatal): {e}")
    # ratings_all is the slow one (per-ticker Yahoo). Bounded per run — largest
    # caps first — and it accumulates, so a few daily runs cover the universe and
    # then keep it fresh without ever timing out.
    try:
        run_ratings_all(limit=RATINGS_DAILY_CAP)
    except Exception as e:
        warnings.append(f"ratings_all failed: {e}")
        print(f"[ratall] FAILED (non-fatal): {e}")
    # Layer 4 in the batch: CIO theses (C1) — needs ANTHROPIC_API_KEY secret
    try:
        run_theses()
    except Exception as e:
        warnings.append(f"theses failed: {e}")
        print(f"[theses] FAILED (non-fatal): {e}")
    # Push alerts on CHANGES only (E2) — needs NTFY_TOPIC secret
    try:
        run_alerts()
    except Exception as e:
        warnings.append(f"alerts failed: {e}")
        print(f"[alerts] FAILED (non-fatal): {e}")
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
    elif name == "detailbundle":
        run_detail_bundle()
    elif name == "congress":
        run_congress_trades()
    elif name == "institutional":
        run_institutional_13f()
    elif name == "backtest":
        backtest_smart_money()
    elif name == "financialsall":
        run_financials_all()
    elif name == "ratingsall":
        run_ratings_all()
    elif name == "stocks":
        run_stocks()
    elif name == "theses":
        run_theses()
    elif name == "alerts":
        run_alerts()
    elif name == "gexuniverse":
        run_gex_universe()
    elif name == "gexbriefing":
        run_gex_from_briefing(sys.argv[3] if len(sys.argv) > 3 else None)
    elif name == "gexstocks":
        run_gex_stocks()
    elif name == "vixterm":
        run_vix_term()
    else:
        print(f"[{name}] not yet wired — coming next")

# ============================================================
# ENTRYPOINT
# ============================================================
def main():
    p = argparse.ArgumentParser(description="Project Phoenix (flat v0)")
    p.add_argument("--full", action="store_true")
    p.add_argument("--engine", type=str)
    p.add_argument("--calib-add", nargs="+", metavar="V",
                   help="log source values: DATE NET_GEX VANNA CHARM [FLIP] "
                        "(e.g. 2026-07-17 -14.11 47.0 13.7 7495.40)")
    p.add_argument("--calib-analyze", action="store_true", help="show calibration ratios + suggested factors")
    p.add_argument("--calib-backfill", action="store_true",
                   help="pair calib_source.csv (briefing archive) with the git history of outputs/gex.json")
    a = p.parse_args()
    if a.full: run_full()
    elif a.engine: run_engine(a.engine)
    elif a.calib_add:
        args = a.calib_add
        if len(args) not in (4, 5):
            print("--calib-add needs DATE NET_GEX VANNA CHARM [FLIP]"); sys.exit(1)
        d, ng, vn, cm = args[:4]
        fl = float(args[4]) if len(args) == 5 else None
        calib_log_add(d, float(ng), float(vn), float(cm), source_flip=fl)
    elif a.calib_analyze: calib_analyze()
    elif a.calib_backfill: calib_backfill()
    else: p.print_help(); sys.exit(1)

if __name__ == "__main__":
    main()
