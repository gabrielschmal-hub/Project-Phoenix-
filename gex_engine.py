"""
GEX ENGINE — dealer gamma/vanna/charm exposure from the SPX options chain.
Pure function: input option chain → output flip, walls, greeks, regime.

CONTRACT:
  run(options_chain) -> dict
    options_chain: [ {strike, expiry, kind('call'|'put'), open_interest, iv}, ... ]
                   plus spot, risk_free, div_yield
  returns:
    {
      "asof": "...", "source": "SPY_x10" | "ibkr",
      "overview": {spx_spot, net_gex_B, regime, gamma_flip, dist_to_flip_pct,
                   net_vanna_B_per_volpt, net_charm_B_per_day},
      "levels": {pin, call_wall, put_wall, gamma_flip},
      "profile": [ {strike, net_gex_B, coi, poi}, ... ],   # drives both charts
      "confidence": {"levels": "high", "regime_sign": "low_on_proxy"}
    }

STATUS: Track 2 — port the validated v3 logic (SPY full chain + vanna/charm).
KNOWN LIMITATION: on SPY proxy, levels accurate but net-GEX SIGN unreliable.
The 'confidence' field must reflect this. IBKR options data fixes the sign later.
"""
from core.config import GEX

def run(options_chain):
    raise NotImplementedError("Track 2: port GEX v3 logic here")
