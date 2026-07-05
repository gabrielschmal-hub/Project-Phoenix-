"""
Phoenix configuration — the SINGLE place for every threshold and parameter.
Today these are scattered across Colab scripts. Here they live once.
Change a number here, the whole system uses it.
"""

# ---------- API keys (move to env vars / GitHub secrets before going public) ----------
FRED_API_KEY = "2f76d833bc195077cd00b1b4c2150dce"   # TODO: move to env var

# ---------- Macro / regime engine thresholds ----------
REGIME = {
    "policy_tightening_2y_3m_bp": 40,    # 2Y 3-month change >= this → POLICY_TIGHTENING
    "cpi_goldilocks_ceiling": 3.0,       # CPI above this blocks GOLDILOCKS
    "hy_spread_stress_bp": 500,          # HY spread above this → credit stress
    # ... full 8-regime rules get codified in engines/macro_engine.py
}

# ---------- Stock engine gates & scoring ----------
STOCK = {
    "min_market_cap": 300e6,             # gate 4: minimum market cap
    "fundamentals_min_cap": 2e9,         # only pull fundamentals above this
    "high_lookback_weeks": 104,          # gate 5: 104-week high window
    "near_high_band": (-8, -1),          # % below high to count as "near"
    "breakout_vol_surge_pct": 70,        # breakout: volume surge threshold
    "accum_surge_pct": 30,               # accumulation volume threshold
    # Two-score weights (ASSERTED — to be DERIVED via backtest, see architecture §5.3)
    "trade_weights":  {"rs_mkt": .30, "vol_surge": .25, "tightness": .20, "rs12": .15, "base": .10},
    "invest_weights": {"long_rs": .35, "durability": .25, "fundamentals": .25, "rs12": .15},
}

# ---------- GEX engine ----------
GEX = {
    "source": "SPY_x10",                 # SPY proxy (^SPX Yahoo data is broken). IBKR later.
    "risk_free": 0.045,
    "div_yield": 0.013,
    "otm_band": 0.15,                    # keep strikes within ±15% of spot
    "max_expiries": 16,                  # weeklies + monthly OPEX
    "calibration_factor": 1.0,           # TODO: tune vs source (sign correction)
}

# ---------- Trade engine / risk ----------
RISK = {
    "risk_conservative": 0.01,
    "risk_aggressive": 0.02,
    "atr_stop_mult": 2.0,
    "max_position_pct": 0.35,
    "max_heat_R": 3,
    "cooloff_losses": 6,
    "cooloff_days": 5,
}

# ---------- Paths ----------
DB_PATH = "macroflow.db"
OUTPUTS_DIR = "outputs"
