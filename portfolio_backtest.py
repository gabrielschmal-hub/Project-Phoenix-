#!/usr/bin/env python3
"""
PHOENIX — PORTFOLIO BACKTEST (allocation lane, not the trade book).

Answers one question honestly: what do the Ballast (medium) and Compounder
(long) allocations actually return in EUR, after Italian tax, and what would
the single-stock satellite have to deliver for the total to reach a target.

WHY PROXIES: every UCITS line we would actually buy (VWCE, WEBN, SGLD, iBonds)
has 1-6 years of history. Backtesting the TICKER would be a fabrication dressed
as evidence. So we backtest the long-history PROXY, in USD, convert to EUR, then
subtract the real cost layer (TER, bollo, tax). The ticker is the implementation;
the proxy is the thing with a track record. Every proxy is declared below with
its known mismatch. Read them before trusting an output.

Run:
  python portfolio_backtest.py --run           full backtest + sweep + bootstrap
  python portfolio_backtest.py --sweep-only    satellite alpha sweep (no fetch)
  python portfolio_backtest.py --selftest      offline synthetic-fixture test

Writes outputs/portfolio_backtest.json
"""
import argparse, json, math, os, random, sys, time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

OUTPUTS_DIR = "outputs"
OUT_FILE = os.path.join(OUTPUTS_DIR, "portfolio_backtest.json")

# ============================================================
# CONFIG — every threshold in one place
# ============================================================

# Italian tax, verified Aug 2026. Change here, nowhere else.
TAX = {
    "etf_equity":   0.26,   # redditi di capitale — NOT offsettable by minusvalenze
    "etf_govt":     0.125,  # white-list government portion
    "etc_gold":     0.26,   # redditi diversi — IS offsettable
    "stock":        0.26,   # redditi diversi — IS offsettable
    "crypto_etp":   0.26,   # ASSERTED: security treatment. Unconfirmed — see note
    "crypto_spot":  0.33,   # L.199/2025, realizations from 01-01-2026
    "cash":         0.26,
}
BOLLO_ANNUAL = 0.0020      # 0.20%/yr on account value (IVAFE equivalent at IBKR)

# Per-line TER, annual drag. Approximate — verify against Fineco list.
TER = {
    "equity_core": 0.0022,  # VWCE. WEBN ~0.0007 but thin tracking record
    "stocks":      0.0000,  # single names carry no TER
    "bonds":       0.0015,
    "gold":        0.0012,
    "crypto":      0.0020,
    "cash":        0.0010,
}

# Yahoo proxies. history_from is the first date the proxy actually has data.
# mismatch is the known lie in the proxy — never delete these strings.
PROXIES = {
    "equity_core": {"ticker": "ACWI", "ccy": "USD", "history_from": "2008-03",
                    "fallback": ["VT", "^GSPC"],
                    "mismatch": "MSCI ACWI in USD. VWCE tracks FTSE All-World — "
                                "near-identical exposure, minor index differences."},
    "stocks":      {"ticker": None, "ccy": "USD", "history_from": None,
                    "fallback": [],
                    "mismatch": "NO PROXY. The satellite is unvalidated, so it is "
                                "modelled as core + alpha and SWEPT, never assumed."},
    "bonds":       {"ticker": "IEF", "ccy": "USD", "history_from": "2002-07",
                    "fallback": ["AGG"],
                    "mismatch": "US 7-10y Treasury. This is a DURATION proxy, not a "
                                "EUR govt proxy — it carries US rate and USD risk that "
                                "the real EUR line does not. Treat bond leg as "
                                "directionally right, level wrong."},
    "gold":        {"ticker": "GLD", "ccy": "USD", "history_from": "2004-11",
                    "fallback": ["GC=F"],
                    "mismatch": "Gold spot in USD. SGLD is the same underlying."},
    "crypto":      {"ticker": "BTC-USD", "ccy": "USD", "history_from": "2014-09",
                    "fallback": [],
                    "mismatch": "BTC spot. Short history, non-stationary distribution. "
                                "Any BTC-inclusive result before 2014 is truncated."},
    "cash":        {"ticker": None, "ccy": "EUR", "history_from": None,
                    "fallback": [],
                    "mismatch": "Modelled at CASH_YIELD, not fetched."},
}
FX_TICKER = "EURUSD=X"     # USD per 1 EUR
CASH_YIELD = 0.020         # annual, applied monthly

