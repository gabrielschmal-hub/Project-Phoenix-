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
def gex_engine(options_chain):
    """CONTRACT: chain -> {overview, levels, profile, confidence}. Track 2: port v3 logic."""
    raise NotImplementedError("GEX engine: Track 2 — porting v3 logic next")

def stock_engine(stock_data, industry_scores, fundamentals, universe):
    """CONTRACT: data -> {meta, stocks[]}. Track 1: port validated selection logic."""
    raise NotImplementedError("Stock engine: Track 1 — porting selection logic next")

# ============================================================
# SCHEDULER — orchestrates the daily run
# ============================================================
def run_full():
    print("=== Phoenix full run ===")
    init_db()
    warnings, flags = [], {}
    print("[collect] collectors not yet wired")
    print("[store]   db writes not yet wired")
    print("[score]   engines not yet wired (Track 1 + Track 2 next)")
    write_meta(source_flags=flags, warnings=warnings)
    print("[write]   meta.json written")
    print("=== done — scaffold runs clean ===")

def run_engine(name):
    print(f"=== Phoenix engine: {name} ===")
    print(f"[{name}] not yet wired — coming in Track 1/2")

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
