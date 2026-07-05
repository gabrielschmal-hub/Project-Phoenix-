"""
The single place that encodes 'this field comes from IBKR, that from free sources.'
Your ruling: IBKR for accuracy, free for the rest — with graceful fallback.
"""

# preferred source per field
SOURCE_MAP = {
    "spx_price":      "ibkr",       # accuracy matters
    "ndx_price":      "ibkr",
    "spx_options":    "ibkr",       # GEX needs TRUE SPX
    "vix_term":       "ibkr",       # futures curve
    "positions":      "ibkr",       # your live book — IBKR only, no fallback
    "rates":          "fred",       # authoritative + free
    "credit_spreads": "fred",
    "stock_universe": "yfinance",   # 3,900 tickers — free is fine
    "sector_etfs":    "yfinance",
    "macro_indices":  "yfinance",   # SPX/NDX/VIX/WTI/gold proxies when IBKR down
    "gex_chain":      "yfinance",   # SPY proxy (fallback for spx_options)
}

# if the preferred source fails, fall back to this (None = no fallback, field is optional)
FALLBACK_MAP = {
    "spx_price":   "yfinance",      # ^GSPC
    "ndx_price":   "yfinance",      # ^NDX
    "spx_options": "yfinance",      # SPY proxy → gex_chain path
    "vix_term":    None,            # keep last known curve; no clean free source
    "positions":   None,            # IBKR only — no substitute for your book
}

def preferred(field: str) -> str:
    return SOURCE_MAP.get(field, "yfinance")

def fallback(field: str):
    return FALLBACK_MAP.get(field, None)