# ---- THE TWO PORTFOLIOS -------------------------------------------------
# Weights must sum to 1.0. Validated at load.

COMPOUNDER = {
    "name": "Compounder (long, 10y+)",
    "capital_eur": 18000.0,
    "weights": {"equity_core": 0.60, "stocks": 0.25, "gold": 0.08,
                "crypto": 0.07, "bonds": 0.00, "cash": 0.00},
    "glidepath": None,
    "rebalance": "quarterly",
    "band_pp": 0.05,
}

# Medium sleeve de-risks as the call date approaches. Keyed by years remaining.
BALLAST_GLIDE = [
    (6, {"equity_core": 0.45, "stocks": 0.10, "bonds": 0.28,
         "gold": 0.10, "crypto": 0.05, "cash": 0.02}),
    (4, {"equity_core": 0.38, "stocks": 0.08, "bonds": 0.36,
         "gold": 0.10, "crypto": 0.04, "cash": 0.04}),
    (2, {"equity_core": 0.20, "stocks": 0.00, "bonds": 0.62,
         "gold": 0.08, "crypto": 0.00, "cash": 0.10}),
    (0, {"equity_core": 0.08, "stocks": 0.00, "bonds": 0.62,
         "gold": 0.05, "crypto": 0.00, "cash": 0.25}),
]
BALLAST = {
    "name": "Ballast (medium, 3-7y)",
    "capital_eur": 7000.0,
    "weights": BALLAST_GLIDE[0][1],
    "glidepath": BALLAST_GLIDE,
    "horizon_years": 7,
    "rebalance": "quarterly",
    "band_pp": 0.05,
}

TAX_BY_LINE = {
    "equity_core": "etf_equity", "stocks": "stock", "bonds": "etf_govt",
    "gold": "etc_gold", "crypto": "crypto_etp", "cash": "cash",
}

# Satellite alpha sweep: annual excess return over core, in pp.
ALPHA_SWEEP = [-0.10, -0.05, 0.0, 0.05, 0.10, 0.20, 0.30, 0.36, 0.50]

BOOTSTRAP_PATHS = 10000
BOOTSTRAP_BLOCK_MONTHS = 12
EUR_INFLATION = 0.020      # for the real-return line


# ============================================================
# FETCH — Yahoo chart API via urllib. No yfinance dependency.
# ============================================================

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{t}?period1=0&period2={now}&interval=1mo"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def fetch_monthly(ticker, retries=3):
    """
    Monthly ADJUSTED closes for one ticker -> {'YYYY-MM': close}.
    Retries with backoff. Returns None on permanent failure — the caller must
    NOT silently cache a failure as an empty series (Phoenix data-integrity rule).
    """
    url = YAHOO.format(t=urllib.parse.quote(ticker), now=int(time.time()))
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode())
            res = data["chart"]["result"][0]
            stamps = res["timestamp"]
            quote = res["indicators"]["quote"][0]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
            closes = adj if adj else quote["close"]
            out = {}
            for ts, c in zip(stamps, closes):
                if c is None:
                    continue
                d = datetime.fromtimestamp(ts, tz=timezone.utc)
                out[f"{d.year:04d}-{d.month:02d}"] = float(c)
            if len(out) < 12:
                raise ValueError(f"only {len(out)} months returned")
            return out
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    print(f"  [fetch] FAILED {ticker}: {last}")
    return None


def fetch_line(line):
    """Fetch a portfolio line, walking the fallback chain. None if all fail."""
    spec = PROXIES[line]
    if spec["ticker"] is None:
        return None
    for t in [spec["ticker"]] + spec["fallback"]:
        s = fetch_monthly(t)
        if s:
            if t != spec["ticker"]:
                print(f"  [fetch] {line}: fell back to {t}")
            return {"ticker": t, "series": s}
    return None


def to_eur(series_usd, fx):
    """USD series -> EUR. fx is USD per 1 EUR, so EUR value = USD / fx."""
    out = {}
    for m, v in series_usd.items():
        if m in fx and fx[m]:
            out[m] = v / fx[m]
    return out


