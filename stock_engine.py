"""
STOCK ENGINE — 6 gates + two-score (Trade / Investment) ranking.
Pure function: input data → output scored stocks. No AI, no I/O, no network.

CONTRACT:
  run(stock_data, industry_scores, fundamentals, universe) -> dict
    stock_data:      {ticker: [(date, close, volume), ...]}  weekly bars
    industry_scores: {industry: {momentum, above_ma, rising}}
    fundamentals:    {ticker: {revenue, net_income, fcf, ...}}
    universe:        {ticker: {sector, industry, market_cap}}
  returns:
    {
      "asof": "YYYY-MM-DD",
      "meta": {"passers": int, "breakouts": int, "industries_passing": int},
      "stocks": [ {ticker, industry, mcap, trade_score, invest_score,
                   vol_state, breakout, surge, gates_passed, ...}, ... ]
    }

STATUS: Track 1 — to be filled by porting the validated selection logic (sel_jul2.py).
The math is KNOWN and validated; this is a relocation, not a redesign.
"""
from core.config import STOCK

def run(stock_data, industry_scores, fundamentals, universe):
    raise NotImplementedError("Track 1: port selection logic here")

# --- the gate functions and scorers will live here as small testable pieces ---
# def passes_gates(ticker, closes, vols, mcap, industry_scores, universe) -> (bool, dict)
# def trade_score(features) -> float
# def invest_score(features, fundamentals) -> float
