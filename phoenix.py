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
    "otm_band": 0.15, "max_expiries": 16, "calibration_factor": 1.0,
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
            "net_gex_B": round(net_gex / 1e9, 2),
            "regime": "Positive Gamma" if net_gex > 0 else "Negative Gamma",
            "gamma_flip": round(flip, 2),
            "dist_to_flip_pct": round((flip / spot - 1) * 100, 2),
            "net_vanna_B_per_volpt": round(net_vanna / 1e9, 2),
            "net_charm_B_per_day": round(net_charm / 1e9, 2),
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

def stock_engine(stock_data, industry_scores, fundamentals, universe):
    """CONTRACT: data -> {meta, stocks[]}. Track 1: port validated selection logic."""
    raise NotImplementedError("Stock engine: Track 1 — porting selection logic next")


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
    print("[score]   stock engine next (Track 1)")
    write_meta(source_flags=flags, warnings=warnings)
    print("[write]   meta.json written")
    print("=== done — scaffold runs clean ===")

def run_engine(name):
    print(f"=== Phoenix engine: {name} ===")
    if name == "gex":
        run_gex()
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
    a = p.parse_args()
    if a.init_db: init_db()
    elif a.full: run_full()
    elif a.engine: run_engine(a.engine)
    else: p.print_help(); sys.exit(1)

if __name__ == "__main__":
    main()