def monthly_returns(series):
    """{'YYYY-MM': level} -> {'YYYY-MM': simple return vs prior month}."""
    months = sorted(series)
    out = {}
    for a, b in zip(months, months[1:]):
        if series[a]:
            out[b] = series[b] / series[a] - 1.0
    return out


def build_panel(lines, verbose=True):
    """
    Fetch every line, convert to EUR, return (returns_by_line, months, notes).
    months is the intersection where EVERY requested line has data — the panel
    is only as long as its shortest leg, and that truncation is reported.
    """
    notes = []
    if verbose:
        print("[panel] fetching FX")
    fx = fetch_monthly(FX_TICKER)
    if not fx:
        raise SystemExit("[panel] FATAL: no FX series — cannot express anything in EUR")

    rets, spans = {}, {}
    for line in lines:
        if line == "cash":
            continue
        if line == "stocks":
            continue  # modelled from core + alpha, never fetched
        if verbose:
            print(f"[panel] fetching {line}")
        got = fetch_line(line)
        if not got:
            notes.append(f"{line}: FETCH FAILED — line dropped, weights renormalised")
            continue
        eur = to_eur(got["series"], fx) if PROXIES[line]["ccy"] == "USD" else got["series"]
        r = monthly_returns(eur)
        rets[line] = r
        spans[line] = (min(r), max(r)) if r else (None, None)
        notes.append(f"{line}: proxy {got['ticker']}, {len(r)} months, "
                     f"{spans[line][0]}..{spans[line][1]}")

    if not rets:
        raise SystemExit("[panel] FATAL: no lines fetched")

    common = set.intersection(*[set(r) for r in rets.values()])
    months = sorted(common)
    binding = min(spans.items(), key=lambda kv: kv[1][0] or "9999")
    notes.append(f"PANEL TRUNCATED TO {len(months)} months ({months[0]}..{months[-1]}) "
                 f"— binding constraint is {binding[0]} starting {binding[1][0]}")
    return rets, months, notes


# ============================================================
# SIMULATE — the actual portfolio, with tax charged on real rebalance sales
# ============================================================

def glide_weights(pf, months_elapsed):
    """Weights for the current point on the glidepath (or static if none)."""
    if not pf.get("glidepath"):
        return pf["weights"]
    yrs_left = pf["horizon_years"] - months_elapsed / 12.0
    chosen = pf["glidepath"][-1][1]
    for threshold, w in pf["glidepath"]:
        if yrs_left >= threshold:
            chosen = w
            break
    return chosen


def simulate(pf, rets, months, satellite_alpha=0.0, contribution_eur=0.0):
    """
    Walk the panel month by month.

    satellite_alpha: annual excess return of the single-stock sleeve over the
      core ETF. NEVER assumed — the caller sweeps it. This is the honest way to
      carry an unvalidated selection edge.
    contribution_eur: monthly addition, deployed to the most underweight line
      first (contributions before sales — the tax-efficient rebalance rule).

    Returns dict with the equity curve and the tax/cost ledger.
    """
    lines = [l for l, w in pf["weights"].items() if w > 0]
    active = [l for l in lines if l in rets or l in ("cash", "stocks")]

    w0 = glide_weights(pf, 0)
    tot_w = sum(w0.get(l, 0) for l in active)
    holdings = {l: pf["capital_eur"] * w0.get(l, 0) / tot_w for l in active}
    basis = dict(holdings)  # cost basis per line, for tax on sales

    alpha_m = (1.0 + satellite_alpha) ** (1 / 12.0) - 1.0
    cash_m = (1.0 + CASH_YIELD) ** (1 / 12.0) - 1.0

    curve, tax_paid, cost_paid, contributed = [], 0.0, 0.0, 0.0

    for i, m in enumerate(months):
        # --- grow every line ---
        for l in list(holdings):
            if l == "cash":
                r = cash_m
            elif l == "stocks":
                r = rets["equity_core"].get(m, 0.0) + alpha_m
            else:
                r = rets[l].get(m, 0.0)
            ter_m = TER.get(l, 0.0) / 12.0
            holdings[l] *= (1.0 + r - ter_m)
            cost_paid += holdings[l] * ter_m

        # --- bollo, monthly accrual on account value ---
        val = sum(holdings.values())
        bollo = val * BOLLO_ANNUAL / 12.0
        cost_paid += bollo
        biggest = max(holdings, key=holdings.get)
        holdings[biggest] -= bollo

        # --- contribution, to the most underweight line ---
        if contribution_eur:
            tgt = glide_weights(pf, i)
            val = sum(holdings.values())
            gaps = {l: tgt.get(l, 0) - (holdings[l] / val if val else 0) for l in holdings}
            into = max(gaps, key=gaps.get)
            holdings[into] += contribution_eur
            basis[into] += contribution_eur
            contributed += contribution_eur

        # --- quarterly band rebalance ---
        if (i + 1) % 3 == 0:
            tgt = glide_weights(pf, i)
            val = sum(holdings.values())
            if val > 0:
                drift = {l: (holdings[l] / val) - tgt.get(l, 0) for l in holdings}
                if any(abs(d) > pf["band_pp"] for d in drift.values()):
                    for l in holdings:
                        want = val * tgt.get(l, 0)
                        delta = want - holdings[l]
                        if delta < 0:  # SELLING — realises gain, tax due
                            sold = -delta
                            frac = sold / holdings[l] if holdings[l] else 0
                            gain = max(0.0, (holdings[l] - basis[l]) * frac)
                            t = gain * TAX[TAX_BY_LINE[l]]
                            tax_paid += t
                            basis[l] -= basis[l] * frac
                            holdings[l] -= sold
                            # tax leaves the portfolio
                            val -= t
                        else:
                            basis[l] += delta
                            holdings[l] += delta
        curve.append({"month": m, "value": sum(holdings.values())})

    final = curve[-1]["value"] if curve else 0.0
    invested = pf["capital_eur"] + contributed
    return {"curve": curve, "final_eur": final, "invested_eur": invested,
            "tax_paid_eur": tax_paid, "costs_paid_eur": cost_paid,
            "months": len(curve), "satellite_alpha": satellite_alpha}


# ============================================================
# METRICS — allocation-lane metrics. NOT the trade book's E - 0.054R.
# ============================================================

def metrics(sim):
    vals = [p["value"] for p in sim["curve"]]
    if len(vals) < 13:
        return {"error": "panel too short for annualised metrics"}
    yrs = sim["months"] / 12.0

    # money-weighted CAGR is wrong when contributions exist; report both honestly
    cagr = (vals[-1] / vals[0]) ** (1 / yrs) - 1.0 if vals[0] > 0 else None
    real = (1 + cagr) / (1 + EUR_INFLATION) - 1.0 if cagr is not None else None

    peak, dd, maxdd, ulcer, trough_i, peak_i, rec = vals[0], 0.0, 0.0, 0.0, 0, 0, None
    for i, v in enumerate(vals):
        if v > peak:
            peak, peak_i = v, i
        d = (v / peak - 1.0) if peak else 0.0
        ulcer += d * d
        if d < maxdd:
            maxdd, trough_i = d, i
    ulcer = math.sqrt(ulcer / len(vals))

    # months from the max-drawdown trough back to the prior peak
    prior_peak = max(vals[:trough_i + 1]) if trough_i else vals[0]
    for j in range(trough_i, len(vals)):
        if vals[j] >= prior_peak:
            rec = j - trough_i
            break

    r12 = [vals[i] / vals[i - 12] - 1.0 for i in range(12, len(vals))]
    neg60 = None
    if len(vals) > 60:
        r60 = [vals[i] / vals[i - 60] - 1.0 for i in range(60, len(vals))]
        neg60 = sum(1 for x in r60 if x < 0) / len(r60)

    return {
        "cagr_nominal_eur": round(cagr, 4) if cagr is not None else None,
        "cagr_real_eur": round(real, 4) if real is not None else None,
        "max_drawdown": round(maxdd, 4),
        "max_dd_recovery_months": rec,
        "ulcer_index": round(ulcer, 4),
        "worst_12m": round(min(r12), 4) if r12 else None,
        "best_12m": round(max(r12), 4) if r12 else None,
        "pct_negative_5y_windows": round(neg60, 4) if neg60 is not None else None,
        "years": round(yrs, 2),
    }


def bootstrap(sim, paths=BOOTSTRAP_PATHS, block=BOOTSTRAP_BLOCK_MONTHS, horizon_m=None):
    """
    Block bootstrap on the realised monthly returns.

    WHY: one historical path is ONE SAMPLE. The single backtest line tells you
    what happened, not what the strategy does. Resampling in 12-month blocks
    preserves within-year autocorrelation while producing a distribution.
    """
    vals = [p["value"] for p in sim["curve"]]
    if len(vals) < block * 2:
        return {"error": "panel too short to bootstrap"}
    mr = [vals[i] / vals[i - 1] - 1.0 for i in range(1, len(vals))]
    horizon_m = horizon_m or len(mr)
    nblocks = max(1, horizon_m // block)

    finals, dds = [], []
    rng = random.Random(20260831)
    for _ in range(paths):
        v, peak, worst = 1.0, 1.0, 0.0
        for _ in range(nblocks):
            s = rng.randrange(0, len(mr) - block)
            for r in mr[s:s + block]:
                v *= (1.0 + r)
                peak = max(peak, v)
                worst = min(worst, v / peak - 1.0)
        finals.append(v)
        dds.append(worst)
    finals.sort(); dds.sort()
    q = lambda a, p: a[int(p * (len(a) - 1))]
    yrs = (nblocks * block) / 12.0
    ann = lambda x: x ** (1 / yrs) - 1.0
    return {
        "paths": paths, "horizon_years": round(yrs, 1),
        "cagr_p05": round(ann(q(finals, 0.05)), 4),
        "cagr_p25": round(ann(q(finals, 0.25)), 4),
        "cagr_median": round(ann(q(finals, 0.50)), 4),
        "cagr_p75": round(ann(q(finals, 0.75)), 4),
        "cagr_p95": round(ann(q(finals, 0.95)), 4),
        "prob_beat_15pct": round(sum(1 for f in finals if ann(f) >= 0.15) / len(finals), 4),
        "prob_loss": round(sum(1 for f in finals if f < 1.0) / len(finals), 4),
        "median_max_drawdown": round(q(dds, 0.50), 4),
        "p05_max_drawdown": round(q(dds, 0.05), 4),
    }


# ============================================================
# SWEEP — what must the satellite deliver for the total to hit a target
# ============================================================

def required_satellite(target, core_return, w_core, w_sat, others):
    """
    Solve target = w_core*core + w_sat*x + sum(others) for x.
    others: list of (weight, assumed_return). Pure algebra — no forecast.
    """
    if w_sat <= 0:
        return None
    rest = sum(w * r for w, r in others)
    return (target - w_core * core_return - rest) / w_sat


def alpha_sweep(pf, rets, months):
    """Run the portfolio at each satellite alpha. This is the 15% question."""
    rows = []
    for a in ALPHA_SWEEP:
        sim = simulate(pf, rets, months, satellite_alpha=a)
        m = metrics(sim)
        rows.append({"satellite_alpha": a,
                     "cagr_nominal_eur": m.get("cagr_nominal_eur"),
                     "cagr_real_eur": m.get("cagr_real_eur"),
                     "max_drawdown": m.get("max_drawdown"),
                     "final_eur": round(sim["final_eur"], 2),
                     "tax_paid_eur": round(sim["tax_paid_eur"], 2)})
    return rows


def run(portfolios, contribution_eur=0.0):
    lines = set()
    for pf in portfolios:
        lines |= {l for l, w in pf["weights"].items() if w > 0}
        for _, w in (pf.get("glidepath") or []):
            lines |= {l for l, ww in w.items() if ww > 0}
    rets, months, notes = build_panel(sorted(lines))

    out = {"schema": "phoenix-portfolio-backtest/1",
           "generated": datetime.now(timezone.utc).isoformat(),
           "panel_notes": notes,
           "proxy_mismatches": {k: v["mismatch"] for k, v in PROXIES.items()},
           "tax_applied": TAX, "bollo_annual": BOLLO_ANNUAL,
           "portfolios": []}

    for pf in portfolios:
        print(f"\n[run] {pf['name']}")
        base = simulate(pf, rets, months, satellite_alpha=0.0,
                        contribution_eur=contribution_eur)
        entry = {
            "name": pf["name"], "capital_eur": pf["capital_eur"],
            "weights_initial": pf["weights"],
            "baseline_zero_alpha": {**metrics(base),
                                    "final_eur": round(base["final_eur"], 2),
                                    "tax_paid_eur": round(base["tax_paid_eur"], 2),
                                    "costs_paid_eur": round(base["costs_paid_eur"], 2)},
            "bootstrap": bootstrap(base),
            "satellite_alpha_sweep": alpha_sweep(pf, rets, months),
            "curve": base["curve"],
        }
        out["portfolios"].append(entry)
        b = entry["baseline_zero_alpha"]
        print(f"  zero-alpha CAGR {b.get('cagr_nominal_eur')}  "
              f"maxDD {b.get('max_drawdown')}  final EUR {b.get('final_eur')}")

    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=1)
    print(f"\n[run] wrote {OUT_FILE}")
    return out


# ============================================================
# SELFTEST — synthetic fixtures, no network. Proves the plumbing.
# ============================================================

def selftest():
    print("=== SELFTEST (synthetic fixtures, offline) ===")
    rng = random.Random(7)
    months = [f"{y:04d}-{m:02d}" for y in range(2010, 2026) for m in range(1, 13)]
    spec = {"equity_core": (0.0065, 0.042), "bonds": (0.0020, 0.012),
            "gold": (0.0035, 0.045), "crypto": (0.0150, 0.220)}
    rets = {l: {m: rng.gauss(mu, sd) for m in months} for l, (mu, sd) in spec.items()}

    fails = []
    for pf in (COMPOUNDER, BALLAST):
        s = sum(pf["weights"].values())
        if abs(s - 1.0) > 1e-9:
            fails.append(f"{pf['name']}: weights sum to {s}, not 1.0")
        for _, w in (pf.get("glidepath") or []):
            if abs(sum(w.values()) - 1.0) > 1e-9:
                fails.append(f"{pf['name']}: glidepath step sums to {sum(w.values())}")

        sim = simulate(pf, rets, months, satellite_alpha=0.0)
        m = metrics(sim)
        if sim["final_eur"] <= 0:
            fails.append(f"{pf['name']}: non-positive final value")
        if m.get("max_drawdown", 0) > 0:
            fails.append(f"{pf['name']}: positive max drawdown — sign error")
        print(f"  {pf['name']}: CAGR {m['cagr_nominal_eur']}  maxDD {m['max_drawdown']}  "
              f"final {sim['final_eur']:.0f}  tax {sim['tax_paid_eur']:.0f}")

        hi = simulate(pf, rets, months, satellite_alpha=0.30)
        if pf["weights"].get("stocks", 0) > 0 and hi["final_eur"] <= sim["final_eur"]:
            fails.append(f"{pf['name']}: +30% alpha did not increase terminal value")

        bs = bootstrap(sim, paths=500)
        if "error" in bs:
            fails.append(f"{pf['name']}: bootstrap {bs['error']}")
        else:
            print(f"    bootstrap median CAGR {bs['cagr_median']}  "
                  f"P(>=15%) {bs['prob_beat_15pct']}  medDD {bs['median_max_drawdown']}")

    # glidepath must de-risk monotonically
    eq = [w["equity_core"] + w.get("stocks", 0) for _, w in BALLAST_GLIDE]
    if eq != sorted(eq, reverse=True):
        fails.append(f"glidepath equity not monotonically decreasing: {eq}")

    print("\n=== SELFTEST", "FAILED ===" if fails else "PASSED ===")
    for f in fails:
        print("  !!", f)
    return 1 if fails else 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--run", action="store_true")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--sweep-only", action="store_true")
    p.add_argument("--contribution", type=float, default=0.0,
                   help="monthly EUR contribution to both sleeves")
    a = p.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if a.sweep_only:
        for tgt in (0.10, 0.12, 0.15, 0.20):
            for core in (0.06, 0.08, 0.10):
                x = required_satellite(tgt, core, 0.60, 0.25,
                                       [(0.08, 0.04), (0.07, 0.15)])
                print(f"  target {tgt:.0%}  core {core:.0%}  -> satellite must return {x:.1%}")
        return
    if a.run:
        run([COMPOUNDER, BALLAST], contribution_eur=a.contribution)
        return
    p.print_help()


if __name__ == "__main__":
    main()
