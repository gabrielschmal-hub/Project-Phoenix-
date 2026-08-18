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
    # tactical walls are searched within +/-5% of spot. Beyond that a strike is
    # a magnet, not a level dealers defend intraday.
    "wall_band": 0.05,
    # a wall must still be a real cluster; gamma decides WHICH cluster
    "wall_min_oi": 15000,
    # bump whenever the LEVEL math changes: the session lock invalidates on a
    # version change, so a fix takes effect on the next run instead of waiting
    # for tomorrow
    # Bump this tag on ANY change to how levels are computed — the freeze
    # compares it and recomputes rather than serving the old method's numbers.
    # On 10 Aug the MME spec shipped WITHOUT a bump, so the first run held the
    # morning's legacy walls (7,775 — not even a multiple of 50, ineligible
    # under the spec) over the fresh spec walls all day. Exactly the failure
    # this tag exists to prevent.
    "levels_engine": "2026-08-10.mme-spec.bs-resolve-flip+closest-wall",
    # per-greek calibration. Left at 1.0 and uncalibrated: on the real CBOE
    # chain net gamma reads ~20% above the coach's figure, but until the flip
    # method is settled a fudge factor would only hide the discrepancy.
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
PUBLISH_HOLDS = []   # files the publish gate refused to overwrite this run


# ============================================================
# OUTPUTS — the JSON the frontend reads
# ============================================================
def _json_safe(o, _stats=None):
    """
    Replace NaN/Infinity with None, recursively. PURE.

    json.dump happily writes bare NaN, which is NOT valid JSON: the browser's
    JSON.parse rejects the whole file ("The string did not match the expected
    pattern"), so one bad float from a rate-limited Yahoo pull takes down an
    entire panel. Every writer goes through this now.
    """
    import math
    if isinstance(o, float):
        if math.isnan(o) or math.isinf(o):
            if _stats is not None:
                _stats[0] += 1
            return None
        return o
    if isinstance(o, dict):
        return {k: _json_safe(v, _stats) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v, _stats) for v in o]
    return o


def write_json(name, data):
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    path = os.path.join(OUTPUTS_DIR, f"{name}.json")
    stats = [0]
    clean = _json_safe(data, stats)
    if stats[0]:
        print(f"[json] {name}.json: {stats[0]} NaN/Inf value(s) written as null "
              f"— upstream data was incomplete (rate limit or partial bar)")
    with open(path, "w") as f:
        json.dump(clean, f, indent=1, allow_nan=False)
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
        PUBLISH_HOLDS.append(msg)
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


# ============================================================
# TRADES — the book is DATA, served like gex.json, not localStorage.
#
# WHY: trades used to live in each browser's localStorage, so Gabriel and
# Aldemar saw different books off the same URL, Safari's ITP could evict the lot
# after 7 idle days, and a profile-cache guard silently dropped saves.
# outputs/trades.json is now the single source of truth: one file, in git,
# identical for everyone, versioned so an edit shows in the diff.
#
# The engine does NOT write this file — Gabriel commits it. The engine's job is
# to refuse to let a bad row through quietly, because every portfolio analytic
# (expectancy, payoff, heat, projection, the Council) reads it, and a wrong row
# renders as a confident number rather than an error.
# ============================================================

def _load_trades():
    """
    The trade book, from the single source of truth.

    Prefers trades.json; falls back to the legacy trades_log.json so the engine
    keeps working during the transition and on any checkout that predates the
    move. Returns [] rather than raising — callers here are best-effort.
    """
    import json, os
    for fn in ("trades.json", "trades_log.json"):
        p = os.path.join(OUTPUTS_DIR, fn)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p))
        except Exception as e:
            print(f"[trades] {fn} unreadable: {e}")
            continue
        rows = d.get("trades") if isinstance(d, dict) else d
        if isinstance(rows, list):
            return rows
    return []


def _open_trades(rows=None):
    """Live positions only. 'open' plus legacy rows with no exit recorded."""
    rows = _load_trades() if rows is None else rows
    return [t for t in rows if t.get("ticker")
            and str(t.get("status", "open")).lower() not in
                ("closed", "exited", "stopped", "cancelled", "plan")
            and not t.get("exit_date")]


def validate_trades(path=None):
    """Check outputs/trades.json. Returns (errors, warnings, rows)."""
    import json, os
    path = path or os.path.join(OUTPUTS_DIR, "trades.json")
    errs, warns = [], []
    if not os.path.exists(path):
        return [f"{os.path.basename(path)} not found"], [], None
    try:
        d = json.load(open(path))
    except Exception as e:
        return [f"cannot parse {os.path.basename(path)}: {e}"], [], None

    accts = set((d.get("accounts") or {}).keys())
    rows = d.get("trades") or []
    if not accts:
        errs.append("no accounts block")
    seen = set()

    for t in rows:
        tid = t.get("id") or "<no id>"
        tag = f"{tid} {t.get('ticker', '?')}"
        if tid in seen:
            errs.append(f"{tag}: duplicate id")
        seen.add(tid)

        if t.get("account") not in accts:
            errs.append(f"{tag}: unknown account {t.get('account')!r}")

        st = t.get("status")
        if st not in ("plan", "open", "closed"):
            errs.append(f"{tag}: bad status {st!r}")
            continue
        if st == "plan":
            continue                                # a plan may be incomplete

        for k in ("entry", "initial_stop", "qty", "entry_date"):
            if t.get(k) in (None, ""):
                errs.append(f"{tag}: {st} trade missing {k}")

        e, s = t.get("entry"), t.get("initial_stop")
        if e is not None and s is not None:
            # initial_stop is what 1R is measured against and must NEVER be
            # overwritten by a trailed stop — that bug understates every
            # trailed winner and is invisible once it ships.
            if s >= e:
                warns.append(f"{tag}: initial_stop {s} >= entry {e} (zero/negative 1R)")
            if e > 0 and (e - s) / e > 0.35:
                warns.append(f"{tag}: stop {round((e - s) / e * 100, 1)}% below entry — check")

        hist = t.get("stop_history")
        if not isinstance(hist, list) or not hist:
            errs.append(f"{tag}: stop_history missing or empty")
        elif s is not None and hist[0].get("stop") != s:
            errs.append(f"{tag}: stop_history[0] {hist[0].get('stop')} != initial_stop {s}")

        if st == "closed":
            for k in ("exit_price", "exit_date"):
                if t.get(k) in (None, ""):
                    errs.append(f"{tag}: closed trade missing {k}")
            if t.get("entry_date") and t.get("exit_date") and t["exit_date"] < t["entry_date"]:
                errs.append(f"{tag}: exit_date before entry_date")
            if t.get("current_stop") is not None:
                warns.append(f"{tag}: closed trade carries current_stop")
        else:                                        # open
            if t.get("current_stop") is None:
                # A MANAGED position (bank/mandate account) cannot carry a stop
                # order — flagging it as an error would force fake data into the
                # file. It must say so explicitly via managed:true, and it still
                # needs initial_stop as the reference for R.
                if t.get("managed"):
                    warns.append(f"{tag}: managed position, no live stop — "
                                 f"excluded from stop-based heat")
                else:
                    errs.append(f"{tag}: open trade missing current_stop")
            if t.get("exit_price") is not None:
                errs.append(f"{tag}: open trade has an exit_price")

    return errs, warns, rows


def run_trades():
    """
    Pipeline step: validate the committed trade book.

    Raises on errors. run_full()'s step() wrapper catches it, marks the step
    failed in meta.json and prints it in the run report — loud and visible,
    but non-fatal to the other steps.
    """
    import collections
    errs, warns, rows = validate_trades()
    if rows is not None:
        c = collections.Counter((t.get("account"), t.get("status")) for t in rows)
        parts = [f"{a} {s}:{n}" for (a, s), n in sorted(c.items())]
        print(f"[trades] {len(rows)} rows · " + " · ".join(parts) +
              f" · {len(errs)} errors · {len(warns)} warnings")
    for w in warns:
        print(f"[trades] WARN  {w}")
    for e in errs:
        print(f"[trades] ERROR {e}")
    if errs:
        raise ValueError(f"trades.json has {len(errs)} error(s) — first: {errs[0]}")
    return len(rows or [])


# ============================================================
# GEX DIAGNOSTIC — the DTE sweep. READ-ONLY: writes nothing, changes nothing.
#
# WHY: Phoenix captures ~63% of the open interest Elliott's briefing reports,
# uniformly across strikes (59-73% at 11 of 12 published strikes). That
# uniformity says a SLICE of the chain is missing, not that the feed is broken.
# The prime suspect is the expiry window — GEX_MAX_DTE caps at 90 days.
#
# This fetches the chain ONCE and re-counts it at several cutoffs. Three
# outcomes, all useful:
#   - OI matches the briefing at some cutoff  -> the window was the bug
#   - it overshoots between two cutoffs       -> the real window sits between
#   - it never reaches the briefing at any    -> something ELSE is filtering
#                                                (strike band, missing series,
#                                                 a parse dropping rows)
#
# It also prints BOTH flip candidates at every cutoff, so one run answers the
# second question too: does the cumulative crossing or the per-strike crossing
# converge on the briefing's flip as coverage improves?
#
# THE SHIPPED FLIP IS NOT TOUCHED. Per the handover rule, the calculation does
# not change until it is measured — this is the measurement.
# ============================================================

# Reference OI from Elliott's briefing, 7 Aug 2026. Override with a JSON dict
# in GEX_SWEEP_REF to compare against any other session.
GEX_SWEEP_REF = {
    7400: 324472, 7450: 231443, 7500: 470909, 7550: 246839, 7600: 364076,
    7700: 195000, 7750: 106141, 7800: 202030, 7850: 73875, 7900: 183402,
    8000: 1661050,
}
GEX_SWEEP_REF_FLIP = 7580.49
GEX_SWEEP_REF_NET_B = 66.63


def _flip_candidates(profile):
    """Both crossing methods from one profile. Returns (cumulative, per_strike_25pt)."""
    import collections
    prof = sorted(profile, key=lambda p: p["strike"])

    def cross(pairs):
        out = []
        for i in range(1, len(pairs)):
            (s0, v0), (s1, v1) = pairs[i - 1], pairs[i]
            if v0 == 0 or (v0 < 0) != (v1 < 0):
                out.append(s0 + (s1 - s0) * (-v0) / (v1 - v0) if v1 != v0 else s0)
        return out

    cum, pairs = 0.0, []
    for p in prof:
        cum += p["net_gex_B"]
        pairs.append((p["strike"], cum))
    cumulative = cross(pairs)

    # Raw per-strike crossings are far too noisy to be a rule (27 of them on the
    # 7 Aug chain). Bucketing to 25pt — the granularity the histogram already
    # draws — collapses that to one.
    buck = collections.defaultdict(float)
    for p in prof:
        buck[int(p["strike"] // 25 * 25)] += p["net_gex_B"]
    per_strike = cross(sorted(buck.items()))
    return cumulative, per_strike


def run_gex_sweep(symbol="_SPX"):
    """Sweep GEX_MAX_DTE against the briefing's published OI. Prints only."""
    import os, json
    ref = GEX_SWEEP_REF
    try:
        env = os.environ.get("GEX_SWEEP_REF")
        if env:
            ref = {int(k): int(v) for k, v in json.loads(env).items()}
    except Exception as e:
        print(f"[sweep] GEX_SWEEP_REF unusable ({e}) — using the built-in 7 Aug table")

    try:
        spot, chain, note = _cboe_chain(symbol)
    except Exception as e:
        print(f"[sweep] fetch failed: {e}")
        return None
    if not spot or not chain:
        print(f"[sweep] {note}")
        return None

    band = float(os.environ.get("GEX_BAND", "0.10"))
    cuts = [int(x) for x in os.environ.get("GEX_SWEEP_CUTS", "30,60,90,120,180,365,3650").split(",")]
    print(f"[sweep] {len(chain)} contracts, spot {spot:,.2f}, band +/-{band*100:.0f}%")
    print(f"[sweep] reference: {len(ref)} strikes, flip {GEX_SWEEP_REF_FLIP}, "
          f"net {GEX_SWEEP_REF_NET_B}B")
    print()
    hdr = "  ".join(f"{k:>9,}" for k in sorted(ref))
    print(f"{'DTE':>5}  {'rows':>6}  {'capt%':>6}  {'netB':>8}  {'cum flip':>9}  {'25pt flip':>9}")
    print("-" * 60)

    best = None
    for dte in cuts:
        rows, _ = _cboe_rows(chain, spot, band, dte, int(os.environ.get("GEX_MIN_DTE", "1")))
        if len(rows) < 40:
            print(f"{dte:>5}  {len(rows):>6}  (too thin)")
            continue
        res = gex_engine(rows, spot, scale=1.0)
        if res.get("error"):
            print(f"{dte:>5}  {len(rows):>6}  engine: {res['error']}")
            continue
        prof = res.get("profile") or []
        oi = {}
        for p in prof:
            oi[p["strike"]] = oi.get(p["strike"], 0) + p.get("coi", 0) + p.get("poi", 0)
        got = sum(oi.get(k, 0) for k in ref)
        want = sum(ref.values())
        capt = 100.0 * got / want if want else 0.0
        netb = (res.get("overview") or {}).get("net_gex_B")
        cum, per = _flip_candidates(prof)
        near = lambda xs: min(xs, key=lambda x: abs(x - spot)) if xs else None
        c, p25 = near(cum), near(per)
        print(f"{dte:>5}  {len(rows):>6}  {capt:>5.0f}%  {netb:>8}  "
              f"{(f'{c:,.2f}' if c else '-'):>9}  {(f'{p25:,.2f}' if p25 else '-'):>9}")
        if best is None or abs(capt - 100) < abs(best[1] - 100):
            best = (dte, capt, netb, c, p25, oi)

    if not best:
        print("[sweep] no usable cutoff — the problem is not the expiry window")
        return None

    dte, capt, netb, c, p25, oi = best
    print()
    print(f"[sweep] closest coverage at DTE<={dte}: {capt:.0f}% of the reference OI")
    print(f"{'strike':>7}  {'reference':>11}  {'phoenix':>11}  {'capt':>6}")
    for k in sorted(ref):
        g = oi.get(k, 0)
        print(f"{k:>7}  {ref[k]:>11,}  {g:>11,}  {100.0*g/ref[k]:>5.0f}%")
    print()
    print(f"[sweep] net gamma  {netb}B  vs reference {GEX_SWEEP_REF_NET_B}B")
    for label, v in (("cumulative", c), ("per-strike 25pt", p25)):
        if v:
            print(f"[sweep] flip {label:<16} {v:>9,.2f}  vs {GEX_SWEEP_REF_FLIP} "
                  f"({v - GEX_SWEEP_REF_FLIP:+.2f}, "
                  f"{'below' if v < spot else 'ABOVE'} spot)")
    print("[sweep] nothing written — this is a diagnostic")
    return best


# ============================================================
# SIGNAL LOG — the experiment. APPEND-ONLY, NEVER OVERWRITTEN.
#
# WHY THIS EXISTS: the screener shows today's list and forgets it. So when a
# name we passed on runs 40%, or one we took stalls, there is no record it was
# ever on the list. Without that record there is no backtest, and without a
# backtest there is no answer to the only question that matters — do the six
# gates beat buying MTUM, net of costs?
#
# The mechanical 20-day-high breakout proxy used in phoenix_backtest.py is NOT
# equivalent to a real Phoenix signal. This file is what makes
# `--entry-source screener` possible.
#
# ONE FILE PER TRADING DAY, written by the FIRST run of that day and never
# rewritten. Two runs a day would otherwise give two different snapshots of
# "the signal" and we would not know which one we would have acted on. The
# 05:00 UTC run is pre-market, which is the decision point, so it wins.
# Set PHOENIX_SIGNALS_FORCE=1 to overwrite deliberately.
#
# Its value is a function of how long ago it started. A day not logged is gone.
# ============================================================

SIGNALS_DIR = "outputs/history"

# Recorded per candidate. Kept deliberately WIDE: adding a field later cannot
# retro-fill days already written, so anything plausibly useful goes in now.
SIGNAL_FIELDS = [
    "ticker", "name", "sector", "industry", "rank",
    "passer", "gates_passed", "missing_gate", "breakout",
    "trade_score", "invest_score",
    "price", "pos_vs_high", "surge", "dollar_vol_M", "atr14_pct", "mcap_B",
    "industry_mom_3m", "entry", "stop", "target", "rr",
    # profitability: recorded from day one so the gate-or-not decision can be
    # made on evidence in three months instead of on instinct today
    "profitability", "profitability_why", "net_margin", "gross_margin",
    "op_margin", "roe_ttm", "ocf_ttm_B", "fcf_ttm_B", "capex_ttm_B",
    "capex_pct_of_ocf", "debt_equity", "current_ratio", "pe_ttm",
    # persistence: builds forward, cannot be backfilled
    "first_seen", "days_on_list", "appearances", "continuous",
]


def _signal_row(c, book):
    row = {"book": book}
    for k in SIGNAL_FIELDS:
        if k in c:
            row[k] = c[k]
    lv = c.get("levels") or {}
    for k in ("support", "resistance"):
        if lv.get(k) is not None:
            row[k] = lv[k]
    return row


def write_signal_log(v2, regime=None, spx=None):
    """
    Append today's ranked screener output to outputs/history/signals_<date>.json.

    Returns the path written, or None if today is already logged.
    """
    import os, json
    from datetime import datetime, timezone

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    os.makedirs(SIGNALS_DIR, exist_ok=True)
    path = os.path.join(SIGNALS_DIR, f"signals_{day}.json")

    if os.path.exists(path) and os.environ.get("PHOENIX_SIGNALS_FORCE") != "1":
        print(f"[signals] {day} already logged — not overwriting "
              f"(PHOENIX_SIGNALS_FORCE=1 to replace)")
        return None

    trade = [_signal_row(c, "trade") for c in (v2.get("trade_ranked") or [])]
    invest = [_signal_row(c, "invest") for c in (v2.get("invest_ranked") or [])]
    meta = v2.get("meta") or {}

    doc = {
        "schema": "phoenix.signals/1",
        "date": day,
        "asof": _now(),
        # The market state the signal was generated in. Without this a future
        # backtest cannot ask "does the screener work better in some regimes?"
        "context": {
            "regime": (regime or {}).get("regime") if isinstance(regime, dict) else regime,
            "spx_close": spx,
            "gates": {k: meta.get(k) for k in
                      ("trade_candidates", "trade_near_misses", "trade_breakouts",
                       "ext_hard_capped", "invest_candidates")},
            "industries_passing": meta.get("industries_passing_v2"),
        },
        # The gate definitions AS THEY WERE TODAY, in full — thresholds AND the
        # scoring weights. If the rules are ever tuned, a backtest over old
        # signals must know which ruleset produced each day, or it silently
        # mixes two different strategies and reports the average as one edge.
        # Deep-copied via json round-trip so a later mutation of the module
        # constants cannot reach back into a file already written.
        "ruleset": {
            "stock": json.loads(json.dumps(STOCK)),
            "stock_v2": json.loads(json.dumps(STOCK_V2)),
            "validated": meta.get("validated"),
        },
        "counts": {"trade": len(trade), "invest": len(invest)},
        "trade": trade,
        "invest": invest,
    }

    with open(path, "w") as f:
        json.dump(_json_safe(doc), f, separators=(",", ":"), allow_nan=False)
    kb = os.path.getsize(path) / 1024
    print(f"[signals] {day}: {len(trade)} trade + {len(invest)} invest rows "
          f"-> {path} ({kb:.0f}KB)")
    return path



# ============================================================
# TIER 0 CHARTS — a weekly chart for EVERY universe name
#
# The 2-year weekly history for the whole universe already sits committed in
# stock_weekly.csv. This step cuts it into one tiny JSON per ticker
# (outputs/charts_w/<TK>.json) so a ticker page can render a real chart for
# ANY name — including ones the daily-coverage tiers never touch. Hash-gated:
# the ~2,900 files are written only when the CSV actually changes (weekly),
# so the daily run normally skips in milliseconds.
# ============================================================
CHARTS_W_DIR = os.path.join("outputs", "charts_w")


def run_universe_charts():
    """Cut stock_weekly.csv into per-ticker weekly chart JSONs (hash-gated)."""
    import hashlib, json as _json, os as _os
    src = "stock_weekly.csv"
    if not _os.path.exists(src):
        print("[chartsw] stock_weekly.csv not in repo — skipped")
        return
    h = hashlib.sha256(open(src, "rb").read()).hexdigest()[:16]
    _os.makedirs(CHARTS_W_DIR, exist_ok=True)
    man_path = _os.path.join(CHARTS_W_DIR, "_manifest.json")
    try:
        man = _json.load(open(man_path))
    except Exception:
        man = {}
    if man.get("csv_sha") == h and man.get("files"):
        print(f"[chartsw] unchanged (sha {h}) — {man['files']} files stand")
        return
    weekly = load_weekly_from_csv(src)
    n = 0
    n_fail = 0
    for tk, bars in weekly.items():
        try:
            safe = tk.replace("/", "-").replace(".", "-")   # same rule as charts/
            t = [b[0] for b in bars]
            c = [b[1] for b in bars]
            _json.dump(_json_safe({"tk": tk, "w": {"t": t, "c": c}}),
                       open(_os.path.join(CHARTS_W_DIR, f"{safe}.json"), "w"),
                       separators=(",", ":"))
            n += 1
        except Exception as e:
            n_fail += 1
            if n_fail == 1:
                print(f"[chartsw] first write failure ({tk}): {e}")
            continue
    if n_fail:
        print(f"[chartsw] {n_fail} tickers failed to write")
    _json.dump({"csv_sha": h, "files": n, "asof": _now()}, open(man_path, "w"))
    print(f"[chartsw] wrote {n} weekly chart files (csv sha {h})")


# ============================================================
# SENATE eFD — the official source, at last
#
# The community mirrors froze years ago (12 commits, total); every "Senate"
# row they can still serve dies on the lookback cutoff, and the merge's
# keep-on-zero defence then politely preserves the seed forever. This step
# goes to efdsearch.senate.gov itself: session cookie -> agreement POST ->
# the DataTables search endpoint -> each ELECTRONIC PTR parsed from its HTML
# table. Paper-scanned filings are counted as coverage gaps, loudly, never
# silently skipped. Results merge into congress_trades.json through the same
# dedupe as the House Clerk step; a cursor file stops re-parsing old PTRs.
#
# UNTESTED IN THE BUILD SANDBOX (no outbound POST here) — instrumented so its
# first Actions run tells the whole story in the log.
# ============================================================
SENATE_EFD = {
    "base": "https://efdsearch.senate.gov",
    "lookback_days": 120,        # PTRs to sweep per run (45-day filing lag + slack)
    "max_reports_per_run": 60,   # politeness cap; cursor carries the rest forward
    "timeout": 30,
}


def run_senate_efd():
    """Official Senate PTRs from efdsearch.senate.gov into congress_trades.json."""
    import json as _json, os as _os, re as _re, requests
    from datetime import datetime, timedelta

    cfg = SENATE_EFD
    s = requests.Session()
    s.headers.update({"User-Agent": "phoenix-smartmoney/1.0 (personal research)"})
    base = cfg["base"]

    # -- 1) session + agreement ------------------------------------------------
    try:
        r0 = s.get(f"{base}/search/home/", timeout=cfg["timeout"])
        csrf = s.cookies.get("csrftoken") or ""
        m = _re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', r0.text)
        tok = m.group(1) if m else csrf
        r1 = s.post(f"{base}/search/home/",
                    data={"prohibition_agreement": "1",
                          "csrfmiddlewaretoken": tok},
                    headers={"Referer": f"{base}/search/home/"},
                    timeout=cfg["timeout"])
        if r1.status_code not in (200, 302):
            print(f"[senate] agreement POST returned HTTP {r1.status_code} — aborting")
            return
        print("[senate] session established, agreement accepted")
    except Exception as e:
        print(f"[senate] cannot reach efdsearch ({e}) — keeping existing file")
        return

    # -- 2) search: electronic PTRs in the lookback window ----------------------
    start = (datetime.now() - timedelta(days=cfg["lookback_days"])).strftime("%m/%d/%Y")
    csrf = s.cookies.get("csrftoken") or ""
    rows = []
    try:
        for offset in (0, 100, 200):
            rq = s.post(f"{base}/search/report/data/",
                        data={"start": str(offset), "length": "100",
                              "report_types": "[11]", "filer_types": "[1]",
                              "submitted_start_date": f"{start} 00:00:00",
                              "submitted_end_date": "", "candidate_state": "",
                              "senator_state": "", "office_id": "",
                              "first_name": "", "last_name": ""},
                        headers={"Referer": f"{base}/search/",
                                 "X-CSRFToken": csrf},
                        timeout=cfg["timeout"])
            if rq.status_code != 200:
                print(f"[senate] search HTTP {rq.status_code} at offset {offset}")
                break
            batch = (rq.json() or {}).get("data") or []
            rows.extend(batch)
            if len(batch) < 100:
                break
        print(f"[senate] search window {start} -> today: {len(rows)} filings listed")
    except Exception as e:
        print(f"[senate] search failed ({e}) — keeping existing file")
        return
    if not rows:
        print("[senate] zero filings listed — nothing to do")
        return

    # -- 3) parse each ELECTRONIC ptr; count paper as gaps ----------------------
    seen_path = _os.path.join(OUTPUTS_DIR, "senate_efd_seen.json")
    try:
        seen = set(_json.load(open(seen_path)))
    except Exception:
        seen = set()
    by_ticker, n_new, n_paper, n_parsed = {}, 0, 0, 0
    _names = _sec_name_ticker_map()

    def _clean(x):
        return _re.sub(r"<[^>]+>", "", x or "").strip()

    for row in rows:
        try:
            first, last = _clean(row[0]), _clean(row[1])
            link_html, filed = row[3], _clean(row[4])
        except Exception:
            continue
        m = _re.search(r'href="([^"]+)"', link_html or "")
        if not m:
            continue
        href = m.group(1)
        if "/paper/" in href:
            n_paper += 1
            continue
        rid = href.rstrip("/").split("/")[-1]
        if rid in seen:
            continue
        if n_parsed >= cfg["max_reports_per_run"]:
            break
        member = f"{first} {last}".strip()
        try:
            rp = s.get(base + href, timeout=cfg["timeout"])
            if rp.status_code != 200:
                print(f"[senate] PTR {rid}: HTTP {rp.status_code}")
                continue
            n_parsed += 1
            trs = _re.findall(r"<tr[^>]*>(.*?)</tr>", rp.text, _re.S)
            got = 0
            for tr in trs:
                tds = [_clean(td) for td in
                       _re.findall(r"<td[^>]*>(.*?)</td>", tr, _re.S)]
                if len(tds) < 8:
                    continue
                # eFD PTR table: #, date, owner, ticker, asset, type, amount, comment
                _n, tdate, owner, tk, asset, ttype, amount = tds[:7]
                tk = (tk or "").strip().upper()
                if tk in ("--", "N/A", ""):
                    m2 = _re.search(r"\(([A-Z][A-Z0-9.\-]{0,5})\)", asset or "")
                    tk = m2.group(1) if m2 else \
                        (_cusip_ticker_from_universe(asset, _names) or "")
                if not tk:
                    continue
                side = ("buy" if "purchase" in ttype.lower() else
                        "sell" if "sale" in ttype.lower() else None)
                if not side:
                    continue
                td = None
                for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
                    try:
                        td = datetime.strptime(tdate.strip(), fmt); break
                    except Exception:
                        continue
                by_ticker.setdefault(tk, []).append({
                    "member": member, "chamber": "Senate",
                    "date": td.strftime("%Y-%m-%d") if td else tdate,
                    "reported": filed, "side": side, "amount": amount,
                    "owner": owner, "asset_type": "Stock",
                    "ptr": rid})
                got += 1; n_new += 1
            seen.add(rid)
            print(f"[senate] {member}: PTR {rid} -> {got} transactions")
        except Exception as e:
            print(f"[senate] PTR {rid} failed ({e})")
            continue

    print(f"[senate] parsed {n_parsed} electronic PTRs, {n_paper} paper filings "
          f"(coverage gaps), {n_new} transactions extracted")
    if n_new == 0:
        _json.dump(sorted(seen), open(seen_path, "w"))
        print("[senate] no new transactions — congress_trades.json untouched")
        return

    # -- 4) merge into congress_trades.json (same dedupe as house_ptr) ----------
    ep = _os.path.join(OUTPUTS_DIR, "congress_trades.json")
    try:
        existing = _json.load(open(ep)).get("tickers", {})
    except Exception:
        existing = {}
    merged = {tk: list(rw) for tk, rw in existing.items()}
    added = 0
    for tk, rws in by_ticker.items():
        have = {(r.get("member"), r.get("date"), r.get("side"), r.get("amount"))
                for r in merged.get(tk, [])}
        for r in rws:
            k = (r["member"], r["date"], r["side"], r["amount"])
            if k not in have:
                merged.setdefault(tk, []).append(r); have.add(k); added += 1
    for tk in merged:
        merged[tk].sort(key=lambda x: x.get("date", ""), reverse=True)
    write_json("congress_trades", {
        "asof": _now(),
        "source": "House Clerk PTR (official) + Senate eFD (official) + committed history",
        "note": "45-day disclosure lag; amounts are ranges as filed. Display and "
                "backtests scope to large caps; collection keeps every ticker a "
                "filing yields.",
        "ticker_count": len(merged),
        "trade_count": sum(len(v) for v in merged.values()),
        "tickers": merged})
    _json.dump(sorted(seen), open(seen_path, "w"))
    print(f"[senate] merged {added} new Senate trades "
          f"({sum(len(v) for v in merged.values())} total on file)")


# ============================================================
# THE WIRE — the 7am news briefing, committed like Elliott's PDF
#
# The morning chat writes one HTML file per account into wire/
# (phoenix-wire-<account>-YYYY-MM-DD.html). This step turns the newest
# file per account into outputs/wire.json for the Edition's two news
# sections. ADDITIVE by design: the wire never replaces an engine
# section — the tape, the map (GEX histogram + VIX term), the calendar
# all stay engine-owned.
#
# Parse order: (1) an embedded <script type="application/json"
# id="wire-data"> payload if the morning chat provides one — the robust
# contract; (2) best-effort HTML parsing of the known wire markup;
# (3) if nothing parses, the publish gate KEEPS the previous wire.json
# rather than shipping an empty one. A wire from a previous day still
# publishes with its own date — the app renders the stale badge.
# ============================================================
WIRE_DIR = "wire"


def _wire_strip(s):
    """Tags out, entities resolved. PURE."""
    import re as _re
    from html import unescape
    return unescape(_re.sub(r"<[^>]+>", "", s or "")).strip()


def _wire_keep_b(s):
    """Reduce inner HTML to text plus <b>/<i>/<em> only. PURE."""
    import re as _re
    from html import unescape
    s = _re.sub(r"<(?!/?(?:b|i|em)\b)[^>]+>", "", s or "")
    return unescape(s).strip()


def _wire_from_embedded_json(raw):
    """The preferred contract: a JSON payload inside the wire HTML."""
    import re as _re, json as _json
    m = _re.search(r'<script type="application/json" id="wire-data">(.*?)</script>',
                   raw, _re.S)
    if not m:
        return None
    try:
        d = _json.loads(m.group(1))
        return d if isinstance(d, dict) else None
    except Exception as e:
        print(f"[wire] embedded JSON present but unreadable ({e}) — "
              f"falling back to HTML parse")
        return None


def _wire_parse_html(raw):
    """Best-effort parse of the known phoenix-wire markup. PURE."""
    import re as _re
    out = {"headline": None, "standfirst": None, "positions": None,
           "themes": [], "world": None, "ondeck": None}
    m = _re.search(r'class="ed-call"[^>]*>(.*?)</div>', raw, _re.S)
    if m:
        out["headline"] = _wire_strip(m.group(1))
    m = _re.search(r'class="ed-vwhy"[^>]*>(.*?)</div>', raw, _re.S)
    if m:
        out["standfirst"] = _wire_strip(m.group(1))
    m = _re.search(r'<p class="ed-body">\s*<b>On deck\.?</b>(.*?)</p>', raw, _re.S)
    if m:
        out["ondeck"] = _wire_keep_b(m.group(1))
    for ch in _re.split(r'<div class="ed-sec">', raw)[1:]:
        k = _re.search(r'<div class="ed-k"><span>(.*?)</span>'
                       r'<span class="ed-n">(.*?)</span>', ch, _re.S)
        kicker = _wire_strip(k.group(1)) if k else ""
        right = _wire_strip(k.group(2)) if k else ""
        t = _re.search(r'<h2 class="ed-hl[^"]*">(.*?)</h2>', ch, _re.S)
        title = _wire_strip(t.group(1)) if t else ""
        items = []
        for im in _re.finditer(
                r'<div class="ed-w"><span class="w-tag ?([a-z\- ]*)">(.*?)</span>'
                r'<span>(.*?)</span><span class="w-when">(.*?)</span></div>',
                ch, _re.S):
            it = {"tone": im.group(1).strip(), "tag": _wire_strip(im.group(2)),
                  "html": _wire_keep_b(im.group(3)), "when": _wire_strip(im.group(4))}
            tk = _re.match(r'<b>([A-Z][A-Z0-9.\-]{0,6})</b>', im.group(3).strip())
            if tk:
                it["ticker"] = tk.group(1)
            items.append(it)
        note = None
        n = _re.search(r'<p class="ed-body">(?!\s*<b>On deck)(.*?)</p>', ch, _re.S)
        if n:
            note = _wire_keep_b(n.group(1))
        low = kicker.lower()
        if low.startswith("your positions"):
            body = None
            if not items:
                b = _re.search(r'</h2>\s*<p[^>]*>(.*?)</p>', ch, _re.S)
                body = _wire_keep_b(b.group(1)) if b else note
            out["positions"] = {"right": right, "title": title,
                                "items": items, "note": note if items else body}
        elif low.startswith("the world"):
            out["world"] = {"kicker": kicker, "right": right, "title": title,
                            "items": items, "note": note}
        elif (not kicker) or low.startswith("the tape"):
            continue
        else:
            out["themes"].append({"kicker": kicker, "right": right,
                                  "title": title, "items": items, "note": note})
    return out


def _wire_sanitize(s):
    """
    Strip anything executable or style-bearing from committed HTML before the
    app injects it. The weekly is written in the Edition's own class language,
    so its section markup renders natively — but its <style> block carries a
    LIGHT :root and would repaint the app if it ever came along for the ride.
    PURE.
    """
    import re as _re
    s = s or ""
    for tag in ("script", "style", "iframe", "object", "embed", "svg"):
        s = _re.sub(rf"<{tag}\b.*?</{tag}>", "", s, flags=_re.S | _re.I)
        s = _re.sub(rf"<{tag}\b[^>]*/?>", "", s, flags=_re.I)
    s = _re.sub(r"<(link|meta)\b[^>]*>", "", s, flags=_re.I)
    s = _re.sub(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", s, flags=_re.I)
    s = _re.sub(r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2", r'\1="#"',
                s, flags=_re.I)
    return s.strip()


def _wire_parse_weekly(raw):
    """
    Parse a weekly brief into ORDERED sections, keeping each section's markup
    verbatim (sanitized) rather than reducing it to items.

    The weekly carries element types the daily never uses — tape cards, book
    rows with R-multiples, a calendar strip. Reducing those to a fixed schema
    would silently drop the richest parts, and would break again every time the
    Saturday brief gains a new block. Capturing the section body instead means
    the app renders whatever was written, and a format change costs nothing.
    PURE.
    """
    import re as _re
    out = {"headline": None, "standfirst": None, "sections": []}
    m = _re.search(r'class="ed-call"[^>]*>(.*?)</div>', raw, _re.S)
    if m:
        out["headline"] = _wire_strip(m.group(1))
    m = _re.search(r'class="ed-vwhy"[^>]*>(.*?)</div>', raw, _re.S)
    if m:
        out["standfirst"] = _wire_strip(m.group(1))
    chunks = _re.split(r'<div class="ed-sec"[^>]*>', raw)[1:]
    for ch in chunks:
        # the last section runs to the page furniture — cut it there
        for stop in ('<div class="ed-jump"', '<div class="ed-colophon"'):
            i = ch.find(stop)
            if i > 0:
                ch = ch[:i]
        k = _re.search(r'<div class="ed-k">\s*<span>(.*?)</span>\s*'
                       r'<span class="ed-n">(.*?)</span>\s*</div>', ch, _re.S)
        kicker = _wire_strip(k.group(1)) if k else ""
        right = _wire_strip(k.group(2)) if k else ""
        body = ch[k.end():] if k else ch
        body = _wire_sanitize(body)
        if not (kicker or body):
            continue
        out["sections"].append({"kicker": kicker, "right": right, "html": body})
    return out


def run_wire():
    """Publish outputs/wire.json: the newest daily wire AND weekly brief per account."""
    import glob as _g, re as _re, os as _os
    files = _g.glob(_os.path.join(WIRE_DIR, "phoenix-*-*.html"))
    if not files:
        print(f"[wire] no files under {WIRE_DIR}/ — nothing to publish. Commit "
              f"phoenix-wire-<account>-YYYY-MM-DD.html (daily) or "
              f"phoenix-weekly-<account>-YYYY-MM-DD.html (weekend) to feed it.")
        return
    best, best_wk = {}, {}
    for p in sorted(files):
        m = _re.search(r"phoenix-(wire|weekly)-([a-z0-9_]+)-"
                       r"(\d{4}-\d{2}-\d{2})\.html$", p)
        if not m:
            print(f"[wire] skipping {p} — name must be phoenix-wire-<account>-"
                  f"YYYY-MM-DD.html or phoenix-weekly-<account>-YYYY-MM-DD.html")
            continue
        kind, acct, d = m.group(1), m.group(2), m.group(3)
        tgt = best if kind == "wire" else best_wk
        if acct not in tgt or d > tgt[acct][0]:
            tgt[acct] = (d, p)
    accounts = {}
    for acct, (d, p) in sorted(best.items()):
        try:
            raw = open(p, encoding="utf-8", errors="ignore").read()
        except Exception as e:
            print(f"[wire] {acct}: cannot read {p}: {e}")
            continue
        payload = _wire_from_embedded_json(raw)
        parser = "embedded-json"
        if payload is None:
            payload = _wire_parse_html(raw)
            parser = "html-parse"
        n_items = (len((payload.get("positions") or {}).get("items") or [])
                   + sum(len(t.get("items") or []) for t in payload.get("themes") or [])
                   + len((payload.get("world") or {}).get("items") or []))
        payload.update({"date": d, "file": p, "parser": parser})
        accounts[acct] = payload
        print(f"[wire] {acct}: daily {d} via {parser} — "
              f"headline {'ok' if payload.get('headline') else 'MISSING'}, "
              f"{len(payload.get('themes') or [])} themes, {n_items} items")

    # ---- the weekly brief: same folder, its own cadence --------------------
    for acct, (d, p) in sorted(best_wk.items()):
        try:
            raw = open(p, encoding="utf-8", errors="ignore").read()
        except Exception as e:
            print(f"[wire] {acct} weekly: cannot read {p}: {e}")
            continue
        wk = _wire_parse_weekly(raw)
        wk.update({"date": d, "file": p})
        if not wk["sections"] and not wk.get("headline"):
            print(f"[wire] {acct} weekly: nothing recognised in {p} — skipped")
            continue
        accounts.setdefault(acct, {"date": None, "parser": "weekly-only"})
        accounts[acct]["weekly"] = wk
        print(f"[wire] {acct}: weekly {d} — {len(wk['sections'])} sections "
              f"({', '.join(s['kicker'] for s in wk['sections'][:4])})")
    data = {"asof": _now(),
            "date": max((v["date"] for v in accounts.values()), default=None),
            "accounts": accounts}

    def _validate_wire(pl):
        probs = []
        if not pl.get("accounts"):
            probs.append("no accounts parsed from wire/")
        for a, v in (pl.get("accounts") or {}).items():
            n = (len((v.get("positions") or {}).get("items") or [])
                 + sum(len(t.get("items") or []) for t in (v.get("themes") or []))
                 + len((v.get("world") or {}).get("items") or []))
            if not v.get("headline") and n == 0 and not v.get("weekly"):
                probs.append(f"{a}: no headline and no items — the parser found "
                             f"nothing it recognises in {v.get('file')}")
        return probs

    write_json_guarded("wire", data, _validate_wire)


# ============================================================
# THE DAY SCORE — the coach's #1 ask, built under two non-negotiables:
#   1. AUDITABLE BY CONSTRUCTION. The score never renders without its
#      components. A composite that hides its inputs is the black box this
#      system was praised for not being.
#   2. LOGGED FROM DAY ONE. Weights are provisional; the daily log is the
#      asset — in three months it can be tested against what the days
#      actually did. Same start-the-clock property as the signal log.
#
# It reads only committed outputs (macro, gex, vix_term, trades), so it can
# never disagree with what the tiles show. If the gamma data is stale, the
# score is CAPPED, not silently computed on yesterday's structure — that
# exact failure produced a bad SNOW call on 6 Aug.
# ============================================================

def run_dayscore():
    import os, json
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _read(name):
        try:
            return json.load(open(os.path.join(OUTPUTS_DIR, name + ".json")))
        except Exception:
            return {}

    macro, gex, vix, trades = _read("macro"), _read("gex"), _read("vix_term"), _read("trades")
    comps, flags = [], []

    def comp(name, pts, mx, why):
        pts = max(0.0, min(float(pts), mx))
        comps.append({"name": name, "pts": round(pts, 1), "max": mx, "why": why})
        return pts

    total = 0.0

    # ---- 1. Regime (0-25): a named regime plus its confidence ----------------
    regime = macro.get("regime")
    conf = float(macro.get("confidence") or 0)
    if regime and regime != "UNKNOWN":
        total += comp("Regime", 10 + conf / 100.0 * 15, 25,
                      f"{regime}, confidence {conf:.0f}/100")
    else:
        total += comp("Regime", 0, 25, "no named regime")
        flags.append("regime unknown")

    # ---- 2. Gamma structure (0-30): sign, room to the flip, corridor ---------
    ov = gex.get("overview") or {}
    lv = gex.get("levels") or {}
    net = ov.get("net_gex_B")
    spot = ov.get("spx_spot")
    flip = ov.get("gamma_flip")
    cw, pw = lv.get("call_wall"), lv.get("put_wall")
    if net is not None and net > 0 and spot:
        pts, why = 14.0, [f"positive gamma {net:+.1f}B"]
        if flip:
            dist = abs(spot / flip - 1) * 100
            pts += min(dist, 1.5) / 1.5 * 10
            why.append(f"{dist:.1f}% above the flip")
        if cw and pw and cw > pw:
            corridor = (cw - pw) / spot * 100
            pts += min(corridor, 1.0) / 1.0 * 6
            why.append(f"corridor {pw:.0f}-{cw:.0f} ({corridor:.1f}%)")
        total += comp("Gamma", pts, 30, ", ".join(why))
    elif net is not None and spot:
        total += comp("Gamma", 3, 30, f"NEGATIVE gamma {net:+.1f}B — moves extend")
        flags.append("negative gamma")
    else:
        total += comp("Gamma", 0, 30, "no gamma data")
        flags.append("gamma missing")

    # ---- 3. Volatility (0-20): level and term shape --------------------------
    vspot = vix.get("spot")
    fut = vix.get("futures") or []
    if vspot:
        lvl = 10 if vspot < 16 else 7 if vspot < 20 else 3 if vspot < 25 else 0
        shape, spts = "term shape unknown", 5
        if len(fut) >= 2:
            front, back = fut[0]["value"], fut[-1]["value"]
            if front < back:
                shape, spts = f"contango {front:.1f}->{back:.1f}", 10
            else:
                shape, spts = f"BACKWARDATION {front:.1f}->{back:.1f}", 0
                flags.append("vix backwardation")
        total += comp("Volatility", lvl + spts, 20, f"VIX {vspot:.1f}, {shape}")
    else:
        total += comp("Volatility", 5, 20, "no vix data — neutral")

    # ---- 4. Book state (0-25): heat headroom and the loss streak -------------
    rows = (trades.get("trades") or [])
    open_risk_R = 0.0
    for t in rows:
        if t.get("status") != "open" or t.get("managed"):
            continue
        if (t.get("account") or "gabriel") != "gabriel":
            continue
        e, s0 = t.get("entry"), t.get("current_stop") or t.get("initial_stop")
        q = t.get("qty") or 0
        rd = ((t.get("account_size") or 2000) * 0.01) or 20
        if e and s0 and q:
            open_risk_R += max(0.0, (e - s0) * q) / rd
    head = max(0.0, (3.0 - open_risk_R) / 3.0) * 15
    closed = sorted([t for t in rows if t.get("status") == "closed"
                     and (t.get("account") or "gabriel") == "gabriel"],
                    key=lambda t: t.get("exit_date") or "")
    streak = 0
    for t in reversed(closed):
        e, s0, x = t.get("entry"), t.get("initial_stop"), t.get("exit_price")
        if not (e and s0 and x and e > s0):
            break
        if (x - e) / (e - s0) < 0:
            streak += 1
        else:
            break
    spts = 10 if streak < 3 else 0
    if streak >= 3:
        flags.append(f"{streak}-loss streak — cool-off")
    total += comp("Book", head + spts,
                  25, f"heat {open_risk_R:.2f}R of 3R, loss streak {streak}")

    # ---- staleness cap -------------------------------------------------------
    capped = None
    gex_day = (gex.get("asof") or "")[:10]
    if gex_day and gex_day != today:
        if total > 45:
            capped = f"gamma data is from {gex_day} — score capped at 45"
            total = 45.0
            flags.append("stale gamma")

    verdict = ("CLEAR" if total >= 70 else
               "TRADE CAREFULLY" if total >= 45 else "STAND DOWN")
    out = {"asof": _now(), "date": today, "score": round(total, 1),
           "verdict": verdict, "capped": capped, "flags": flags,
           "components": comps,
           "weights_note": "provisional weights — the daily log exists so they "
                           "can be validated against outcomes, not defended"}
    write_json("dayscore", out)

    # one line per day, first run wins (PHOENIX_DAYSCORE_FORCE=1 to replace)
    try:
        os.makedirs(os.path.join(OUTPUTS_DIR, "history"), exist_ok=True)
        lp = os.path.join(OUTPUTS_DIR, "history", "dayscore_log.jsonl")
        seen = False
        if os.path.exists(lp) and os.environ.get("PHOENIX_DAYSCORE_FORCE") != "1":
            with open(lp) as f:
                seen = any(json.loads(l).get("date") == today
                           for l in f if l.strip())
        if not seen:
            with open(lp, "a") as f:
                f.write(json.dumps({"date": today, "score": out["score"],
                                    "verdict": verdict,
                                    "c": {c["name"]: c["pts"] for c in comps},
                                    "flags": flags}) + "\n")
    except Exception as e:
        print(f"[dayscore] log append failed: {e}")

    print(f"[dayscore] {out['score']:.0f}/100 {verdict}"
          + (f" (CAPPED: {capped})" if capped else "")
          + (f" · flags: {', '.join(flags)}" if flags else ""))
    return out


def run_signals_index():
    """
    Rebuild outputs/history/signals_index.json — one line per logged day.

    Cheap, and it means the app or a backtest can see the span of the record
    without reading every file.
    """
    import os, json, glob
    os.makedirs(SIGNALS_DIR, exist_ok=True)
    days = []
    for p in sorted(glob.glob(os.path.join(SIGNALS_DIR, "signals_*.json"))):
        try:
            d = json.load(open(p))
        except Exception as e:
            print(f"[signals] {os.path.basename(p)} unreadable: {e}")
            continue
        days.append({"date": d.get("date"), "file": os.path.basename(p),
                     "trade": (d.get("counts") or {}).get("trade"),
                     "invest": (d.get("counts") or {}).get("invest"),
                     "regime": (d.get("context") or {}).get("regime")})
    out = {"schema": "phoenix.signals.index/1", "asof": _now(),
           "days": len(days), "first": days[0]["date"] if days else None,
           "last": days[-1]["date"] if days else None, "log": days}
    with open(os.path.join(SIGNALS_DIR, "signals_index.json"), "w") as f:
        json.dump(out, f, indent=1)
    if days:
        print(f"[signals] index: {len(days)} days, {days[0]['date']} -> {days[-1]['date']}")
    else:
        print("[signals] index: no days logged yet")
    return len(days)


# ============================================================
# PROFITABILITY FLAG — DISPLAYED AND LOGGED, NEVER GATED.
#
# The six trade gates contain no financial data at all: `tradability` checks
# market cap and dollar volume, which is LIQUIDITY, not quality. A company
# burning cash with no revenue passes all six if the chart is right.
#
# That is defensible — momentum arguably works BECAUSE it ignores fundamentals
# — but it was never visible. So: compute it, show it, log it, gate on nothing.
# In three months the signal log can answer whether profitable names actually
# outperformed IN THIS SCREENER, and the decision to gate gets made on evidence
# instead of instinct.
#
# "unknown" is a real answer and is never silently treated as either pass or
# fail: fundamentals_min_cap is $2B while the trade gate is $1B, so a slice of
# trade candidates genuinely has no financial data.
# ============================================================

def profitability_flag(qs):
    """
    Returns {"state": ..., ...evidence}. DISPLAYED AND LOGGED, NEVER GATED.

    THE TEST IS OPERATING CASH FLOW, NOT FREE CASH FLOW.

    FCF = operating cash flow - capex. A hyperscaler putting $50B into AI data
    centres prints negative FCF while being hugely profitable, and that spending
    is growth investment, not distress. Judging on FCF would rank META or MSFT
    mid-buildout below a company with no ambitions, which is the opposite of the
    truth. Only OCF separates the two cases:

        OCF positive, FCF negative  -> INVESTING. Earning cash, choosing to
                                       spend more than it earns on capacity.
        OCF negative                -> BURNING. No choice in the matter.

    States (descriptive, not a ranking):
      profitable  earning on the income statement, generating operating cash
      investing   same, but capex exceeds OCF - a capex cycle, NOT a downgrade
      marginal    thin, inconsistent, or profit WITHOUT operating cash (the
                  earnings-quality red flag: accounting profit, no cash behind it)
      lossmaking  negative margins and negative operating cash
      unknown     no data - a real third answer, never silently pass or fail
    """
    qs = qs or []
    if not qs:
        return {"state": "unknown", "why": "no quarterly data"}

    def tail(key, n=4):
        return [q.get(key) for q in qs[-n:] if q.get(key) is not None]

    margins = tail("net_margin")
    op_margins = tail("op_margin")
    ocf = tail("ocf_B")
    fcf = tail("fcf_B")
    roe_q = tail("roe")

    # ROE in the CSV is QUARTERLY; the invest gates annualise before comparing
    # to an annual floor, and so must we or the number means nothing.
    if len(roe_q) >= 4:
        roe_ttm = sum(roe_q[-4:])
    elif len(roe_q) >= 2:
        roe_ttm = sum(roe_q) / len(roe_q) * 4
    else:
        roe_ttm = None

    ocf_ttm = sum(ocf) if len(ocf) >= 2 else None
    fcf_ttm = sum(fcf) if len(fcf) >= 2 else None
    latest_margin = margins[-1] if margins else None
    pos_margin_q = sum(1 for v in margins if v > 0)

    # implied capex, and how much of operating cash it consumes
    capex_ttm = (ocf_ttm - fcf_ttm) if (ocf_ttm is not None and fcf_ttm is not None) else None
    capex_intensity = (round(100.0 * capex_ttm / ocf_ttm, 0)
                       if (capex_ttm is not None and ocf_ttm and ocf_ttm > 0) else None)

    gross_margins = tail("gross_margin")
    ni = tail("net_income_B")
    ni_ttm = sum(ni) if len(ni) >= 4 else None
    de = [q.get("debt_equity") for q in qs[-2:] if q.get("debt_equity") is not None]
    cr = [q.get("current_ratio") for q in qs[-2:] if q.get("current_ratio") is not None]

    ev = {
        "net_margin": latest_margin,
        "gross_margin": gross_margins[-1] if gross_margins else None,
        "op_margin": op_margins[-1] if op_margins else None,
        "net_income_ttm_B": round(ni_ttm, 2) if ni_ttm is not None else None,
        "debt_equity": de[-1] if de else None,
        "current_ratio": cr[-1] if cr else None,
        "margin_pos_quarters": pos_margin_q if margins else None,
        "margin_quarters_known": len(margins),
        "roe_ttm": round(roe_ttm, 1) if roe_ttm is not None else None,
        "ocf_ttm_B": round(ocf_ttm, 2) if ocf_ttm is not None else None,
        "fcf_ttm_B": round(fcf_ttm, 2) if fcf_ttm is not None else None,
        "capex_ttm_B": round(capex_ttm, 2) if capex_ttm is not None else None,
        "capex_pct_of_ocf": capex_intensity,
    }

    if latest_margin is None and ocf_ttm is None:
        return dict(ev, state="unknown", why="no margin or cash-flow data")

    earning = (latest_margin is not None and latest_margin > 0
               and pos_margin_q >= max(1, len(margins) - 1))
    cash_positive = ocf_ttm is not None and ocf_ttm > 0
    cash_negative = ocf_ttm is not None and ocf_ttm <= 0

    if earning and cash_positive:
        if fcf_ttm is not None and fcf_ttm < 0:
            state = "investing"
            why = (f"profitable, OCF {ocf_ttm:+.1f}B, but capex {capex_ttm:.1f}B "
                   f"({capex_intensity:.0f}% of OCF) takes FCF negative "
                   f"- spending, not struggling")
        else:
            state = "profitable"
            why = f"positive margin, OCF {ocf_ttm:+.1f}B"
            if capex_intensity is not None and capex_intensity >= 50:
                why += f", heavy capex ({capex_intensity:.0f}% of OCF)"
    elif earning and cash_negative:
        # Accounting profit with no operating cash behind it. This one IS a
        # warning: it is an earnings-quality problem, not a capex cycle.
        state = "marginal"
        why = f"positive margin but OCF {ocf_ttm:+.1f}B - profit without cash"
    elif earning:
        state, why = "profitable", "positive margin; no cash-flow data"
    elif latest_margin is not None and latest_margin <= 0 and pos_margin_q == 0:
        state = "lossmaking"
        why = f"negative net margin in all {len(margins)} known quarters"
        if cash_positive:
            state = "marginal"
            why += f", though OCF is {ocf_ttm:+.1f}B"
    elif latest_margin is None and ocf_ttm is not None:
        state = "marginal" if ocf_ttm > 0 else "lossmaking"
        why = f"no margin data; TTM operating cash {ocf_ttm:+.1f}B"
    else:
        state, why = "marginal", "profitability inconsistent across quarters"
    return dict(ev, state=state, why=why)


# ============================================================
# DAYS ON LIST — how long has this name been saying the same thing?
#
# Every gate is slow by construction (40-week MA, 30-week MA, 104-week high,
# 3-month industry momentum) and the screener scores weekly bars, so the same
# names persist for months. That is what a trend screen SHOULD do. The problem
# is that a name in its twelfth week renders identically to one that appeared
# this morning, so "still valid" and "stale" look the same.
#
# Read from the signal log. It is empty today, so every name reads day 1 and
# the history builds forward — there is no way to backfill it.
# ============================================================

def _signal_history_appearances(lookback_days=180):
    """{ticker: [dates it passed all six gates]}, oldest first."""
    import os, json, glob
    out = {}
    files = sorted(glob.glob(os.path.join(SIGNALS_DIR, "signals_*.json")))[-lookback_days:]
    for p in files:
        try:
            d = json.load(open(p))
        except Exception:
            continue
        day = d.get("date")
        for r in (d.get("trade") or []):
            if r.get("passer") and r.get("ticker"):
                out.setdefault(r["ticker"], []).append(day)
    return out


def annotate_persistence(trade_ranked):
    """Add first_seen / days_on_list / appearances / continuous to each row."""
    from datetime import datetime, timezone
    hist = _signal_history_appearances()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for c in trade_ranked:
        days = hist.get(c.get("ticker")) or []
        if not days:
            c["first_seen"] = today if c.get("passer") else None
            c["days_on_list"] = 1 if c.get("passer") else 0
            c["appearances"] = 1 if c.get("passer") else 0
            c["continuous"] = True
            continue
        first = days[0]
        try:
            span = (datetime.strptime(today, "%Y-%m-%d")
                    - datetime.strptime(first, "%Y-%m-%d")).days + 1
        except Exception:
            span = len(days)
        c["first_seen"] = first
        c["days_on_list"] = span
        c["appearances"] = len(days) + (1 if c.get("passer") else 0)
        # A name that dropped off and came back is a NEW signal, not an old one
        # still running. Without this they would be indistinguishable.
        c["continuous"] = (len(days) >= span - 1)
    return trade_ranked




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


def write_meta(source_flags=None, warnings=None, progress=None):
    return write_json("meta", {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "source_flags": source_flags or {}, "warnings": warnings or [],
        "progress": progress or [], "publish_holds": list(PUBLISH_HOLDS),
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
        d = agg.setdefault(round(K), {"gex": 0.0, "vex": 0.0, "cex": 0.0,
                                      "coi": 0.0, "poi": 0.0,
                                      "cgex": 0.0, "pgex": 0.0})
        _dg = g * oi * 100 * (spot ** 2) * 0.01 / scale     # unsigned dollar gamma
        d["gex"] += _dg * sign
        d["vex"] += vn * oi * 100 * spot * 0.01 * sign / scale
        d["cex"] += (cm * oi * 100 * spot * sign / scale) / 365.0
        if row["kind"] == "call":
            d["coi"] += oi
            d["cgex"] += _dg
        else:
            d["poi"] += oi
            d["pgex"] += _dg

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
            "call_gex_B": round(v.get("cgex", 0.0) / 1e9, 3),
            "put_gex_B": round(v.get("pgex", 0.0) / 1e9, 3),
            "coi": 0 if v["coi"] != v["coi"] else int(v["coi"]),
            "poi": 0 if v["poi"] != v["poi"] else int(v["poi"]),
        })

    # GAMMA FLIP - solved, not approximated.
    #
    # WHAT WAS WRONG: cumulating net gamma across strikes and looking for a sign
    # change answers "at which strike does the running total cross zero". That is
    # a different question from the one the flip asks, which is: at what SPOT
    # PRICE would total dealer gamma be zero? The two coincide only by accident,
    # which is why the approximation kept returning levels above spot in positive
    # gamma, and why the consistency guard then had to paper over it with a
    # meaningless window edge.
    #
    # Correct method: re-evaluate TOTAL gamma at candidate spot levels and find
    # where it crosses zero. Greeks are recomputed at each candidate, so the
    # answer is the real boundary rather than an artefact of strike ordering.
    def _total_gamma_at(S):
        tot = 0.0
        for row in chain:
            K = row["strike"]
            iv = row.get("iv") or 0
            oi = row.get("open_interest") or 0
            if iv <= 0 or oi <= 0:
                continue
            T = max(row.get("T_years", 0), 1e-6)
            g, _v, _c = compute_greeks(S, K, T, r, q, iv)
            sign = 1.0 if row["kind"] == "call" else -1.0
            tot += g * oi * 100 * (S ** 2) * 0.01 * sign / scale
        return tot

    total_net = sum(p["net_gex_B"] for p in profile)
    lo_s, hi_s = strikes[0], strikes[-1]
    steps = 24
    grid = [lo_s + (hi_s - lo_s) * t / steps for t in range(steps + 1)]
    vals = [_total_gamma_at(S) for S in grid]
    flip = None
    for t in range(1, len(grid)):
        a, b = vals[t - 1], vals[t]
        if (a < 0 <= b) or (a > 0 >= b):
            lo_b, hi_b, fa = grid[t - 1], grid[t], a
            for _ in range(40):
                mid = (lo_b + hi_b) / 2
                fm = _total_gamma_at(mid)
                if (fa < 0) == (fm < 0):
                    lo_b, fa = mid, fm
                else:
                    hi_b = mid
            cand = round((lo_b + hi_b) / 2, 2)
            if flip is None or abs(cand - spot) < abs(flip - spot):
                flip = cand
    if flip is None:
        print(f"[gex] total gamma never crosses zero between {lo_s} and {hi_s} "
              f"(net {total_net:+.1f}B) - no flip exists in the listed range")


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
    call_magnet = max(above, key=lambda p: p["coi"]) if above else None
    # (magnet computed over the FULL window, so it may sit outside the band)
    put_magnet = max(below, key=lambda p: p["poi"]) if below else None
    thr = GEX.get("wall_threshold", 0.30)
    # The ordering rule below was already right, but the THRESHOLD was computed
    # against the whole search window. Round strikes (7000, 8000) carry huge
    # LEAP open interest, so 0.30 x that max set a bar nothing near spot could
    # clear - leaving only the far strike qualifying, which then printed as the
    # tactical wall. Search for tactical walls inside a near-spot band; the far
    # strikes stay available as magnets.
    wb = GEX.get("wall_band", 0.05)
    near_above = [p for p in above if p["strike"] <= spot * (1 + wb)] or above
    near_below = [p for p in below if p["strike"] >= spot * (1 - wb)] or below
    max_coi = max((p["coi"] for p in near_above), default=0)
    max_poi = max((p["poi"] for p in near_below), default=0)
    # "Nearest strike clearing 30% of the local max" breaks when the chain is
    # fine-grained: with 5-point SPX strikes the strike ADJACENT to spot can
    # clear the bar, giving support 7,600 / resistance 7,610 against spot 7,601
    # - true, useless, and not what the briefing means. The briefing states it
    # plainly: "the largest single-strike dealer position above spot". So take
    # the LARGEST OI inside the tactical band, and require a real cluster.
    # THIRD attempt, and the first that matches what the chart shows. Ranking on
    # raw open interest picked 7,900 - a far-OTM round strike holding cheap
    # lottery calls with almost no per-contract gamma, i.e. no hedging pressure
    # at all. Ranking on nearest-qualifying picked the strike adjacent to spot.
    # The quantity dealers actually hedge is DOLLAR GAMMA (OI x per-contract
    # gamma), so rank on that and keep the OI as reporting. This also makes the
    # walls land on the tall bars in the histogram, which is what a reader
    # expects when a line is drawn on a chart.
    min_oi = GEX.get("wall_min_oi", 0.0)
    # The magnet strike must be excluded from wall candidacy, not merely
    # out-ranked. Leaving it in makes thr * max unreachable for every other
    # strike, so it becomes the ONLY qualifier and therefore also the "nearest"
    # one - which is how 8,000 kept taking the call wall.
    _mag = {p["strike"] for p in (call_magnet, put_magnet) if p}
    cand_r = [p for p in near_above if p["coi"] >= min_oi and p["strike"] not in _mag]
    cand_s = [p for p in near_below if p["poi"] >= min_oi and p["strike"] not in _mag]
    if not cand_r:
        cand_r = [p for p in near_above if p["coi"] >= thr * max_coi] or near_above
    if not cand_s:
        cand_s = [p for p in near_below if p["poi"] >= thr * max_poi] or near_below
    # Rank by gamma to find what COUNTS as a wall, then take the NEAREST one to
    # spot - which is the rule as originally specified. Taking the largest hands
    # every wall to whichever round strike (7000, 7500, 8000) sits inside the
    # band: on 2026-08-04 the 8,000 strike was simultaneously call wall, pin and
    # deep magnet, which tells you nothing. The biggest stays available as the
    # magnet; the tactical wall is the first real cluster price meets.
    def _pick(cands, key):
        if not cands:
            return []
        top = max(abs(p.get(key, 0.0)) for p in cands) or 0.0
        qual = [p for p in cands if abs(p.get(key, 0.0)) >= thr * top] or cands
        return sorted(qual, key=lambda p: abs(p["strike"] - spot))   # nearest first

    resistances = _pick(cand_r, "call_gex_B")
    supports = _pick(cand_s, "put_gex_B")
    call_wall = resistances[0] if resistances else (max(above, key=lambda p: p["coi"]) if above else None)
    put_wall = supports[0] if supports else (max(below, key=lambda p: p["poi"]) if below else None)
    # pin is a magnet price gravitates to, so it only means anything near spot
    near_all = [p for p in profile if abs(p["strike"] - spot) <= spot * wb] or profile
    # exclude the deep magnets: a pin that is also the magnet carries no extra
    # information, and the round strike would win every time
    mag_strikes = {p["strike"] for p in (call_magnet, put_magnet) if p}
    pin_pool = [p for p in near_all if p["strike"] not in mag_strikes] or near_all
    pin = max(pin_pool, key=lambda p: p["coi"] + p["poi"]) if pin_pool else None

    return {
        "asof": _now(),
        "source": GEX["source"],
        "overview": {
            "spx_spot": round(spot, 2),
            "net_gex_B": round(net_gex / 1e9 * GEX["calib_net_gex"], 2),
            "regime": "Positive Gamma" if net_gex > 0 else "Negative Gamma",
            "gamma_flip": (round(flip, 2) if flip is not None else None),
            "dist_to_flip_pct": (round((flip / spot - 1) * 100, 2) if flip is not None else None),
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
            "gamma_flip": (round(flip, 2) if flip is not None else None),
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
            "regime_sign": "high",
            "note": "Real SPX chain (CBOE delayed). Open interest matches the coach's "
                    "published figures; the gamma flip METHOD is still under review.",
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

def _cap_weighted_index(series_list, caps_now, bars=60):
    """
    Cap-weighted index with TIME-VARYING weights.

    THE BUG THIS FIXES: universe.csv carries one static market_cap per ticker.
    Weighting 60 weeks of history by today's cap gives a name that doubled over
    the year its post-run weight all the way back through that year, so the
    index systematically overstates whatever has already moved - precisely
    backwards for a rotation tool, whose job is to show what is STARTING to move.

    Shares outstanding are not in the universe file, but they can be derived:
        shares = cap_now / price_now
    and then the cap at any earlier bar is shares * price_then. That assumes the
    share count is roughly stable, which is true within a few percent for most
    names over a year - far smaller than the error it removes.

    Returns the index rebased to 100 at the first bar, or None.
    """
    n = len(series_list)
    if n < 2:
        return None
    shares = []
    for i in range(n):
        px_now = series_list[i][-1]
        if not px_now or px_now <= 0 or not caps_now[i] or caps_now[i] <= 0:
            shares.append(0.0)
        else:
            shares.append(caps_now[i] / px_now)
    if not any(shares):
        return None
    idx = []
    for t in range(bars):
        mcap_t = sum(shares[i] * series_list[i][t] for i in range(n))
        idx.append(mcap_t)
    base = idx[0] or 1.0
    return [round(v / base * 100.0, 3) for v in idx]


def compute_sector_performance(stock_data, universe, daily_ret=None):
    """
    Cap-weighted SECTOR returns, built from the same universe constituents as
    the industry numbers.

    WHY THIS EXISTS: run_sectors() reads the SPDR ETFs, which are GICS. The
    universe uses a different taxonomy (20 sectors, 125 industries). Only one
    name overlaps between them. So a sector line from XLK and an industry line
    from the universe could not be compared - "tech is weak but software is
    strong" was literally unanswerable, because the two lines came from
    different classification systems.

    Building sectors from the universe's own constituents makes the hierarchy
    hold: a sector series IS the aggregate of its industries, and a ticker rolls
    into exactly one of each. The ETFs stay available as a market-standard
    cross-check, not as the primary.
    """
    from collections import defaultdict
    daily_ret = daily_ret or {}
    members = defaultdict(list)
    for tk, info in universe.items():
        sec = (info.get("sector") or "").strip()
        if sec and tk in stock_data and len(stock_data[tk]) >= 60:
            members[sec].append(tk)

    out = []
    for sec, tickers in members.items():
        series, weights, d1s, d1w, inds = [], [], [], [], set()
        for tk in tickers:
            closes = [x[1] for x in stock_data[tk]]
            mc = universe[tk].get("market_cap") or 0
            if len(closes) < 60 or mc <= 0:
                continue
            series.append(closes[-60:]); weights.append(mc)
            ind = (universe[tk].get("industry") or "").strip()
            if ind:
                inds.add(ind)
            if daily_ret.get(tk) is not None:
                d1s.append(daily_ret[tk] * mc); d1w.append(mc)
        if len(series) < 3:
            continue
        W = sum(weights)
        idx_norm = _cap_weighted_index(series, weights)
        if not idx_norm:
            continue
        idx = idx_norm

        def ret(nbars):
            return round((idx[-1] / idx[-1 - nbars] - 1) * 100, 2) if len(idx) > nbars else None

        out.append({
            "sector": sec, "series": idx_norm, "n": len(series),
            "n_industries": len(inds), "mcap_B": round(W / 1e9, 1),
            "d1": (round(sum(d1s) / sum(d1w), 2) if d1w else None),
            "w1": ret(1), "m1": ret(4), "m3": ret(13),
        })
    out.sort(key=lambda r: -(r.get("mcap_B") or 0))
    return out


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
    ind_sector = {}
    for tk, info in universe.items():
        if tk in stock_data and len(stock_data[tk]) >= 60:
            members[info["industry"]].append(tk)
            # every industry belongs to exactly one sector in this universe -
            # verified across all 2,893 names - so carrying the parent through
            # lets the app roll a ticker up to its industry and its sector
            # without a second lookup table
            if info.get("sector"):
                ind_sector.setdefault(info["industry"], info["sector"])

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
        # time-varying cap weights: see _cap_weighted_index. The old line here
        # weighted 60 weeks of history by TODAY's market cap.
        idx_norm = _cap_weighted_index(series, weights)
        if not idx_norm:
            continue
        idx = idx_norm
        def ret(nbars):
            return round((idx[-1] / idx[-1 - nbars] - 1) * 100, 2) if len(idx) > nbars else None
        w1 = ret(1); m1 = ret(4); m3 = ret(13)
        d1 = round(sum(d1s) / sum(d1w), 2) if d1w else None
        ma10 = _sma(idx, 10); ma10_prev = _sma(idx[:-4], 10)
        above = ma10 is not None and idx[-1] > ma10
        rising = ma10 is not None and ma10_prev is not None and ma10 > ma10_prev
        out.append({"industry": ind, "sector": ind_sector.get(ind, ""),
                    "series": idx_norm,
                    "n": len(series), "mcap_B": round(W / 1e9, 1),
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
    ledger = {}   # EVERY evaluated ticker, not just candidates (universe ledger)

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
        # ---- universe ledger row: the gates verdict for THIS name today ----
        # Compact by design (~2,900 rows ship in one file). "miss" carries the
        # by-how-much for the distance-bearing gates so a dropped name's page
        # can say "near_high -28.6% vs -8% band" instead of just "failed".
        _miss = []
        if not gates["trend_long"]:
            _miss.append({"g": "trend200", "by": round((last/ma40-1)*100, 1)})
        if not gates["trend_med"]:
            _miss.append({"g": "trend50",
                          "by": round((last/ma10-1)*100, 1) if last <= ma10 else 0,
                          "note": None if last <= ma10 else "10w not rising"})
        if not gates["industry"]:
            _miss.append({"g": "industry", "by": None})
        if not gates["near_high"]:
            _miss.append({"g": "near_high", "by": round(pos_vs_high, 1)})
        if not gates["stage2"]:
            _miss.append({"g": "stage2", "by": None})
        if not gates["tradability"]:
            _miss.append({"g": "tradability", "by": None})
        ledger[tk] = {
            "st": ("passer" if n_pass == 6 else
                   "near_miss" if n_pass == 5 and not over_ext else "dropped"),
            "gp": n_pass, "miss": _miss,
            "last": round(last, 2), "pvh": round(pos_vs_high, 1),
            "ext": round(ext, 1), "sec": universe[tk].get("sector", ""),
            "ind": ind, "mc": round(mc / 1e9, 2),
            "brk": bool(n_pass == 6 and pos_vs_high > -1.0),
        }
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
            # Shown and logged. NOT a gate - see profitability_flag().
            pf = profitability_flag(quarterly.get(tk) if quarterly else None)
            e["profitability"] = pf["state"]
            e["profitability_why"] = pf.get("why")
            for _k in ("net_margin", "gross_margin", "op_margin", "roe_ttm",
                       "ocf_ttm_B", "fcf_ttm_B", "capex_ttm_B",
                       "capex_pct_of_ocf", "debt_equity", "current_ratio"):
                e[_k] = pf.get(_k)
            # Trailing P/E. Negative earnings give None, not a negative P/E —
            # a "P/E under 20" filter must never quietly admit lossmakers.
            _ni = pf.get("net_income_ttm_B")
            e["pe_ttm"] = (round((mc / 1e9) / _ni, 1)
                           if (_ni and _ni > 0 and mc) else None)
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
    try:
        annotate_persistence(trade_pool)
    except Exception as e:
        print(f"[persist] FAILED (non-fatal): {e}")
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
        "ledger": ledger,
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
# Evaluates open trades in outputs/trades.json against P1–P5 daily and
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
    Check every OPEN trade in outputs/trades.json against the promotion
    spec. Reads/updates the P3 streak state, writes outputs/promotions.json.
    Missing fields on a trade row mark the criterion False with a note —
    unknown never promotes.
    """
    import os, json
    from datetime import date

    pcfg = STOCK_V2["promotion"]
    trades = _load_trades()
    if not trades:
        print("[promo] no trade book (trades.json) — nothing to evaluate")
        return None

    open_trades = _open_trades(trades)

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

# Every gate in detect_regime(), as data. Keeping this next to the rules means
# the explanation can never drift from the logic that produced the call.
REGIME_GATES = {
    "ENERGY_GRIND": [("wti_12m", ">", 25, "WTI 12m"),
                     ("wti_vs_2yr", ">", 0, "WTI vs 2yr"),
                     ("cpi_chg_3m", ">", 0.3, "CPI 3m change")],
    "ENERGY_SPIKE": [("wti_3m", ">", 25, "WTI 3m"),
                     ("vix_3m_chg", ">", 15, "VIX 3m change"),
                     ("cpi_yoy", ">", 3, "CPI y/y"),
                     ("wti_vs_2yr", ">", 0, "WTI vs 2yr")],
    "POLICY_TIGHTENING": [("dd", ">", -12, "Drawdown"),
                          ("us02_3m", ">", 40, "2Y yield 3m change (bp)"),
                          ("real_3m", ">", 25, "Real 10Y 3m change (bp)"),
                          ("cpi_yoy", ">", 2.5, "CPI y/y")],
    "CRISIS_PEAK": [("dd", "<", -15, "Drawdown"), ("vix", ">", 35, "VIX")],
    "RECOVERY_EARLY": [("spx_off_low", ">", 5, "SPX off the low"),
                       ("dd", "<", -12, "Drawdown"),
                       ("vix", ">", 22, "VIX"),
                       ("vix_3m_chg", "<", 0, "VIX 3m change")],
    "RECOVERY_LATE": [("spx_3m", ">", 5, "SPX 3m"),
                      ("trail_dd", ">", -12, "Trailing drawdown"),
                      ("trail_dd", "<", -2, "Trailing drawdown"),
                      ("vix", "<", 22, "VIX"),
                      ("hy", "<", 450, "HY spread (bp)")],
    "GOLDILOCKS": [("vix", "<", 16, "VIX"), ("dd", ">", -5, "Drawdown"),
                   ("spx_3m", ">", 2, "SPX 3m"), ("spx_1m", ">", -2, "SPX 1m"),
                   ("cpi_yoy", "<", 3, "CPI y/y"),
                   ("wti_3m", "<", 20, "WTI 3m"), ("hy", "<", 400, "HY spread (bp)")],
}


def explain_regime(macro_weekly, det, R):
    """
    Why this regime, how long it has held, and what would break it.

    Everything here is derived from REGIME_GATES and a replay of
    detect_regime() over the weekly history - no hand-written narrative, so it
    cannot disagree with the call it is explaining.
    """
    regime = det.get("regime")

    def val(k):
        v = R.get(k)
        return None if v is None else float(v)

    def passes(k, op, thr):
        v = val(k)
        if v is None:
            return None
        return (v > thr) if op == ">" else (v < thr)

    # ---- why: the gates the winning regime had to clear ---------------------
    drivers = []
    for k, op, thr, label in REGIME_GATES.get(regime, []):
        v = val(k)
        if v is None:
            continue
        margin = (v - thr) if op == ">" else (thr - v)
        drivers.append({"key": k, "label": label, "value": round(v, 2),
                        "op": op, "threshold": thr,
                        "clear_by": round(margin, 2), "ok": margin > 0})
    drivers.sort(key=lambda d: abs(d["clear_by"]))

    # ---- how long: replay the engine week by week ---------------------------
    weeks = 0
    since = None
    try:
        n = len(macro_weekly)
        for i in range(n, 13, -1):
            past = detect_regime(compute_regime_inputs(macro_weekly[:i]))
            if past.get("regime") != regime:
                break
            weeks += 1
            since = macro_weekly[i - 1].get("date")
    except Exception as e:
        print(f"[regime] history replay failed: {e}")

    # ---- watch-out: the gate closest to breaking, and where we would land ---
    watch = []
    for d in drivers:
        if not d["ok"]:
            continue
        watch.append({
            "if": f"{d['label']} {'falls below' if d['op']=='>' else 'rises above'} "
                  f"{d['threshold']}",
            "now": d["value"], "threshold": d["threshold"],
            "distance": abs(d["clear_by"]),
            "then": f"{regime} no longer qualifies",
        })
    watch = watch[:3]

    # which regime is nearest to qualifying instead
    scores = det.get("scores") or {}
    others = {k: v for k, v in scores.items() if k != regime}
    runner_up = None
    if others:
        rk = max(others, key=others.get)
        runner_up = {"regime": rk, "score": others[rk],
                     "behind_by": round(scores.get(regime, 0) - others[rk], 1)}
    else:
        # nothing else scored: report the non-qualifying regime with the fewest
        # broken gates, since that is what we would flip to first
        best, best_missing = None, 99
        for rk, gates in REGIME_GATES.items():
            if rk == regime:
                continue
            missing = [g for g in gates if passes(g[0], g[1], g[2]) is False]
            if 0 < len(missing) < best_missing:
                best, best_missing = rk, len(missing)
                near = [{"label": g[3], "need": f"{g[1]} {g[2]}",
                         "now": val(g[0])} for g in missing]
        if best:
            runner_up = {"regime": best, "score": None,
                         "gates_missing": best_missing, "needs": near}

    return {
        "regime": regime,
        "held_weeks": weeks,
        "since": since,
        "drivers": drivers,
        "watch": watch,
        "runner_up": runner_up,
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
        "explain": explain_regime(macro_weekly, det, R),
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
        # macro_series.json is now the DAILY file the brief reads; keep the
        # weekly history under its own name so nothing overwrites the other
        write_json("macro_series_weekly", {
            "asof": result.get("asof"),
            "weeks": len(_trimmed),
            "series": _trimmed,
        })
        print(f"[macro] wrote outputs/macro_series_weekly.json ({len(_trimmed)} weeks)")
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
EARNINGS_CHECK_PER_RUN = 260
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
    # Every ticker in the book — including CLOSED trades. Keeping their charts
    # alive is what lets the post-exit tracker follow a name after the exit
    # instead of silently dropping the trade.
    for row in _load_trades():
        if row.get("ticker"):
            pinned.add(row["ticker"])
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
    _diag = [0, 0]   # capped diagnostics: (empty-statement, exception)
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
                # yfinance moved the canonical accessor to quarterly_income_stmt;
                # quarterly_financials still exists but can come back empty. Try
                # both, newest name first, and say so when neither yields rows.
                qf = None
                for _acc in ("quarterly_income_stmt", "quarterly_financials",
                             "quarterly_incomestmt"):
                    try:
                        _df = getattr(t, _acc, None)
                        if _df is not None and getattr(_df, "shape", (0, 0))[1] > 0:
                            qf = _df
                            break
                    except Exception:
                        continue
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
                if qf is None:
                    if _diag[0] < 3:
                        _diag[0] += 1
                        print(f"[earnings]   {tk}: no quarterly income statement "
                              f"from yfinance (all accessors empty)")
                if qf is not None and qf.shape[1] > 0:
                    for col in list(qf.columns):
                        qend = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)[:10]
                        if qend in have.get(tk, set()):
                            continue   # already have this quarter

                        def g(df, *keys):
                            if df is None:
                                return ""
                            for key in keys:
                                try:
                                    v = df.loc[key, col]
                                except Exception:
                                    continue
                                if v is None or _isnan(v):
                                    continue
                                try:
                                    return float(v)
                                except Exception:
                                    continue
                            return ""

                        rev = g(qf, "Total Revenue", "TotalRevenue",
                                "Total Revenues", "Operating Revenue",
                                "OperatingRevenue")
                        if rev == "":
                            if _diag[0] < 3:
                                _diag[0] += 1
                                try:
                                    _lbl = list(qf.index)[:12]
                                except Exception:
                                    _lbl = "?"
                                print(f"[earnings]   {tk} {qend}: no revenue row. "
                                      f"labels seen: {_lbl}")
                            continue   # no revenue -> not a real quarter row
                        row = {
                            "ticker": tk, "quarter_end": qend,
                            "revenue": rev,
                            "gross_profit": g(qf, "Gross Profit", "GrossProfit"),
                            "operating_income": g(qf, "Operating Income", "OperatingIncome", "EBIT"),
                            "net_income": g(qf, "Net Income", "NetIncome", "Net Income Common Stockholders", "Net Income From Continuing Operation Net Minority Interest"),
                            "ebitda": g(qf, "EBITDA", "Normalized EBITDA"),
                            "cost_of_revenue": g(qf, "Cost Of Revenue", "CostOfRevenue", "Reconciled Cost Of Revenue"),
                            "operating_cash_flow": g(cf, "Operating Cash Flow", "OperatingCashFlow", "Cash Flow From Continuing Operating Activities"),
                            "free_cash_flow": g(cf, "Free Cash Flow", "FreeCashFlow"),
                            "capex": g(cf, "Capital Expenditure", "CapitalExpenditure"),
                            "total_debt": g(bs, "Total Debt", "TotalDebt"),
                            "total_equity": g(bs, "Stockholders Equity", "StockholdersEquity", "Total Equity Gross Minority Interest"),
                            "total_assets": g(bs, "Total Assets", "TotalAssets"),
                            "cash": g(bs, "Cash And Cash Equivalents", "CashAndCashEquivalents", "Cash Cash Equivalents And Short Term Investments"),
                            "current_assets": g(bs, "Current Assets", "CurrentAssets", "Total Current Assets"),
                            "current_liabilities": g(bs, "Current Liabilities", "CurrentLiabilities", "Total Current Liabilities"),
                        }
                        new_rows.append(row)
                        changed.add(tk)
                        print(f"[earnings]   NEW: {tk} {qend} rev ${rev/1e9:.2f}B")
            except Exception as _e:
                fail += 1
                if _diag[1] < 5:
                    _diag[1] += 1
                    print(f"[earnings]   {tk}: statement fetch failed: {_e}")

            state.setdefault("last_checked", {})[tk] = today_s
            if idx % 25 == 0:
                print(f"[earnings]   {idx}/{len(batch)} checked, {len(changed)} with new data")
            time.sleep(0.6)
        except Exception as _e:
            fail += 1
            if _diag[1] < 5:
                _diag[1] += 1
                print(f"[earnings]   {tk}: ticker failed: {_e}")
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
            json.dump(_json_safe(payload), f, separators=(",", ":"), allow_nan=False)
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
            json.dump(_json_safe(payload), f, separators=(",", ":"), allow_nan=False)
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


def run_macro_series_daily():
    """
    outputs/macro_series.json at DAILY cadence.

    The brief compares "since yesterday" and "since last Friday's close", but
    macro_series was built from macro_weekly - rows dated every Monday. Both
    comparisons therefore read the same two weekly rows, which is why the daily
    and weekly blocks printed identical numbers and why "last week's close"
    resolved to a Monday (Jul 27) instead of a Friday (Jul 31).

    The regime engine still runs weekly. Only the SERIES the brief reads is
    daily. Yield and credit series stay weekly from FRED and are forward-filled.
    """
    import os, json
    try:
        import yfinance as yf
    except Exception as e:
        print(f"[macroday] yfinance unavailable ({e}) - keeping previous series")
        return None

    SYMS = {"spx": "^GSPC", "ndx": "^IXIC", "dow": "^DJI", "rut": "^RUT",
            "vix": "^VIX", "wti": "CL=F", "gold": "GC=F", "dxy": "DX-Y.NYB",
            "tnx": "^TNX", "btc": "BTC-USD"}   # the asset board plots btc too
    period = os.environ.get("MACRO_DAILY_PERIOD", "1y")
    cols = {}
    for key, sym in SYMS.items():
        try:
            df = yf.download(sym, period=period, interval="1d",
                             auto_adjust=True, progress=False)
            if df is None or len(df) == 0:
                continue
            close = df["Close"]
            if hasattr(close, "columns"):
                close = close.iloc[:, 0]
            cols[key] = {str(d)[:10]: float(v) for d, v in close.dropna().items()}
        except Exception as e:
            print(f"[macroday] {sym}: {e}")

    if "spx" not in cols or len(cols["spx"]) < 30:
        print("[macroday] no usable SPX history - keeping previous series")
        return None

    # carry the weekly-only fields forward onto each trading day
    weekly = {}
    for fname in ("macro_series_weekly.json", "macro_series.json"):
        try:
            prev = json.load(open(os.path.join(OUTPUTS_DIR, fname)))
        except Exception:
            continue
        rows = prev if isinstance(prev, list) else (prev.get("series") or [])
        # skip a file that is already the daily one we are about to replace
        if (prev if isinstance(prev, dict) else {}).get("cadence") == "daily":
            continue
        for r in rows:
            if isinstance(r, dict) and r.get("date"):
                weekly[r["date"][:10]] = r
        if weekly:
            break
    carry_keys = ("us02", "real10", "hy", "cpi_yoy")

    dates = sorted(cols["spx"])
    wkeys = sorted(weekly)
    out, carry = [], {}
    for d in dates:
        row = {"date": d}
        for k in SYMS:
            v = cols.get(k, {}).get(d)
            if v is not None:
                row[k] = round(v, 4)
        if "tnx" in row:
            row["us10"] = round(row["tnx"] / 10.0, 3)
        # weekly rows are dated Mondays; a trading day rarely matches exactly,
        # so take the most recent weekly row on or before this date
        for wd in wkeys:
            if wd > d:
                break
            w = weekly[wd]
            for k in carry_keys:
                if w.get(k) is not None:
                    carry[k] = w[k]
        for k, v in carry.items():
            row.setdefault(k, v)
        out.append(row)

    out = out[-260:]
    write_json("macro_series", {"asof": _now(), "days": len(out),
                                "cadence": "daily", "series": out})
    print(f"[macroday] {len(out)} DAILY rows, {out[0]['date']} -> {out[-1]['date']}")
    return len(out)


# ============================================================
# MACRO DAILY OHLC — one chart, six instruments
#
# spx_daily.json stays exactly as it is (the Edition and the gamma overlay
# read it). This writes the SAME shape for the other macro instruments so the
# Markets chart can switch between them without changing its renderer.
#
# Candles need OHLC. Five instruments have it from Yahoo. The credit spread
# does NOT — FRED publishes one number per day — so it is written as a LINE
# and labelled as one rather than faked into four identical values.
# ============================================================
MACRO_DAILY_INSTRUMENTS = [
    # key      yahoo/fred     label                    kind
    ("SPX",   "^GSPC",  "S&P 500",                  "candles"),
    ("NDX",   "^IXIC",  "Nasdaq Composite",         "candles"),
    ("GOLD",  "GC=F",   "Gold (front future)",      "candles"),
    ("OIL",   "CL=F",   "WTI crude (front future)", "candles"),
    ("TLT",   "TLT",    "Bonds · 20yr Treasury",    "candles"),
    ("HYOAS", "BAMLH0A0HYM2", "Credit spread · HY OAS", "line"),
]


def run_macro_daily(period="1y"):
    """Publish outputs/macro_daily.json: daily bars for every macro instrument."""
    import os as _os
    try:
        import yfinance as yf
    except Exception as e:
        print(f"[macrod] yfinance unavailable: {e} — keeping previous file")
        return None

    instruments, problems = {}, []
    for key, sym, label, kind in MACRO_DAILY_INSTRUMENTS:
        if kind == "line":
            continue
        try:
            df = yf.download(sym, period=period, interval="1d",
                             auto_adjust=False, progress=False)
            if df is None or len(df) == 0:
                problems.append(f"{key}: empty pull")
                continue

            def col(name):
                c = df[name]
                if hasattr(c, "columns"):
                    c = c.iloc[:, 0]
                return c

            o, h, l, cl, v = (col("Open"), col("High"), col("Low"),
                              col("Close"), col("Volume"))
            bars = []
            for d in df.index:
                def g(series, nd=2):
                    try:
                        return round(float(series.loc[d]), nd)
                    except Exception:
                        return None
                cv = g(cl)
                if cv is None:
                    continue
                # a rate-limited pull returns a row with NaN close; keeping it
                # produced a hole in the chart AND invalid JSON downstream
                import math as _m
                if isinstance(cv, float) and (_m.isnan(cv) or _m.isinf(cv)):
                    continue
                try:
                    ds = d.strftime("%Y-%m-%d")
                except AttributeError:
                    ds = str(d)[:10]
                try:
                    vv = int(float(v.loc[d]))
                except Exception:
                    vv = None
                bars.append({"date": ds, "o": g(o), "h": g(h), "l": g(l),
                             "c": cv, "v": vv})
            if len(bars) < 30:
                problems.append(f"{key}: only {len(bars)} bars")
                continue
            instruments[key] = {"label": label, "kind": "candles",
                                "symbol": sym, "bars": bars,
                                "asof": bars[-1]["date"]}
            print(f"[macrod] {key:6} {len(bars):4} bars  last {bars[-1]['date']} "
                  f"{bars[-1]['c']}")
        except Exception as e:
            problems.append(f"{key}: {e}")

    # ---- credit spread: read the HY OAS the daily macro series already carries.
    # fred() lives INSIDE run_macro(), so it cannot be called from here, and
    # re-fetching the same series twice per run would be waste. macro_series
    # forward-fills it from FRED's weekly release — which is exactly why it is
    # drawn as a LINE and never as candles.
    try:
        import json as _json
        rows = []
        try:
            ms = _json.load(open(_os.path.join(OUTPUTS_DIR, "macro_series.json")))
            for r in (ms.get("series") or []):
                v = r.get("hy")
                if v is None or not r.get("date"):
                    continue
                rows.append({"date": str(r["date"])[:10], "c": round(float(v), 2)})
        except FileNotFoundError:
            problems.append("HYOAS: macro_series.json not written yet")
        rows = rows[-400:]
        if len(rows) >= 30:
            instruments["HYOAS"] = {
                "label": "Credit spread · HY OAS", "kind": "line",
                "symbol": "BAMLH0A0HYM2", "bars": rows,
                "asof": rows[-1]["date"],
                "note": "FRED HY OAS, forward-filled from the weekly release — "
                        "drawn as a line, not candles."}
            print(f"[macrod] HYOAS  {len(rows):4} points last {rows[-1]['date']} "
                  f"{rows[-1]['c']}%")
        else:
            problems.append(f"HYOAS: only {len(rows)} points")
    except Exception as e:
        problems.append(f"HYOAS: {e}")

    if not instruments:
        print("[macrod] nothing fetched — keeping previous macro_daily.json")
        return None
    for p in problems:
        print(f"[macrod] MISSING {p}")

    def _validate_macro_daily(res):
        probs = []
        n = len((res or {}).get("instruments") or {})
        if n < 3:
            probs.append(f"only {n} instruments fetched (<3) — pull likely broken")
        return probs

    write_json_guarded("macro_daily", {
        "asof": _now(), "count": len(instruments),
        "order": [k for k, _s, _l, _t in MACRO_DAILY_INSTRUMENTS
                  if k in instruments],
        "missing": problems,
        "instruments": instruments}, _validate_macro_daily)
    return len(instruments)


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
                    val = float(series.loc[d])
                    import math as _m
                    if _m.isnan(val) or _m.isinf(val):
                        return None          # rate-limited / partial bar
                    return round(val, 2)
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
# GEX DATA FETCH — CBOE delayed SPX chain (briefing PDF takes precedence)
# Kept SEPARATE from gex_engine so we can change the source without touching the math.
# ============================================================
# SPY x10 proxy REMOVED 7 Aug 2026. fetch_gex_chain_yfinance() and run_gex()
# lived here. They were deleted rather than deprecated because a dead code path
# that still runs on failure is how the wrong number reaches the screen.
# Sources are now: committed briefing PDF, then CBOE. Nothing else.




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
        # the repo has shipped this file under a few names over time; try them
        # all before giving up, and never fail silently.
        for alt in ("universe.csv", "universe_2.csv", "macroflow_universe.csv",
                    "outputs/universe.csv"):
            if os.path.exists(alt):
                path = alt
                break
        else:
            print(f"[universe] WARNING: no universe CSV found (looked for "
                  f"universe.csv, universe_2.csv, macroflow_universe.csv). "
                  f"Ticker filters that depend on it will be DISABLED.")
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

def load_daily_from_csv(path="stock_daily.csv"):
    """
    Load {ticker: [(date, close), ...]} from the committed daily CSV.

    Searches the plain file, the gzipped twin, and a couple of places the file
    commonly lands by accident. When nothing is found it LISTS what is actually
    in the working directory — a silent {} here is what made the chart fall
    back to the 2-day log while the CSV sat somewhere unexpected.
    """
    import csv, os, gzip, io
    from collections import defaultdict
    # Name-agnostic discovery. iOS Files rewrites extensions on rename, so the
    # same data legitimately arrives as stock_daily.csv, stock_daily.csv.gz,
    # stock_daily.gz.csv, stock_daily.gz, or "stock_daily (1).csv". Content is
    # sniffed below, so the NAME only has to get us to the right file.
    import glob as _glob
    stem = os.path.basename(path).rsplit(".", 1)[0]        # "stock_daily"
    cands = []
    for d in ("", "data", OUTPUTS_DIR):
        for pat in (stem + ".*", stem + "*.csv", stem + "*.gz"):
            cands += _glob.glob(os.path.join(d, pat) if d else pat)
    # exact names first, then anything else; de-duplicate, keep order
    pref = [path + ".gz", path, path + ".gz.csv", stem + ".gz"]
    ordered, seen_p = [], set()
    for c in pref + sorted(cands):
        if c not in seen_p and os.path.exists(c) and os.path.isfile(c):
            seen_p.add(c)
            ordered.append(c)
    found = ordered[0] if ordered else None
    if len(ordered) > 1:
        print(f"[daily] {len(ordered)} candidate files found {ordered} — "
              f"using {found}")
    if not found:
        print(f"[daily] {path}[.gz] NOT FOUND. cwd={os.getcwd()}")
        try:
            here = sorted(os.listdir("."))
            csvs = [f for f in here if f.lower().endswith((".csv", ".csv.gz"))]
            print(f"[daily] CSV-ish files in the repo root: {csvs or 'NONE'}")
            near = [f for f in here if "daily" in f.lower() or "stock" in f.lower()]
            if near:
                print(f"[daily] files matching stock/daily: {near}")
        except Exception as e:
            print(f"[daily] could not list cwd: {e}")
        return {}
    # Sniff the MAGIC BYTES, do not trust the extension. iPad downloads and
    # GitHub uploads routinely drop the .gz, leaving gzip data in a file called
    # stock_daily.csv — which then parses as binary garbage and yields nothing.
    # 1f 8b is gzip; anything else is read as text.
    with open(found, "rb") as _probe:
        magic = _probe.read(2)
    is_gz = (magic == b"\x1f\x8b")
    if is_gz:
        fh = io.TextIOWrapper(gzip.open(found, "rb"), encoding="utf-8")
    else:
        fh = open(found, encoding="utf-8", errors="replace")
    label = "gzipped" if is_gz else "plain"
    if is_gz and not found.endswith(".gz"):
        print(f"[daily] NOTE: {found} is gzip data with a .csv name — reading it "
              f"as gzip anyway (rename it to {found}.gz when convenient)")
    print(f"[daily] reading {found} ({os.path.getsize(found)/1e6:.1f} MB, {label})")
    data = defaultdict(list)
    bad_rows = 0
    with fh as f:
        rd = csv.DictReader(f)
        cols = [c.strip().lower() for c in (rd.fieldnames or [])]
        if not {"ticker", "date", "close"} <= set(cols):
            print(f"[daily] WRONG COLUMNS: got {rd.fieldnames}, need ticker,date,close")
            return {}
        for row in rd:
            try:
                data[row["ticker"]].append((row["date"], float(row["close"])))
            except Exception:
                bad_rows += 1
                continue
    if bad_rows:
        print(f"[daily] {bad_rows} unparseable rows skipped")
    for tk in data:
        data[tk].sort()
    return dict(data)

def load_shares_outstanding(path="shares_outstanding.csv"):
    """
    {ticker: shares} from the committed file (tools/build_stock_daily.ipynb
    emits it alongside stock_daily.csv). Optional — see refresh_universe_caps
    for what happens without it.
    """
    import csv, os
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                sh = float(row["shares"])
                if sh > 0:
                    out[row["ticker"]] = sh
            except Exception:
                continue
    return out


def refresh_universe_caps(universe, closes, shares=None):
    """
    Recompute market cap from the LATEST committed close, every run. PURE.

    universe.csv ships a market cap with no date on it. Left alone it goes
    stale between exports, and every cap-weighted number downstream — sector
    and industry indices, the tradability gate — quietly drifts with it.

    With shares_outstanding.csv:  mcap = shares x latest close. Exact.
    Without it:                   shares are IMPLIED as mcap / latest close,
                                  which leaves today's cap unchanged but still
                                  gives every past session a real weight (see
                                  compute_rotation_series_daily). Labelled
                                  'implied' so nobody mistakes it for measured.

    Returns (new_universe, report).
    """
    shares = shares or {}
    out, rep = {}, {"source": "shares_outstanding.csv" if shares else "implied",
                    "measured": 0, "implied": 0, "unchanged": 0,
                    "rejected": 0, "rejected_names": [],
                    "inconsistent": 0, "inconsistent_names": [],
                    "moved_5pct": 0, "biggest": []}
    moves = []
    for tk, info in universe.items():
        rows = closes.get(tk) or []
        last = rows[-1][1] if rows else None
        old = float(info.get("market_cap") or 0)
        new_info = dict(info)
        if last and last > 0:
            sh = shares.get(tk)
            if sh:
                new = sh * last
                pct = ((new / old - 1) * 100) if old > 0 else None
                # PLAUSIBILITY TEST (primary). If the universe cap is merely
                # stale, then cap/shares must equal this ticker's price on SOME
                # day in the window. When it lands outside the whole year's
                # range, the cap and the share count are describing different
                # things — ADR ratios (ordinary shares vs ADSs), preferreds and
                # closed-end funds are the usual culprits. Measured on the real
                # 16 Aug file: 2,437 of 2,514 pass, 77 fail. Those 77 would
                # otherwise enter an industry index at a badly wrong weight.
                bad_shares = False
                if old > 0 and rows:
                    px = [c for _d, c in rows if c and c > 0]
                    if px:
                        need = old / sh
                        if not (min(px) * 0.97 <= need <= max(px) * 1.03):
                            bad_shares = True
                if bad_shares:
                    new_info["cap_source"] = "shares_inconsistent"
                    new_info["shares"] = old / last   # implied: consistent by construction
                    rep["inconsistent"] += 1
                    rep["inconsistent_names"].append(
                        {"ticker": tk, "implied_px": round(old / sh, 2),
                         "range": [round(min(px), 2), round(max(px), 2)]})
                # A cap also cannot credibly move >300% between exports.
                elif pct is not None and abs(pct) > 300:
                    new_info["cap_source"] = "rejected_outlier"
                    new_info["shares"] = old / last
                    rep["rejected"] += 1
                    rep["rejected_names"].append({"ticker": tk,
                                                  "pct": round(pct, 1)})
                else:
                    new_info["market_cap"] = new
                    new_info["shares"] = sh
                    new_info["cap_source"] = "measured"
                    rep["measured"] += 1
                    if pct is not None:
                        moves.append((tk, pct))
            elif old > 0:
                new_info["shares"] = old / last     # implied, constant forward
                new_info["cap_source"] = "implied"
                rep["implied"] += 1
            else:
                new_info["cap_source"] = "unknown"
                rep["unchanged"] += 1
        else:
            new_info["cap_source"] = "no_price"
            rep["unchanged"] += 1
        out[tk] = new_info
    moves.sort(key=lambda x: -abs(x[1]))
    rep["moved_5pct"] = sum(1 for _t, m in moves if abs(m) >= 5)
    rep["biggest"] = [{"ticker": t, "pct": round(m, 1)} for t, m in moves[:8]]
    return out, rep


def compute_rotation_series_daily(daily_data, universe, min_members=3):
    """
    Cap-weighted DAILY index per sector and per industry, rebased to 100.

    PURE. Weights DRIFT: each session is weighted by that session's market cap
    (shares x that day's close), not by today's. Static current weights would
    apply a winner's post-run size to its own pre-run returns — the index would
    show a past it never had. Shares come from universe['shares'] (measured or
    implied by refresh_universe_caps) and are held constant across the window;
    a year of buybacks is second-order next to the price move.

    A name counts on a session only if it has both a prior and a current close,
    so a listing that starts mid-window cannot fake a jump.

    Returns (dates, {"sectors": [...], "industries": [...]}).
    """
    from collections import defaultdict

    all_dates = set()
    for _tk, rows in daily_data.items():
        for d, _c in rows:
            all_dates.add(d)
    dates = sorted(all_dates)
    if len(dates) < 2:
        return [], {"sectors": [], "industries": []}

    closes = {tk: dict(rows) for tk, rows in daily_data.items()}
    groups = {"sectors": defaultdict(list), "industries": defaultdict(list)}
    for tk, info in universe.items():
        if tk not in closes or len(closes[tk]) < 2:
            continue
        sh = float(info.get("shares") or 0)
        if sh <= 0:
            mc = float(info.get("market_cap") or 0)
            last = closes[tk].get(dates[-1])
            sh = (mc / last) if (mc > 0 and last) else 0.0
        if sh <= 0:
            continue
        if info.get("sector"):
            groups["sectors"][info["sector"]].append((tk, sh))
        if info.get("industry"):
            groups["industries"][info["industry"]].append((tk, sh))

    out = {"sectors": [], "industries": []}
    for bucket, members in groups.items():
        rows = []
        for name, mems in members.items():
            if len(mems) < min_members:
                continue
            lvl, series = 100.0, [100.0]
            for i in range(1, len(dates)):
                d0, d1 = dates[i - 1], dates[i]
                num = wsum = 0.0
                for tk, sh in mems:
                    c0 = closes[tk].get(d0)
                    c1 = closes[tk].get(d1)
                    if not c0 or not c1 or c0 <= 0:
                        continue
                    w = sh * c0          # cap at the START of the session
                    num += (c1 / c0 - 1.0) * w
                    wsum += w
                lvl *= (1 + (num / wsum if wsum else 0.0))
                series.append(round(lvl, 3))
            last_caps = sum(sh * (closes[tk].get(dates[-1]) or 0)
                            for tk, sh in mems)
            rows.append({"name": name, "series": series, "n": len(mems),
                         "mcap_B": round(last_caps / 1e9, 1),
                         "chg": round(series[-1] - 100.0, 2)})
        rows.sort(key=lambda x: -x["chg"])
        out[bucket] = rows
    return dates, out

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


SECTOR_ETFS = {
    "XLK": "Technology", "XLF": "Financials", "XLV": "Health Care",
    "XLY": "Consumer Discretionary", "XLP": "Consumer Staples",
    "XLE": "Energy", "XLI": "Industrials", "XLB": "Materials",
    "XLU": "Utilities", "XLRE": "Real Estate", "XLC": "Communication Svcs",
}


# FOMC decision dates. Two-day meetings; the date below is the DECISION day.
FOMC_2026 = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
             "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"]
# BLS CPI release dates, 08:30 ET. Published a year ahead; correct these against
# bls.gov/schedule/news_release/cpi.htm rather than trusting an estimate.
CPI_2026 = ["2026-01-13", "2026-02-11", "2026-03-11", "2026-04-10",
            "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
            "2026-09-11", "2026-10-13", "2026-11-10", "2026-12-10"]


def _third_friday(year, month):
    from datetime import date, timedelta
    d = date(year, month, 1)
    d += timedelta(days=(4 - d.weekday()) % 7)      # first Friday
    return d + timedelta(days=14)                    # third Friday


# ============================================================
# ROTATION, DAILY — the log that turns a weekly picture into a daily one
#
# Sector and industry SERIES are cap-weighted from the universe's weekly bars,
# because daily bars for ~2,900 names is not a thing this engine can fetch.
# But the 1-DAY cap-weighted move per sector and per industry IS computed every
# run (d1, from the daily-return pass).
#
# So: append d1 every run, append-only, one row per date, first run of a date
# wins. From those rows a genuine DAILY series is compounded — same doctrine as
# the signal log. Day one shows one point and says so; by month two the chart
# answers "what changed this week" honestly instead of interpolating a guess.
# ============================================================
# ============================================================
# ROTATION NAVIGATOR — the cross-linked dataset
#
# One file that lets the app answer all three directions without another
# fetch: top-down (sector -> its industries -> their names), bottom-up
# (ticker -> its industry and sector), and search (any of the three).
#
# Everything here is derived from files the engine already produced —
# rotation_daily.json for the series, universe.csv for membership,
# stock_daily.csv for per-ticker history. No new network.
#
# Sparklines are resampled ANCHORED ON THE LATEST SESSION. Plain [::step]
# sampling can end on a stale bar, and since the last point decides whether a
# line draws above or below its 50-day average, that silently mis-colours rows.
# ============================================================
NAV_SPARK_POINTS = 40


def _nav_anchored(seq, n=NAV_SPARK_POINTS):
    """Downsample to n points, always keeping the newest. PURE."""
    if not seq:
        return []
    if len(seq) <= n:
        return [round(float(x), 1) for x in seq]
    step = max(1, len(seq) // n)
    out = [seq[i] for i in range(len(seq) - 1, -1, -step)][:n]
    # 1 decimal: these drive a 25px sparkline, and the extra digit across
    # ~2,800 tickers costs about a megabyte of payload for no visible gain
    return [round(float(x), 1) for x in out[::-1]]


def _nav_ma(seq, k=50):
    """Trailing mean. PURE."""
    return [sum(seq[max(0, i - k + 1):i + 1]) / len(seq[max(0, i - k + 1):i + 1])
            for i in range(len(seq))]


def _nav_ret(seq, k):
    return round((seq[-1] / seq[-1 - k] - 1) * 100, 2) if len(seq) > k else 0.0


def run_rotation_nav():
    """Publish outputs/rotation_nav.json — search + drill-down in one payload."""
    import json as _json, os as _os
    from collections import defaultdict

    try:
        rot = _json.load(open(_os.path.join(OUTPUTS_DIR, "rotation_daily.json")))
    except Exception as e:
        print(f"[nav] rotation_daily.json unavailable ({e}) — skipped")
        return
    dates = rot.get("dates") or []
    if len(dates) < 30:
        print(f"[nav] only {len(dates)} sessions — too little to navigate, skipped")
        return

    uni = load_universe_from_csv()
    daily = load_daily_from_csv()
    if not daily:
        print("[nav] no stock_daily.csv — ticker level would be empty, skipped")
        return

    # ---- benchmark, for the RS ranks -------------------------------------
    spx = []
    try:
        sd = _json.load(open(_os.path.join(OUTPUTS_DIR, "spx_daily.json")))
        by = {str(b["date"])[:10]: float(b["c"])
              for b in (sd.get("bars") or []) if b.get("date") and b.get("c")}
        lvl = None
        for d in dates:
            lvl = by.get(d, lvl)
            spx.append(lvl)
        base = next((x for x in spx if x), None)
        spx = [(x / base * 100 if x and base else 100.0) for x in spx]
    except Exception:
        spx = [100.0] * len(dates)

    def pack_group(rows):
        out = {}
        for r in rows:
            ser = r.get("series") or []
            if len(ser) < 30:
                continue
            out[r["name"]] = {
                "n": r.get("n", 0),
                "r1": _nav_ret(ser, 1), "r5": _nav_ret(ser, 5),
                "r21": _nav_ret(ser, 21), "r63": _nav_ret(ser, 63),
                "r252": round(ser[-1] - 100.0, 2),
                "sp": _nav_anchored(ser), "mp": _nav_anchored(_nav_ma(ser)),
            }
        return out

    sectors = pack_group(rot.get("sectors") or [])
    industries = pack_group(rot.get("industries") or [])

    def rank_rs(group):
        base = _nav_ret(spx, 63)
        rel = sorted(((k, v["r63"] - base) for k, v in group.items()),
                     key=lambda x: x[1])
        n = len(rel)
        for i, (k, _v) in enumerate(rel):
            group[k]["rs"] = round((i / max(1, n - 1)) * 100 - 50)
    rank_rs(sectors)
    rank_rs(industries)

    # ---- sector -> industries, straight from the universe taxonomy -------
    s2i = defaultdict(set)
    for tk, info in uni.items():
        s, i = info.get("sector"), info.get("industry")
        if s and i:
            s2i[s].add(i)
    s2i = {k: sorted(v) for k, v in sorted(s2i.items())}

    # ---- tickers ---------------------------------------------------------
    tickers, by_ind = {}, defaultdict(list)
    for tk, info in uni.items():
        rows = daily.get(tk)
        if not rows:
            continue
        cl = dict(rows)
        ser, last = [], None
        for d in dates:
            v = cl.get(d)
            last = v if v else last
            ser.append(last)
        ser = [x for x in ser if x]
        if len(ser) < 70:
            continue
        b = ser[0]
        idx = [x / b * 100 for x in ser]
        tickers[tk] = {
            "s": info.get("sector", ""), "i": info.get("industry", ""),
            "p": round(ser[-1], 2),
            "mc": round(float(info.get("market_cap") or 0) / 1e9, 1),
            "r1": _nav_ret(idx, 1), "r5": _nav_ret(idx, 5),
            "r21": _nav_ret(idx, 21), "r63": _nav_ret(idx, 63),
            "r252": round(idx[-1] - 100.0, 2),
            "sp": _nav_anchored(idx), "mp": _nav_anchored(_nav_ma(idx)),
        }
        if info.get("industry"):
            by_ind[info["industry"]].append(tk)

    # RS per ticker is ranked WITHIN its own industry — a semiconductor is
    # judged against semiconductors, not against utilities.
    for ind, tks in by_ind.items():
        tks.sort(key=lambda t: tickers[t]["r63"])
        n = len(tks)
        for i, t in enumerate(tks):
            tickers[t]["rs"] = round((i / max(1, n - 1)) * 100 - 50)
    for t in tickers:
        tickers[t].setdefault("rs", 0)

    payload = {
        "asof": _now(), "through": dates[-1], "sessions": len(dates),
        "note": ("Search and drill-down in one payload. RS for a sector or "
                 "industry is its 3-month return ranked against the S&P across "
                 "the whole group; RS for a ticker is ranked within its own "
                 "industry."),
        "sectors": sectors, "industries": industries,
        "s2i": s2i, "tickers": tickers,
    }

    def _validate_nav(pl):
        probs = []
        if len(pl.get("tickers") or {}) < 200:
            probs.append(f"only {len(pl.get('tickers') or {})} tickers (<200)")
        if len(pl.get("industries") or {}) < 20:
            probs.append(f"only {len(pl.get('industries') or {})} industries (<20)")
        return probs

    write_json_guarded("rotation_nav", payload, _validate_nav)
    # rewrite without whitespace: this file is fetched by a phone
    try:
        _p = _os.path.join(OUTPUTS_DIR, "rotation_nav.json")
        _d = _json.load(open(_p))
        _json.dump(_json_safe(_d), open(_p, "w"), separators=(",", ":"),
                   allow_nan=False)
        print(f"[nav] compacted to {_os.path.getsize(_p)/1e6:.2f} MB")
    except Exception as e:
        print(f"[nav] compaction skipped: {e}")
    print(f"[nav] rotation_nav.json: {len(sectors)} sectors, "
          f"{len(industries)} industries, {len(tickers)} tickers, "
          f"through {dates[-1]}")


def run_rotation_daily():
    """Append today's d1 per sector/industry; rebuild the daily rotation series."""
    import json as _json, os as _os

    hist_dir = _os.path.join(OUTPUTS_DIR, "history")
    _os.makedirs(hist_dir, exist_ok=True)
    log_path = _os.path.join(hist_dir, "rotation_daily.jsonl")

    def _read(name, key, label_field):
        try:
            d = _json.load(open(_os.path.join(OUTPUTS_DIR, name)))
        except Exception as e:
            print(f"[rotd] {name} unavailable: {e}")
            return {}, None
        rows, asof = d.get(key) or [], d.get("asof")
        out = {}
        for r in rows:
            nm, d1 = r.get(label_field), r.get("d1")
            if nm and d1 is not None:
                out[nm] = round(float(d1), 3)
        return out, asof

    sec, sec_asof = _read("sector_perf.json", "sectors", "sector")
    ind, ind_asof = _read("industry.json", "industries", "industry")
    if not sec and not ind:
        # NOT fatal: d1 only feeds the fallback log. The committed daily CSV is
        # a separate source and must still be read.
        print("[rotd] no d1 values in sector_perf/industry.json — skipping the "
              "log append (the stock_daily.csv path below is unaffected)")

    today = _now()[:10]
    seen = {}
    if _os.path.exists(log_path):
        for line in open(log_path):
            line = line.strip()
            if not line:
                continue
            try:
                r = _json.loads(line)
                seen[r["date"]] = r
            except Exception:
                continue
    # The engine can run twice on one trading day, or on a weekend, and both
    # runs carry the SAME d1 (the last close has not changed). Appending both
    # compounds one day's move twice — visible on 16-17 Aug as Broadcasting
    # +4.53% logged on Sunday and again on Monday. Skip an append whose values
    # are identical to the newest logged row.
    import datetime as _dt
    def _is_session(d):
        try:
            y, m, dd = map(int, str(d)[:10].split("-"))
            return _dt.date(y, m, dd).weekday() < 5      # Mon-Fri only
        except Exception:
            return True
    _prev = seen[max(seen)] if seen else None
    _same = bool(_prev and _prev.get("sectors") == sec
                 and _prev.get("industries") == ind)
    if not (sec or ind):
        pass                                   # nothing to append today
    elif not _is_session(today):
        print(f"[rotd] {today} is not a trading day — not appending "
              f"(a weekend run carries Friday's close)")
    elif _same:
        print(f"[rotd] d1 values identical to {max(seen)} — the underlying close "
              f"has not changed, not appending (would double-count)")
    elif today in seen:
        print(f"[rotd] {today} already logged — first run of a date wins, "
              f"not overwriting")
    else:
        row = {"date": today, "sectors": sec, "industries": ind,
               "src_asof": {"sector": sec_asof, "industry": ind_asof}}
        with open(log_path, "a") as f:
            f.write(_json.dumps(row, separators=(",", ":")) + "\n")
        seen[today] = row
        print(f"[rotd] appended {today}: {len(sec)} sectors, {len(ind)} industries")

    # ---- PRIMARY: true daily series from the committed daily CSV ----------
    # The log below is the fallback that works from day one; this is the real
    # thing — one row per ticker per session, so 3M actually means 63 sessions
    # of history the first time it runs rather than 63 days from now.
    try:
        dd = load_daily_from_csv()
    except Exception as e:
        dd = {}
        print(f"[rotd] stock_daily.csv unreadable: {e}")
    print(f"[rotd] daily CSV: {len(dd)} tickers loaded"
          if dd else "[rotd] daily CSV: NOT FOUND at stock_daily.csv[.gz] "
                     "(repo root) — the chart will stay on the weekly series")
    if dd:
        # refresh the caps FIRST: universe.csv ships a market cap with no date,
        # and every weight below depends on it being current
        uni = load_universe_from_csv()
        uni, caprep = refresh_universe_caps(uni, dd, load_shares_outstanding())
        print(f"[rotd] market caps: {caprep['source']} \u00b7 "
              f"{caprep['measured']} measured, {caprep['implied']} implied, "
              f"{caprep['moved_5pct']} moved >=5% since universe.csv")
        if caprep["inconsistent"]:
            print(f"[rotd] {caprep['inconsistent']} tickers where cap and share "
                  f"count disagree (ADR ratios / preferreds / CEFs) — using "
                  f"implied shares for these: " +
                  ", ".join(r["ticker"] for r in caprep["inconsistent_names"][:8]))
        if caprep["rejected"]:
            print(f"[rotd] REJECTED {caprep['rejected']} implausible cap moves "
                  f"(>300%) — check stock_daily.csv for: " +
                  ", ".join(f"{r['ticker']} {r['pct']:+.0f}%"
                            for r in caprep["rejected_names"][:6]))
        if caprep["biggest"]:
            print("[rotd] biggest cap moves: " +
                  ", ".join(f"{b['ticker']} {b['pct']:+.1f}%"
                            for b in caprep["biggest"][:5]))
        write_json("market_caps", {
            "asof": _now(), "source": caprep["source"],
            "note": ("Market cap recomputed each run as shares x latest "
                     "committed close. 'implied' means shares were derived "
                     "from universe.csv's cap and today's price — the level is "
                     "unchanged, but past sessions still get real weights."),
            "measured": caprep["measured"], "implied": caprep["implied"],
            "rejected": caprep["rejected"],
            "rejected_names": caprep["rejected_names"][:40],
            "inconsistent": caprep["inconsistent"],
            "inconsistent_names": caprep["inconsistent_names"][:60],
            "moved_5pct": caprep["moved_5pct"], "biggest": caprep["biggest"],
            "caps": {tk: round(float(v.get("market_cap") or 0) / 1e9, 3)
                     for tk, v in uni.items() if v.get("market_cap")}})
        cdates, series = compute_rotation_series_daily(dd, uni)
        # ---- SPLICE: the CSV is a snapshot that ends when Colab last ran.
        # Every engine run already computes a cap-weighted 1-day move per
        # sector and industry (d1, from daily_recent.csv). Any logged session
        # AFTER the CSV's last date is compounded onto the end, so the series
        # stays current between weekly rebuilds instead of ageing.
        # Both legs are cap-weighted 1-day returns over the same taxonomy; the
        # CSV leg uses drifting share-based weights and the spliced leg uses
        # universe caps, so the join is labelled rather than hidden.
        spliced = 0
        if cdates:
            last_csv = cdates[-1]
            _last_vals = None
            for d in sorted(seen):
                if d <= last_csv:
                    continue
                row = seen[d]
                # the log written before the double-count guard existed can
                # hold a weekend row and a repeat of the same close — neither
                # is a session, and splicing them invents moves
                if not _is_session(d):
                    print(f"[rotd] splice: skipping {d} (not a trading day)")
                    continue
                _vals = (row.get("sectors"), row.get("industries"))
                if _vals == _last_vals:
                    print(f"[rotd] splice: skipping {d} (identical to the "
                          f"previous logged row — would double-count)")
                    continue
                _last_vals = _vals
                for bucket, key in (("sectors", "sectors"),
                                    ("industries", "industries")):
                    moves = row.get(key) or {}
                    for r in series[bucket]:
                        mv = moves.get(r["name"])
                        nxt = r["series"][-1] * (1 + (mv or 0) / 100.0)
                        r["series"].append(round(nxt, 3))
                cdates.append(d)
                spliced += 1
            if spliced:
                for bucket in ("sectors", "industries"):
                    for r in series[bucket]:
                        r["chg"] = round(r["series"][-1] - 100.0, 2)
                    series[bucket].sort(key=lambda x: -x["chg"])
                print(f"[rotd] spliced {spliced} logged session(s) after "
                      f"{last_csv} onto the CSV history -> now through {cdates[-1]}")
        if cdates and (series["sectors"] or series["industries"]):
            # S&P 500 on the SAME daily grid. Without this the readout showed
            # "-NaN%" because the weekly benchmark cannot be mixed in here.
            bench = None
            try:
                _sp = _json.load(open(_os.path.join(OUTPUTS_DIR, "spx_daily.json")))
                _by = {}
                for b in (_sp.get("bars") or []):
                    if b.get("date") and b.get("c"):
                        _by[str(b["date"])[:10]] = float(b["c"])
                _vals, _base = [], None
                for d in cdates:
                    v = _by.get(d)
                    if v and _base is None:
                        _base = v
                    _vals.append(round(v / _base * 100, 3) if (v and _base) else None)
                # carry the last known level across gaps so the line is continuous
                _last = 100.0
                for i, v in enumerate(_vals):
                    if v is None:
                        _vals[i] = _last
                    else:
                        _last = v
                if _base:
                    bench = {"name": "S&P 500", "series": _vals,
                             "chg": round(_vals[-1] - 100.0, 2)}
                    print(f"[rotd] benchmark: S&P 500 aligned to the daily grid "
                          f"({bench['chg']:+.2f}% over {len(cdates)} sessions)")
            except Exception as e:
                print(f"[rotd] no daily benchmark ({e}) — the S&P row will be hidden")
            payload = {
                "asof": _now(), "dates": cdates, "days": len(cdates),
                "benchmark": bench,
                "cadence": "daily", "source": "stock_daily.csv",
                "cap_source": caprep["source"],
                "csv_through": (cdates[-1 - spliced] if spliced else
                                (cdates[-1] if cdates else None)),
                "spliced_sessions": spliced,
                "note": ("Cap-weighted daily index per sector and industry from "
                         "the committed daily closes. Weights DRIFT: each "
                         "session is weighted by that session's market cap, so "
                         "a winner's current size is never applied to its own "
                         "past returns."),
                "sectors": series["sectors"],
                "industries": series["industries"],
                "today": {"sectors": sec, "industries": ind},
            }
            write_json("rotation_daily", payload)
            print(f"[rotd] rotation_daily.json from stock_daily.csv: "
                  f"{len(cdates)} sessions ({cdates[0]} -> {cdates[-1]}), "
                  f"{len(series['sectors'])} sectors, "
                  f"{len(series['industries'])} industries")
            return
        print("[rotd] stock_daily.csv present but produced no series — "
              "falling back to the append-only log")
    else:
        print("[rotd] no stock_daily.csv — using the append-only log "
              "(build it with tools/build_stock_daily.ipynb for full history)")

    # ---- FALLBACK: compound the logged daily moves into rebased series ----
    dates = sorted(seen)
    def _series(bucket):
        names = set()
        for d in dates:
            names.update((seen[d].get(bucket) or {}).keys())
        out = []
        for nm in sorted(names):
            lvl, ser = 100.0, []
            for d in dates:
                v = (seen[d].get(bucket) or {}).get(nm)
                lvl = lvl * (1 + (v or 0) / 100.0)
                ser.append(round(lvl, 2))
            out.append({"name": nm, "series": ser,
                        "chg": round(ser[-1] - 100.0, 2)})
        out.sort(key=lambda x: -x["chg"])
        return out

    payload = {
        "asof": _now(), "dates": dates, "days": len(dates),
        "cadence": "daily", "source": "daily-log",
        "note": ("Compounded from the cap-weighted 1-day move logged each run. "
                 "The history starts the day this step first ran — it does not "
                 "backfill, because daily bars for the whole universe do not "
                 "exist in this engine."),
        "sectors": _series("sectors"),
        "industries": _series("industries"),
    }
    write_json("rotation_daily", payload)
    print(f"[rotd] rotation_daily.json: {len(dates)} day(s), "
          f"{len(payload['sectors'])} sectors, {len(payload['industries'])} industries")


def run_perf_series():
    """
    outputs/perf_series.json - normalised performance lines for the rotation
    chart: every sector, every industry, and SPX as the benchmark, each rebased
    to 100 at the start of the window.

    Industries come from the cap-weighted index compute_industry_performance
    already builds (60 weekly bars). Sectors come from the SPDR ETFs, resampled
    weekly so the two views share a cadence and can be compared directly.
    """
    import os, json
    import yfinance as yf

    def weekly_norm(sym, weeks=60):
        try:
            df = yf.download(sym, period="18mo", interval="1wk",
                             auto_adjust=True, progress=False)
            if df is None or len(df) == 0:
                return None, None
            c = df["Close"]
            if hasattr(c, "columns"):
                c = c.iloc[:, 0]
            c = c.dropna().tail(weeks)
            if len(c) < 20:
                return None, None
            base = float(c.iloc[0]) or 1.0
            return ([str(d)[:10] for d in c.index],
                    [round(float(v) / base * 100.0, 3) for v in c])
        except Exception as e:
            print(f"[perf] {sym}: {e}")
            return None, None

    dates, spx = weekly_norm("^GSPC")
    if not spx:
        print("[perf] no SPX benchmark - keeping previous file")
        return None

    # PRIMARY: sectors built from the universe's own constituents, so a sector
    # line is genuinely the aggregate of its industries and a ticker rolls into
    # exactly one of each. The ETFs are a different taxonomy and cannot be
    # compared with the industry numbers.
    sectors = []
    try:
        sp = json.load(open(os.path.join(OUTPUTS_DIR, "sector_perf.json")))
        for r in (sp.get("sectors") or []):
            ser = r.get("series")
            if ser and len(ser) >= 20:
                sectors.append({"name": r.get("sector"), "series": ser,
                                "n": r.get("n"), "n_industries": r.get("n_industries"),
                                "mcap_B": r.get("mcap_B"),
                                "chg": round(ser[-1] - 100.0, 2)})
        sectors.sort(key=lambda x: -x["chg"])
    except Exception as e:
        print(f"[perf] universe sector series unavailable: {e}")

    # CROSS-CHECK ONLY: the SPDR ETFs, GICS taxonomy, kept under its own key so
    # nothing accidentally mixes the two classification systems in one chart
    etfs = []
    for etf, label in SECTOR_ETFS.items():
        d, v = weekly_norm(etf)
        if not v:
            continue
        etfs.append({"key": etf, "name": label, "dates": d, "series": v,
                     "chg": round(v[-1] - 100.0, 2)})
    etfs.sort(key=lambda x: -x["chg"])

    industries = []
    try:
        ind = json.load(open(os.path.join(OUTPUTS_DIR, "industry.json")))
        for r in (ind.get("industries") or []):
            ser = r.get("series")
            if ser and len(ser) >= 20:
                industries.append({"name": r.get("industry"),
                                   "sector": r.get("sector"), "series": ser,
                                   "n": r.get("n"), "mcap_B": r.get("mcap_B"),
                                   "chg": round(ser[-1] - 100.0, 2)})
        industries.sort(key=lambda x: -x["chg"])
    except Exception as e:
        print(f"[perf] industry series unavailable: {e}")

    write_json("perf_series", {
        "asof": _now(), "window_weeks": len(spx),
        "benchmark": {"name": "S&P 500", "dates": dates, "series": spx,
                      "chg": round(spx[-1] - 100.0, 2)},
        "taxonomy": "universe: 20 sectors, 125 industries, every industry in "
                    "exactly one sector",
        "sectors": sectors, "industries": industries,
        "sector_etfs": etfs,          # GICS - cross-check only, do not mix
    })
    orphan = [i for i in industries if not i.get("sector")]
    print(f"[perf] {len(sectors)} sectors, {len(industries)} industries "
          f"({len(orphan)} without a parent sector), {len(etfs)} ETFs, "
          f"{len(spx)} weeks, SPX {spx[-1]-100:+.1f}%")
    return len(sectors) + len(industries)


def run_calendar():
    """
    outputs/calendar.json - the dates that change how a week trades.

    Three kinds, and they are not equal:
      FOMC  - a decision that can reprice the whole curve in one afternoon
      CPI   - the print the current regime literally hangs on (its tightest
              gate is CPI y/y against 2.5%)
      OPEX  - monthly expiry. Dealer gamma rolls off and positioning resets,
              so the gamma levels the app draws all month lose their anchor.
              Third Friday, computed rather than listed. Quarterly expiries
              (Mar/Jun/Sep/Dec) are triple witching and matter more.
    """
    from datetime import date, datetime, timedelta
    today = date.today()
    out = []

    for d in FOMC_2026:
        out.append({"date": d, "type": "FOMC", "label": "FOMC decision",
                    "detail": "Rate decision 14:00 ET, press conference 14:30",
                    "importance": "high"})
    for d in CPI_2026:
        out.append({"date": d, "type": "CPI", "label": "CPI release",
                    "detail": "08:30 ET. The regime's tightest gate is CPI y/y.",
                    "importance": "high"})

    for yr in (today.year, today.year + 1):
        for mo in range(1, 13):
            f = _third_friday(yr, mo)
            if f < today - timedelta(days=40):
                continue
            triple = mo in (3, 6, 9, 12)
            out.append({
                "date": f.isoformat(), "type": "OPEX",
                "label": "Triple witching" if triple else "Monthly OPEX",
                "detail": ("Index, options and futures expire together; the "
                           "largest positioning reset of the quarter."
                           if triple else
                           "Monthly expiry. Dealer gamma rolls off and the "
                           "levels reset on Monday."),
                "importance": "high" if triple else "medium"})

    out = [e for e in out if e["date"] >= (today - timedelta(days=2)).isoformat()]
    out.sort(key=lambda e: e["date"])
    write_json("calendar", {"asof": _now(), "count": len(out), "events": out})
    nxt = out[0] if out else None
    print(f"[calendar] {len(out)} upcoming events" +
          (f"; next is {nxt['label']} on {nxt['date']}" if nxt else ""))
    return len(out)


def run_sectors():
    """
    outputs/sector.json - the Markets sector panel.

    AUDIT FINDING: the dashboard fetches sector.json on the Markets tab but
    NOTHING in this engine ever wrote it. The file in the repo was produced by
    hand and has been frozen since June, so the panel has been showing stale
    momentum for weeks with no warning anywhere.
    """
    import yfinance as yf
    import pandas as pd

    out, failed = {}, []
    for etf, label in SECTOR_ETFS.items():
        try:
            h = yf.Ticker(etf).history(period="14mo", interval="1d",
                                       auto_adjust=False)
            if h is None or h.empty or len(h) < 60:
                failed.append(etf)
                continue
            c = h["Close"].dropna()
            last = float(c.iloc[-1])
            sma50 = float(c.tail(50).mean())
            sma50_prev = float(c.tail(60).head(50).mean())

            def back(days):
                return float(c.iloc[-min(len(c), days + 1)])

            out[etf] = {
                "sector": label,
                "close": round(last, 2),
                "sma50": round(sma50, 2),
                "y": round((last / back(252) - 1) * 100, 1),
                "m1": round((last / back(21) - 1) * 100, 1),
                "m3": round((last / back(63) - 1) * 100, 1),
                "above": last > sma50,
                "rising": sma50 > sma50_prev,
                "date": str(c.index[-1])[:10],
            }
        except Exception as e:
            failed.append(etf)
            print(f"[sector] {etf}: {e}")

    if len(out) < 6:
        print(f"[sector] only {len(out)}/11 ETFs resolved - keeping the "
              f"previous file rather than publishing a half panel")
        return None
    if failed:
        print(f"[sector] missing: {', '.join(failed)}")
    write_json("sector", out)
    print(f"[sector] wrote {len(out)} sectors, as of "
          f"{sorted(v['date'] for v in out.values())[-1]}")
    return len(out)


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
        # ---- THE UNIVERSE LEDGER (Tier 0): every name, every run ----------
        # The screener's default view stays passers + near-misses; this file
        # is the search-anything surface behind it. Names the engine could not
        # evaluate (too little history / not in universe CSV) are listed as
        # no_data rather than silently absent — the universe is COMPLETE.
        try:
            _led = dict(v2.get("ledger") or {})
            for _tk in universe:
                if _tk not in _led:
                    _led[_tk] = {"st": "no_data", "gp": 0, "miss": [],
                                 "sec": universe[_tk].get("sector", ""),
                                 "ind": universe[_tk].get("industry", ""),
                                 "mc": round((universe[_tk].get("market_cap") or 0)/1e9, 2)}
            _cnt = {}
            for _r in _led.values():
                _cnt[_r["st"]] = _cnt.get(_r["st"], 0) + 1
            write_json("universe_ledger", {
                "asof": _now(), "count": len(_led), "by_status": _cnt,
                "note": "Gate verdict for every universe name. 'miss' carries "
                        "by-how-much where the gate has a distance.",
                "rows": _led})
            print(f"[ledger] universe_ledger.json: {len(_led)} names — {_cnt}")
        except Exception as e:
            print(f"[ledger] FAILED (non-fatal): {e}")
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
        # The signal log. Non-fatal: a screener that scored fine must still
        # publish even if the log write fails.
        try:
            write_signal_log(v2, regime=result.get("regime"),
                             spx=result.get("spx_close"))
            run_signals_index()
        except Exception as e:
            print(f"[signals] FAILED (non-fatal): {e}")
    except Exception as e:
        result["v2_meta"] = {"error": str(e)}
        print(f"[v2] FAILED (non-fatal): {e}")

    write_json_guarded("stocks", result, _validate_stocks)
    m = result["meta"]
    print(f"[stocks] {m['gate_passers']} passers, {m['breakouts']} breakouts, "
          f"{m['industries_passing']} industries passing")

    # --- charts, from the bars already in memory. No network. ---
    # Only the candidates that made stocks.json, plus anything you've pinned via
    # a committed trades.json — so we don't write 2,893 files for names the
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
    if not auto_pull:
        print("[earnings] SKIPPED — run_stocks called with auto_pull=False, so "
              "no new quarters can be fetched this run")
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
            # NAMES THAT JUST REPORTED jump the queue. earnings_state already
            # knows each ticker's date; once that date passes, its numbers are
            # the only ones that actually changed. Without this the rotating
            # cursor can take weeks to reach a megacap that reported yesterday.
            try:
                from datetime import date as _d, timedelta as _td
                _st = (_load_earnings_state() or {}).get("next_dates") or {}
                _today = _d.today(); _lo = _today - _td(days=12)
                just_reported = []
                for _t, _v in _st.items():
                    if not (isinstance(_v, str) and len(_v) == 10):
                        continue
                    try:
                        _dt = _d.fromisoformat(_v)
                    except Exception:
                        continue
                    if _lo <= _dt <= _today:
                        just_reported.append(_t)
                print(f"[earnings] {len(just_reported)} tickers reported in the "
                      f"last 12 days — checked first")
            except Exception as _e:
                just_reported = []
                print(f"[earnings] just-reported scan skipped: {_e}")

            prio = []
            for _t in ([t for t in sorted(pinned)] + just_reported
                       + [t for t in never_seen if t not in pinned]):
                if _t not in prio:
                    prio.append(_t)
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
        sec_perf = compute_sector_performance(weekly, universe, daily_ret=daily_ret)
        write_json("sector_perf", {"asof": _now(), "count": len(sec_perf),
                                   "taxonomy": "universe (matches industry.json)",
                                   "sectors": sec_perf})
        print(f"[stocks] wrote outputs/sector_perf.json ({len(sec_perf)} sectors, "
              f"same taxonomy as the industries)")
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
    # Elliott's briefing scan names observed Aug 2026 that are NOT in seed —
    # candidates by demonstration (his scan covers them daily). The OCC rules
    # below still confirm or reject each with data; this list only nominates.
    "scan_2026_08": ["GOOG", "MP", "SNDK", "IBM", "CRWD"],
    # Stock cap for the whole GEX universe (indices SPY/QQQ/IWM ride outside
    # the cap). Top-mcap universe names fill remaining slots; Rules 1-3 gate.
    "stock_cap": 50,
    "rule1_min_ratio_pct": 10.0,     # options share-equivalents / shares volume
    "rule2_min_contracts": 100_000,  # avg daily options contracts
    "rule3_min_agg_oi": 500_000,     # aggregate OI, chains within 90d
    "trailing_sessions": 20,
    "provisional_min_samples": 8,    # adaptive depth, same pattern as gate I1
    "confirmed_min_samples": 20,
    "max_state_sessions": 30,        # prune history beyond this
}



def _gex_candidates(cfg=None):
    """
    The GEX-universe candidate list (Tier 2): verified seed first, then
    Elliott's scanned names, then top-market-cap universe names, capped at
    cfg["stock_cap"] stocks (+ SPY/QQQ/IWM outside the cap). Nomination only —
    run_gex_universe's OCC Rules 1-3 still confirm or reject every name.
    """
    cfg = cfg or GEX_UNIVERSE
    idx = [s for s in cfg["seed"] if s in ("SPY", "QQQ", "IWM")]
    out, seen = [], set()
    def _add(sym):
        if sym and sym not in seen and sym not in ("SPY", "QQQ", "IWM"):
            seen.add(sym); out.append(sym)
    for s in cfg["seed"]:
        _add(s)
    for s in cfg.get("scan_2026_08", []):
        _add(s)
    cap = int(cfg.get("stock_cap", 50))
    if len(out) < cap:
        try:
            uni = load_universe_from_csv()
            for tk, _v in sorted(uni.items(),
                                 key=lambda kv: -(kv[1].get("market_cap") or 0)):
                if len(out) >= cap:
                    break
                _add(tk)
        except Exception as e:
            print(f"[gexu] top-mcap fill skipped: {e}")
    return out[:cap] + idx


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


def evaluate_gex_universe(samples, shares_avg, cfg=None, ibkr_seed=None,
                          candidates=None):
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
    # candidates is INJECTED by the caller (run_gex_universe) precisely so this
    # function stays pure — _gex_candidates() reads universe.csv.
    out = []
    for sym in (candidates if candidates is not None else cfg["seed"]):
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
    _gex_cands = _gex_candidates(cfg)   # computed ONCE per run (reads universe.csv)
    print(f"[gexu] {len(_gex_cands)} candidates "
          f"(seed + briefing scan + top-mcap fill, cap {cfg.get('stock_cap')})")
    for sym in _gex_cands:
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
    for sym in _gex_cands:
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

    universe = evaluate_gex_universe(samples, shares_avg, cfg, ibkr_seed,
                                     candidates=_gex_cands)
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
                uni = []
        # UNION with the raw seed list (fix 2026-07-23): gex_universe.json only
        # holds names Stage 0 has ACCUMULATED so far (often just a handful), so
        # reading it alone silently skipped most seed names — AAPL et al. got no
        # histogram. The histogram is a planning tool: compute it for every seed
        # name plus anything Stage 0 has found, not just the Rules 1-2 passers.
        names = [r["ticker"] for r in uni if r.get("ticker")]
        names += [t for t in GEX_UNIVERSE["seed"] if t not in names]
        names += [t for t in _pinned_tickers() if t not in names]
        tickers = [t for t in names if t not in ("SPY", "QQQ", "IWM")]
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
    # "spot" must agree with the regime tile, which reads the completed daily
    # close series. Ticker.history's last row can be a PARTIAL pre-open bar
    # (the 14.90-vs-15.46 split Gabriel caught on 10 Aug): prefer the macro
    # series' last close, fall back to the pulled 30D value, and say which.
    spot_v, spot_src = vals["30D"], "vix_30d_pull"
    try:
        import json as _j
        _ms = _j.load(open(os.path.join(OUTPUTS_DIR, "macro_series.json")))
        _rows = _ms.get("series") or []            # a LIST of daily row dicts
        _last = next((r.get("vix") for r in reversed(_rows)
                      if r.get("vix") is not None), None)
        if _last:
            spot_v, spot_src = round(float(_last), 2), "macro_daily_close"
    except Exception:
        pass
    payload = {
        "asof": _now(),
        "source": "yahoo_vix_indices",
        "note": "4-point index term structure (9D/30D/3M/6M), auto-generated. "
                "A manually uploaded VX futures ladder can overwrite this file "
                "and survives failed pulls (guarded writes).",
        "spot": spot_v,
        "spot_source": spot_src,
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
    want = set(_gex_candidates(GEX_UNIVERSE)) | _pinned_tickers()
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

    # ratings/earnings/profile from Yahoo — small set, paced.
    # CSV-derived data above covers every name in `want` (free, no network).
    # The Yahoo pass deliberately stays on the ORIGINAL small set: seed +
    # pinned. Widening it to the full ~50-name GEX universe would break this
    # step's stated guarantee of never hitting the per-ticker throttle.
    # Everything else keeps its coverage from the rotating run_research pass.
    yahoo_want = sorted((set(GEX_UNIVERSE["seed"]) | _pinned_tickers())
                        - {"SPY", "QQQ", "IWM"})
    print(f"[detail] bundle covers {len(want)} names; "
          f"Yahoo pass on {len(yahoo_want)} (seed + pinned)")
    try:
        import yfinance as yf
        import time
        got = 0
        for tk in yahoo_want:
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



# Tickers that must be refreshed on EVERY run, no matter what the rotation
# thinks. Add anything you care about; env EARNINGS_FORCE="A,B,C" appends to it.
def _held_and_planned_tickers():
    """Best effort: tickers the engine knows we care about (portfolio seed)."""
    import json, os
    out = []
    for p in ("outputs/trades.json", "outputs/trades_seed.json", "outputs/portfolio.json"):
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p))
        except Exception:
            continue
        items = d if isinstance(d, list) else (d.get("trades") or d.get("positions") or [])
        for it in items:
            tk = (it or {}).get("ticker")
            if tk:
                out.append(str(tk).upper())
    return out


EARNINGS_ALWAYS = ["GOOG", "GOOGL", "TSLA", "MSFT", "META", "AMZN", "AAPL",
                   "NVDA", "SNOW", "AMD", "AVGO", "NFLX"]


def _fetch_quarters_direct(tk, have):
    """
    Pull a ticker's quarterly statements and return rows for quarters we do not
    already have. Deterministic, no queue, no cursor. Returns (rows, note).
    """
    import yfinance as yf

    def _nan(v):
        try:
            return v != v
        except Exception:
            return False

    t = yf.Ticker(tk)
    qf = None
    for acc in ("quarterly_income_stmt", "quarterly_financials",
                "quarterly_incomestmt"):
        try:
            df = getattr(t, acc, None)
            if df is not None and getattr(df, "shape", (0, 0))[1] > 0:
                qf = df
                break
        except Exception:
            continue
    if qf is None:
        return [], "no income statement from yfinance"
    try:
        bs = t.quarterly_balance_sheet
    except Exception:
        bs = None
    try:
        cf = t.quarterly_cashflow
    except Exception:
        cf = None

    rows = []
    for col in list(qf.columns):
        qend = col.strftime("%Y-%m-%d") if hasattr(col, "strftime") else str(col)[:10]
        if qend in have:
            continue

        def g(df, *keys):
            if df is None:
                return ""
            for k in keys:
                try:
                    v = df.loc[k, col]
                except Exception:
                    continue
                if v is None or _nan(v):
                    continue
                try:
                    return float(v)
                except Exception:
                    continue
            return ""

        rev = g(qf, "Total Revenue", "TotalRevenue", "Total Revenues",
                "Operating Revenue", "OperatingRevenue")
        if rev == "":
            return [], f"{qend} has no revenue row (labels: {list(qf.index)[:8]})"
        rows.append({
            "ticker": tk, "quarter_end": qend, "revenue": rev,
            "gross_profit": g(qf, "Gross Profit", "GrossProfit"),
            "operating_income": g(qf, "Operating Income", "OperatingIncome", "EBIT"),
            "net_income": g(qf, "Net Income", "NetIncome",
                            "Net Income Common Stockholders"),
            "ebitda": g(qf, "EBITDA", "Normalized EBITDA"),
            "cost_of_revenue": g(qf, "Cost Of Revenue", "CostOfRevenue"),
            "operating_cash_flow": g(cf, "Operating Cash Flow", "OperatingCashFlow"),
            "free_cash_flow": g(cf, "Free Cash Flow", "FreeCashFlow"),
            "capex": g(cf, "Capital Expenditure", "CapitalExpenditure"),
            "total_debt": g(bs, "Total Debt", "TotalDebt"),
            "total_equity": g(bs, "Stockholders Equity", "StockholdersEquity",
                              "Total Equity Gross Minority Interest"),
            "total_assets": g(bs, "Total Assets", "TotalAssets"),
            "cash": g(bs, "Cash And Cash Equivalents", "CashAndCashEquivalents",
                      "Cash Cash Equivalents And Short Term Investments"),
            "current_assets": g(bs, "Current Assets", "CurrentAssets",
                                "Total Current Assets"),
            "current_liabilities": g(bs, "Current Liabilities", "CurrentLiabilities",
                                     "Total Current Liabilities"),
        })
    return rows, ("ok" if rows else "already up to date")


def run_earnings_refresh():
    """
    Deterministic earnings refresh. Runs as its OWN pipeline step so it cannot be
    starved by the rotation inside run_stocks, and prints one line per ticker so
    a failure is never silent.

    Targets, in order: EARNINGS_ALWAYS + $EARNINGS_FORCE, everything held or
    planned, and anything whose known earnings date fell in the last 25 days.
    """
    import os
    from datetime import date, timedelta

    if not os.path.exists(FUND_CSV):
        print("[refresh] no fundamentals CSV - skipping")
        return None

    quarterly = load_quarterly_fundamentals(FUND_CSV)
    have = {tk: {q.get("q") for q in qs} for tk, qs in quarterly.items()}

    targets = list(EARNINGS_ALWAYS)
    for extra in (os.environ.get("EARNINGS_FORCE") or "").replace(" ", "").split(","):
        if extra:
            targets.append(extra.upper())
    try:
        for tk in (_held_and_planned_tickers() or []):
            targets.append(tk)
    except Exception:
        pass
    try:
        st = (_load_earnings_state() or {}).get("next_dates") or {}
        lo = date.today() - timedelta(days=25)
        for tk, ds in st.items():
            if not (isinstance(ds, str) and len(ds) == 10):
                continue
            try:
                d = date.fromisoformat(ds)
            except Exception:
                continue
            if lo <= d <= date.today():
                targets.append(tk)
    except Exception:
        pass

    seen, order = set(), []
    for tk in targets:
        if tk and tk not in seen:
            seen.add(tk)
            order.append(tk)

    cap = int(os.environ.get("EARNINGS_REFRESH_CAP", "300"))
    if len(order) > cap:
        print(f"[refresh] {len(order)} targets, capping at {cap}")
        order = order[:cap]
    print(f"[refresh] {len(order)} tickers to refresh deterministically")
    all_rows, ok, stale = [], 0, []
    for tk in order:
        try:
            rows, note = _fetch_quarters_direct(tk, have.get(tk, set()))
        except Exception as e:
            print(f"[refresh]   {tk}: FAILED {e}")
            continue
        import time as _tm
        _tm.sleep(0.5)                      # be polite to Yahoo
        if rows:
            ok += 1
            for r in rows:
                print(f"[refresh]   {tk}: NEW {r['quarter_end']} "
                      f"rev ${(r['revenue'] or 0)/1e9:.2f}B")
            all_rows.extend(rows)
        else:
            if note != "already up to date":
                stale.append(f"{tk}: {note}")
            print(f"[refresh]   {tk}: {note}")

    if all_rows:
        added = _append_quarters_to_csv(all_rows)
        print(f"[refresh] appended {added} quarters to {FUND_CSV}")
    else:
        print("[refresh] nothing new to append")
    if stale:
        print(f"[refresh] {len(stale)} tickers returned no usable statement:")
        for s_ in stale[:10]:
            print(f"[refresh]   ! {s_}")
    return len(all_rows)


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

    def _roll(dstr, lag):
        """date + lag, rolled forward a quarter at a time. GRACE: an estimate is
        +/- about a week, so don't roll until it's clearly past — otherwise a
        few days' error becomes a 3-month error."""
        try:
            d = datetime.strptime(dstr, "%Y-%m-%d") + timedelta(days=lag)
            while d < datetime.now() - timedelta(days=7):
                d += timedelta(days=91)
            return d.strftime("%Y-%m-%d")
        except Exception:
            return None

    # The earnings auto-updater already resolved real dates into
    # outputs/earnings_state.json. Read them once here: without this the whole
    # universe falls through to the quarter-end + 118d guess, which lands every
    # ticker sharing a quarter end on the SAME day and then goes stale.
    try:
        _state_next = (_load_earnings_state() or {}).get("next_dates") or {}
    except Exception:
        _state_next = {}
    import re as _re
    _state_next = {k: v for k, v in _state_next.items()
                   if isinstance(v, str) and _re.match(r"^\d{4}-\d{2}-\d{2}$", v)}
    print(f"[finall] earnings_state supplied {len(_state_next)} resolved dates")

    def est_ne(qs, tk):
        # Confirmed dates (verified from company IR/filings) always win. If we
        # only know when they LAST reported, next is ~one quarter later, which
        # is far more accurate than guessing from the fiscal quarter end.
        conf = EARNINGS_CONFIRMED.get(tk) or {}
        if conf.get("next_date"):
            return conf["next_date"], False, conf.get("last_reported")
        if _state_next.get(tk):
            return _state_next[tk], False, conf.get("last_reported")
        if conf.get("last_reported"):
            return _roll(conf["last_reported"], 91), True, conf["last_reported"]
        if not qs:
            return None, True, None
        return _roll(qs[-1]["q"], 118), True, None

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
        ne, is_est, last_rep = est_ne(qs, tk)
        if ne:
            e["earnings"] = {"next_date": ne, "estimated": is_est}
            if last_rep:
                e["earnings"]["last_reported"] = last_rep
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
            nm = info.get("longName") or info.get("shortName")
            if rec is not None:
                if tgt:
                    rec["mean_target"] = tgt
                if nm:
                    rec["name"] = nm      # universe CSV has no name column
                out[tk] = rec
            elif nm:
                # no analyst coverage, but the name is still worth keeping
                out[tk] = {"name": nm, "mean_target": tgt}
                got += 1
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
            "gamma_flip": (round(flip, 2) if flip is not None else None),
            "dist_to_flip_pct": (round((flip / spot - 1) * 100, 2) if flip is not None else None),
            "net_vanna_B_per_volpt": vanna, "net_charm_B_per_day": charm, "vix": vix,
        },
        "raw": {"net_gex_B": round(net_gex, 2), "net_vanna_B": vanna,
                "net_charm_B": charm, "calibrated": True},
        "levels": {
            "pin": put_wall, "call_wall": call_wall, "put_wall": put_wall,
            "gamma_flip": (round(flip, 2) if flip is not None else None), "supports": supports, "resistances": resistances,
            "magnets": {"put": lvl(*put_mag, True) if put_mag else None,
                        "call": lvl(*call_mag, False) if call_mag else None},
            "wall_threshold": 0.30,
        },
        "profile": prof,
        "confidence": {"levels": "high", "regime_sign": "high",
            "note": "Real SPX chains via professional briefing (not the SPY proxy). "
                    "Flip, net-GEX sign, and walls are ground truth."},
    }


def _cboe_chain(symbol):
    """Fetch and normalise one CBOE delayed option chain. Returns (spot, rows)."""
    import requests, re
    from datetime import datetime, date
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json"
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    if r.status_code != 200:
        return None, [], f"HTTP {r.status_code}"
    j = r.json()
    data = j.get("data") or {}
    chain = data.get("options") or []
    try:
        spot = float(data.get("current_price") or data.get("close"))
    except Exception:
        return None, [], "no spot"
    if not chain:
        return spot, [], "empty chain"
    return spot, chain, "ok"


def run_gex_ticker(sym, out_dir=None):
    """Per-ticker GEX from CBOE, scored by gex_engine - same rules as SPX."""
    import os, json
    symbol = sym if sym.startswith("_") else sym.upper()
    try:
        spot, chain, note = _cboe_chain(symbol)
    except Exception as e:
        return None, f"fetch failed: {e}"
    if not spot or not chain:
        return None, note
    rows, _ = _cboe_rows(chain, spot,
                         band=float(os.environ.get("GEX_STOCK_BAND", "0.20")),
                         max_dte=int(os.environ.get("GEX_STOCK_DTE", "90")),
                         min_dte=int(os.environ.get("GEX_MIN_DTE", "1")))
    if len(rows) < 12:
        return None, f"thin chain ({len(rows)} contracts)"
    res = gex_engine(rows, spot, scale=1.0)
    if res.get("error"):
        return None, res["error"]
    res["ticker"] = sym
    res["spot"] = round(spot, 2)
    res["asof"] = _now()
    res["source"] = "CBOE delayed chain (free) - real OI"
    prof = res.get("profile") or []
    total_oi = sum((p.get("coi", 0) + p.get("poi", 0)) for p in prof)
    res["stock_validity"] = ("ok" if len(prof) >= 6 and total_oi >= 500
                             else "INVALID_THIN_CHAIN")
    if out_dir:
        safe = sym.replace("/", "-").replace(".", "-")
        with open(os.path.join(out_dir, f"{safe}.json"), "w") as f:
            json.dump(res, f, separators=(",", ":"))
    return res, "ok"


def run_gex_stocks_cboe(tickers=None, limit=None):
    """
    Per-ticker GEX for the names that matter: what you hold, then the screener's
    gate passers, then the biggest by market cap. One request each, so this is
    capped rather than run across the whole universe.
    """
    import os, json, time
    out_dir = os.path.join(OUTPUTS_DIR, "gex_stocks")
    os.makedirs(out_dir, exist_ok=True)
    limit = int(limit or os.environ.get("GEX_STOCK_CAP", "150"))

    if tickers is None:
        tickers = []
        try:
            tickers += [t for t in (_held_and_planned_tickers() or [])]
        except Exception:
            pass
        try:
            st = json.load(open(os.path.join(OUTPUTS_DIR, "stocks.json")))
            rows = sorted((st.get("stocks") or []),
                          key=lambda r: -(r.get("trade_score") or 0))
            tickers += [r["ticker"] for r in rows if r.get("ticker")]
        except Exception as e:
            print(f"[gexc] stocks.json unreadable: {e}")
        try:
            tickers += list(GEX_UNIVERSE.get("seed") or [])
        except Exception:
            pass

    seen, order = set(), []
    for t in tickers:
        t = (t or "").upper()
        if t and t not in seen:
            seen.add(t); order.append(t)
    order = order[:limit]
    print(f"[gexc] per-ticker GEX for {len(order)} names (cap {limit})")

    ok, thin, fail = 0, 0, 0
    for i, sym in enumerate(order):
        try:
            res, note = run_gex_ticker(sym, out_dir=out_dir)
        except Exception as e:
            fail += 1; note = str(e)[:60]; res = None
        if res:
            ok += 1
        elif "thin" in (note or ""):
            thin += 1
        else:
            fail += 1
        if (i + 1) % 25 == 0:
            print(f"[gexc]   {i+1}/{len(order)}  ok={ok} thin={thin} fail={fail}")
        time.sleep(0.35)
    print(f"[gexc] wrote {ok} per-ticker GEX files "
          f"({thin} thin chains, {fail} failures)")
    return ok


_OSI = __import__("re").compile(r"([A-Z^_]+)(\d{6})([CP])(\d{8})$")


def _cboe_rows(chain, spot, band=0.15, max_dte=180, min_dte=1):
    """CBOE contracts -> the row shape gex_engine wants. Returns (rows, skipped)."""
    from datetime import datetime, date
    today = date.today()
    lo, hi = spot * (1 - band), spot * (1 + band)
    rows, skipped = [], 0
    for c in chain:
        m = _OSI.search((c.get("option") or c.get("symbol") or "").replace(" ", ""))
        if not m:
            skipped += 1
            continue
        k = int(m.group(4)) / 1000.0
        if not (lo <= k <= hi):
            continue
        try:
            dte = (datetime.strptime(m.group(2), "%y%m%d").date() - today).days
        except Exception:
            continue
        # Same-day expiries carry enormous at-the-money gamma and none at all
        # a hundred points away. Including them spikes the chart at spot,
        # flattens every other strike, and pins the flip to wherever spot is.
        # Structural dealer positioning is what we want, so skip 0DTE.
        if dte < min_dte or dte > max_dte:
            continue
        try:
            oi = float(c.get("open_interest") or 0)
        except Exception:
            oi = 0.0
        if oi <= 0:
            continue
        try:
            iv = float(c.get("iv") or 0)
        except Exception:
            iv = 0.0
        if iv <= 0:
            continue
        rows.append({"strike": k, "T_years": max(dte, 1) / 365.0,
                     "kind": "call" if m.group(3) == "C" else "put",
                     "open_interest": oi, "iv": iv})
    return rows, skipped


def _gex_freeze_levels(res):
    """
    Hold the day's levels steady.

    Gamma is a function of live spot and live IV, so recomputing intraday moves
    the flip and the walls on every run. Open interest - the thing that defines
    where dealers are positioned - only changes overnight. So: the first run of
    a new trading day sets the levels; later runs the same day refresh the live
    numbers (spot, net gamma, the profile) but keep the levels frozen.

    Set GEX_FORCE_LEVELS=1 to override.
    """
    import os, json
    from datetime import datetime, timezone

    if os.environ.get("GEX_FORCE_LEVELS") == "1":
        return res
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(OUTPUTS_DIR, "gex.json")
    try:
        prev = json.load(open(path))
    except Exception:
        prev = None
    if not prev:
        res["levels_date"] = today
        return res
    ver = GEX.get("levels_engine", "")
    if (prev.get("levels_date") or "")[:10] != today:
        res["levels_date"] = today
        res["levels_engine"] = ver
        print(f"[gexlock] new session - levels set for {today}")
        return res
    if (prev.get("levels_engine") or "") != ver:
        res["levels_date"] = today
        res["levels_engine"] = ver
        print(f"[gexlock] level math changed ({prev.get('levels_engine') or 'unversioned'}"
              f" -> {ver}) - recomputing rather than holding stale levels")
        return res

    keep_lv = prev.get("levels")
    keep_ov = prev.get("overview") or {}
    if not keep_lv:
        res["levels_date"] = today
        return res
    res["levels"] = keep_lv
    ov = res.setdefault("overview", {})
    for k in ("gamma_flip",):
        if keep_ov.get(k) is not None:
            ov[k] = keep_ov[k]
    spot = ov.get("spx_spot") or ov.get("spot")
    if spot and ov.get("gamma_flip"):
        ov["dist_to_flip_pct"] = round((ov["gamma_flip"] / spot - 1) * 100, 2)
    res["levels_date"] = today
    res["levels_engine"] = ver
    res["levels_locked"] = True
    print(f"[gexlock] levels held from this morning ({today}); "
          f"spot/profile refreshed")
    return res



# ============================================================
# MME SPEC — Elliott's exact computation, received 10 Aug 2026.
#
# The re-solve hypothesis was CONFIRMED. His flip is not a crossing in the
# strike profile: it is the level L at which total dealer gamma, RECOMPUTED at
# L (Black-Scholes from each row's own IV, dollar term using L², sticky-strike
# — IV fixed per row, no smile re-anchor), first changes sign on a 120-point
# grid spanning 0.8-1.2x spot, linearly interpolated.
#
# TWO GAMMA SOURCES BY DESIGN — do not unify them:
#   PATH 1  flip + headline net GEX : pure BS, recomputed per contract
#   PATH 2  walls + magnet/pin     : the vendor gamma columns, scaled at spot
#
# Other spec points that differ from what Phoenix did before:
#   - expiry filter: keep expirations strictly AFTER the scrape date. Nothing
#     else. No DTE cap — the fixture uses 55 expiries.
#   - T in BUSINESS days / 262, floored at 1/262. r = 0, q = 0.
#   - spot is the real SPX cash close passed in, NEVER the header "Last".
#   - walls: the CLOSEST significant strike, not the heaviest. Strikes must be
#     multiples of 50. Floor = 0.25 x the side's max |gex| inside the band.
#     (This is exactly why Phoenix used to pick 7,725 while he published 7,800.)
#
# Verification fixture (his, from the Aug 10 2026 CSV at spot 7757.64):
#   contracts 15,497 · expiries 55 · net +84.72B · flip 7,647.01
#   call wall 7,800 · put wall 7,700          -> run:  --engine gexverify
# ============================================================

_MME_INV_SQRT2PI = 0.3989422804014327


def _mme_busdays(d0, d1):
    """Business days in [d0, d1) — numpy busday_count parity, weekends only."""
    if d1 <= d0:
        return 0
    days = (d1 - d0).days
    full, rem = divmod(days, 7)
    n = full * 5
    w = d0.weekday()
    for i in range(rem):
        if (w + i) % 7 < 5:
            n += 1
    return n


def _mme_T(scrape_date, expiry):
    return max(_mme_busdays(scrape_date, expiry), 1) / 262.0


def _mme_contracts_from_chain(chain, scrape_date):
    """CBOE JSON options -> per-contract rows for both paths."""
    from datetime import datetime
    out, skipped = [], 0
    for c in chain:
        m = _OSI.search((c.get("option") or c.get("symbol") or "").replace(" ", ""))
        if not m:
            skipped += 1
            continue
        try:
            expiry = datetime.strptime(m.group(2), "%y%m%d").date()
        except Exception:
            skipped += 1
            continue
        if expiry <= scrape_date:          # strictly AFTER the scrape date
            continue
        try:
            oi = float(c.get("open_interest") or 0)
        except Exception:
            oi = 0.0
        if oi <= 0:
            continue
        def _f(k):
            try:
                v = float(c.get(k))
                return v if v == v else 0.0
            except Exception:
                return 0.0
        out.append({"K": int(m.group(4)) / 1000.0,
                    "cp": "C" if m.group(3) == "C" else "P",
                    "iv": _f("iv"), "gamma_vendor": _f("gamma"),
                    "oi": oi, "expiry": expiry})
    return out, skipped


def mme_flip_net(contracts, spot, scrape_date):
    """
    PATH 1. Sweep L over linspace(0.8*spot, 1.2*spot, 120); at each L recompute
    every contract's BS gamma from its own IV (sticky-strike) and dollar terms
    with L^2. curve(L) = calls - puts. Flip = first sign change, interpolated.
    Net = the same sum at L = spot, in $B.
    """
    import math
    pre = []
    tcache = {}
    iv_skipped = 0
    for c in contracts:
        sig = c["iv"]
        if sig <= 0:
            iv_skipped += 1
            continue
        T = tcache.get(c["expiry"])
        if T is None:
            T = tcache[c["expiry"]] = _mme_T(scrape_date, c["expiry"])
        st = sig * math.sqrt(T)
        a = 1.0 / st
        c2 = a * (-math.log(c["K"]) + 0.5 * sig * sig * T)
        w = (c["oi"] if c["cp"] == "C" else -c["oi"]) * a
        pre.append((a, c2, w))

    if not pre:
        return None, 0.0, {"grid": 0, "iv_skipped": iv_skipped, "expiries": 0}

    def curve(L):
        lnL = math.log(L)
        s = 0.0
        for a, c2, w in pre:
            d1 = lnL * a + c2
            s += w * math.exp(-0.5 * d1 * d1)
        return s * L * _MME_INV_SQRT2PI      # OI*100*L^2*0.01*gamma folds to this

    lo, hi, n = 0.8 * spot, 1.2 * spot, 120
    step = (hi - lo) / (n - 1)
    flip, prevL, prevG = None, None, None
    for i in range(n):
        L = lo + i * step
        g = curve(L)
        if prevG is not None and (prevG == 0 or (prevG < 0) != (g < 0)):
            K_neg, g_neg = (prevL, prevG) if prevG < 0 else (L, g)
            K_pos, g_pos = (L, g) if g > 0 else (prevL, prevG)
            flip = K_pos - (K_pos - K_neg) * g_pos / (g_pos - g_neg)
            break
        prevL, prevG = L, g
    net_B = curve(spot) / 1e9
    return flip, round(net_B, 2), {"grid": n, "iv_skipped": iv_skipped,
                                   "expiries": len(tcache)}


def mme_walls(contracts, spot):
    """
    PATH 2. Vendor gamma, scaled at spot only. Per-strike aggregation across
    all surviving expiries; bands off spot; strike%50==0; the TOP-1 wall is
    the CLOSEST qualifying strike (floor = 0.25 * side max |gex|), NOT the
    heaviest. Magnets inside the margin; PIN when opposite OI >= 0.5 * primary.
    """
    agg = {}
    for c in contracts:
        k = c["K"]
        a = agg.setdefault(k, {"call_gex": 0.0, "put_gex": 0.0,
                               "call_oi": 0.0, "put_oi": 0.0})
        d = c["gamma_vendor"] * c["oi"] * 100.0 * spot * spot * 0.01
        if c["cp"] == "C":
            a["call_gex"] += d
            a["call_oi"] += c["oi"]
        else:
            a["put_gex"] += -d               # explicit -1: negative by construction
            a["put_oi"] += c["oi"]

    margin, prox = spot * 0.0025, spot * 0.05

    def is50(k):
        return abs(k - round(k / 50.0) * 50.0) < 1e-6

    def side(kind):
        if kind == "put":
            band = [k for k in agg if spot - prox <= k <= spot - margin
                    and is50(k) and agg[k]["put_gex"] < 0]
            tiered = sorted(band, key=lambda k: agg[k]["put_gex"])        # most negative first
            val = lambda k: agg[k]["put_gex"]
        else:
            band = [k for k in agg if spot + margin <= k <= spot + prox
                    and is50(k) and agg[k]["call_gex"] > 0]
            tiered = sorted(band, key=lambda k: -agg[k]["call_gex"])      # most positive first
            val = lambda k: agg[k]["call_gex"]
        if not tiered:
            return None, [], 0.0
        floor = 0.25 * max(abs(val(k)) for k in tiered)
        qual = [k for k in tiered if abs(val(k)) >= floor]
        top1 = (min(qual, key=lambda k: abs(k - spot)) if qual else tiered[0])
        rows = [{"strike": k, "gex_B": round(val(k) / 1e9, 3),
                 "call_oi": int(agg[k]["call_oi"]), "put_oi": int(agg[k]["put_oi"])}
                for k in tiered]
        return top1, rows, floor

    call_top, call_tiered, _ = side("call")
    put_top, put_tiered, _ = side("put")

    cw_oi = agg.get(call_top, {}).get("call_oi", 0) if call_top else 0
    pw_oi = agg.get(put_top, {}).get("put_oi", 0) if put_top else 0
    magnets = []
    for k in agg:
        if not (spot - margin < k < spot + margin) or not is50(k):
            continue
        toi = agg[k]["call_oi"] + agg[k]["put_oi"]
        if toi <= 0:
            continue
        if toi >= 100000 or toi >= 0.5 * max(cw_oi, pw_oi, 1):
            magnets.append({"strike": k, "total_oi": int(toi)})
    magnets.sort(key=lambda m: -m["total_oi"])

    def pin(k, primary):
        if k is None:
            return False
        a = agg[k]
        p, o = (a["call_oi"], a["put_oi"]) if primary == "C" else (a["put_oi"], a["call_oi"])
        return p > 0 and o >= 0.5 * p

    # per-strike rows for the histogram (display band, default +/-10%)
    import os
    band = float(os.environ.get("GEX_BAND", "0.10"))
    prof = []
    for k in sorted(agg):
        if abs(k / spot - 1) > band:
            continue
        a = agg[k]
        prof.append({"strike": k,
                     "call_gex_B": round(a["call_gex"] / 1e9, 4),
                     "put_gex_B": round(a["put_gex"] / 1e9, 4),
                     "net_gex_B": round((a["call_gex"] + a["put_gex"]) / 1e9, 4),
                     "coi": int(a["call_oi"]), "poi": int(a["put_oi"])})

    return {"call_wall": call_top, "put_wall": put_top,
            "call_tiered": call_tiered[:8], "put_tiered": put_tiered[:8],
            "magnets": magnets[:5], "profile": prof,
            "call_wall_pin": pin(call_top, "C"), "put_wall_pin": pin(put_top, "P")}


def _mme_read_csv(path):
    """
    Replay a CBOE delayed_quotes SPX CSV exactly as the spec parses it:
    skip leading blanks, then 3 header rows, 22 columns; scrape date from
    header line 2 ("Date: ..."). Each row yields a call and a put contract.
    """
    import csv, re, os
    from datetime import datetime
    with open(path, newline="") as f:
        raw = [ln for ln in f]
    i = 0
    while i < len(raw) and not raw[i].strip():
        i += 1
    header = raw[i:i + 3]
    body = raw[i + 3:]
    scrape = None
    m = re.search(r"Date:\s*([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", " ".join(header))
    if m:
        try:
            scrape = datetime.strptime(f"{m.group(1)[:3]} {m.group(2)} {m.group(3)}",
                                       "%b %d %Y").date()
        except Exception:
            scrape = None
    if scrape is None:
        env = os.environ.get("GEX_SCRAPE_DATE")
        if env:
            scrape = datetime.strptime(env, "%Y-%m-%d").date()
    if scrape is None:
        raise ValueError("cannot parse scrape date from CSV header; set GEX_SCRAPE_DATE=YYYY-MM-DD")

    def fdate(s):
        s = s.strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%a %b %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except Exception:
                pass
        return None

    out, skipped = [], 0
    for row in csv.reader(body):
        if len(row) < 22:
            if any(x.strip() for x in row):
                skipped += 1
            continue
        exp = fdate(row[0])
        if exp is None:
            skipped += 1
            continue
        if exp <= scrape:                    # strictly AFTER
            continue
        def num(idx):
            try:
                v = float(row[idx])
                return v if v == v else 0.0
            except Exception:
                return 0.0
        K = num(11)
        if K <= 0:
            skipped += 1
            continue
        coi, poi = num(10), num(21)
        if coi > 0:
            out.append({"K": K, "cp": "C", "iv": num(7),
                        "gamma_vendor": num(9), "oi": coi, "expiry": exp})
        if poi > 0:
            # 22-col order: ...17 PutVol, 18 PutIV, 19 PutDelta, 20 PutGamma,
            # 21 PutOpenInt. Index 19 is DELTA — reading it as gamma flipped
            # the put sign and emptied the put wall band. Caught by the
            # synthetic-fixture test.
            out.append({"K": K, "cp": "P", "iv": num(18),
                        "gamma_vendor": num(20), "oi": poi, "expiry": exp})
    return out, scrape, skipped


def run_gex_verify():
    """
    --engine gexverify : print the six fixture numbers for reconciliation.

    Source: GEX_CSV=<path> replays a committed CSV exactly as the coach's
    script reads it (spot from GEX_SPOT, defaulting to the fixture's 7757.64
    with a note). Without GEX_CSV it fetches the live JSON chain (spot from
    GEX_SPOT, else the payload close, with a warning — the spec wants the
    real cash close passed in).
    Fixture (Aug 10 2026 file @ 7757.64): contracts 15,497 · expiries 55 ·
    net +84.72B · flip 7,647.01 · call wall 7,800 · put wall 7,700.
    """
    import os
    from datetime import datetime, timezone
    csvp = os.environ.get("GEX_CSV")
    if csvp:
        contracts, scrape, skipped = _mme_read_csv(csvp)
        spot = float(os.environ.get("GEX_SPOT") or 7757.64)
        src = f"CSV {os.path.basename(csvp)} (scrape {scrape})"
        if not os.environ.get("GEX_SPOT"):
            print("[gexverify] GEX_SPOT not set — using the fixture's 7757.64")
    else:
        spot0, chain, note = _cboe_chain("_SPX")
        if not chain:
            print(f"[gexverify] no chain: {note}")
            return None
        scrape = datetime.now(timezone.utc).date()
        contracts, skipped = _mme_contracts_from_chain(chain, scrape)
        spot = float(os.environ.get("GEX_SPOT") or spot0)
        src = "live CBOE JSON"
        if not os.environ.get("GEX_SPOT"):
            print(f"[gexverify] WARNING: spot {spot} is the payload value, not the "
                  f"official cash close — set GEX_SPOT for a clean comparison")
    flip, net_B, meta = mme_flip_net(contracts, spot, scrape)
    walls = mme_walls(contracts, spot)
    print(f"[gexverify] source {src} · spot {spot:,.2f} · skipped {skipped}")
    print(f"  contracts after filter : {len(contracts)}")
    print(f"  expiries               : {meta['expiries']}")
    print(f"  iv<=0 rows skipped     : {meta.get('iv_skipped', 0)}   "
          f"(if this is large pre-open, run again after the close)")
    print(f"  Net GEX at spot (BS)   : {net_B:+.2f} B")
    print(f"  gamma flip             : {flip:,.2f}" if flip else "  gamma flip             : none in 0.8-1.2x")
    print(f"  call wall (top-1)      : {walls['call_wall']}"
          + ("  PIN" if walls["call_wall_pin"] else ""))
    print(f"  put wall (top-1)       : {walls['put_wall']}"
          + ("  PIN" if walls["put_wall_pin"] else ""))
    if walls["call_tiered"]:
        print("  call tiered:", ", ".join(f"{r['strike']:.0f} ({r['gex_B']:+.2f}B)"
                                          for r in walls["call_tiered"][:5]))
    if walls["put_tiered"]:
        print("  put tiered: ", ", ".join(f"{r['strike']:.0f} ({r['gex_B']:+.2f}B)"
                                          for r in walls["put_tiered"][:5]))
    if walls["magnets"]:
        print("  magnets:    ", ", ".join(f"{m['strike']:.0f} ({m['total_oi']:,})"
                                          for m in walls["magnets"]))
    return {"flip": flip, "net_B": net_B, "walls": walls,
            "contracts": len(contracts), "expiries": meta["expiries"]}

def run_gex_cboe(symbol="_SPX", label="SPX"):
    """
    Real SPX gamma from CBOE's free delayed chain, scored by the SAME
    gex_engine() every other GEX path uses.

    The first version reimplemented the rules and got them wrong: it picked
    walls by dollar gamma across ALL strikes with no above/below-spot
    constraint, so one huge round strike (8000) won call wall, put wall and pin
    at once. gex_engine already encodes the rules -
      * call wall = nearest strike ABOVE spot whose call OI >= 30% of the
        largest call OI above spot (the biggest is kept as the magnet)
      * put wall = the same below spot on put OI
      * pin      = max(call OI + put OI) across the profile
      * flip     = net-GEX sign change nearest spot, interpolated
    - so this now only fetches and normalises, then delegates.
    """
    import os
    from datetime import datetime, date

    try:
        spot, chain, note = _cboe_chain(symbol)
    except Exception as e:
        print(f"[gexcboe] fetch failed: {e}")
        return None
    if not spot or not chain:
        print(f"[gexcboe] {note} - keeping previous gex.json")
        return None
    print(f"[gexcboe] {len(chain)} contracts, spot {spot:,.2f}")

    # Spot: the spec wants the REAL cash close passed in — never the header
    # "Last". Order: GEX_SPOT override, then the engine's own spx_daily close,
    # then the payload value with a warning.
    spot_source = "payload"
    env_spot = os.environ.get("GEX_SPOT")
    if env_spot:
        spot, spot_source = float(env_spot), "GEX_SPOT override"
    else:
        try:
            import json as _j
            _sd = _j.load(open(os.path.join(OUTPUTS_DIR, "spx_daily.json")))
            _bars = _sd.get("bars") or []
            _c = _bars[-1].get("c") if _bars else None
            if _c:
                spot, spot_source = float(_c), "spx_daily close"
        except Exception:
            pass
    if spot_source == "payload":
        print("[gexcboe] WARNING: using payload spot — set GEX_SPOT or ensure "
              "spx_daily.json for the official cash close")

    band = float(os.environ.get("GEX_BAND", "0.10"))
    min_dte = int(os.environ.get("GEX_MIN_DTE", "1"))
    # Was 90 ("near 3-month chain"). The coach's own spec (10 Aug) filters by
    # expiry > scrape date and NOTHING else — his fixture carries 55 expiries.
    # Full chain is the rule now; the env stays for experiments.
    max_dte = int(os.environ.get("GEX_MAX_DTE", "3650"))
    # The legacy engine is BEST-EFFORT now. It garnishes vanna, charm and the
    # deep magnets; the decision numbers come from the MME spec below, and a
    # legacy failure must never block them.
    legacy = None
    try:
        rows, skipped = _cboe_rows(chain, spot, band, max_dte, min_dte)
        if len(rows) >= 40:
            _eng = gex_engine(rows, spot, scale=1.0)
            if not _eng.get("error"):
                legacy = _eng
            else:
                print(f"[gexcboe] legacy engine: {_eng['error']} "
                      f"(vanna/charm unavailable today)")
        else:
            print(f"[gexcboe] legacy rows thin ({len(rows)}) - vanna/charm skipped")
    except Exception as e:
        print(f"[gexcboe] legacy engine failed: {e}")

    res = legacy if legacy else {"overview": {}, "levels": {}, "profile": []}
    res["asof"] = _now()
    res["source"] = f"CBOE delayed chain (free) - real {label} OI, full chain (MME spec)"
    res["symbol"] = label
    ov = res.get("overview") or {}
    lv = res.get("levels") or {}

    def _lv(k):
        v = lv.get(k)
        return v.get("strike") if isinstance(v, dict) else v

    # ---- MME SPEC OVERRIDE (10 Aug 2026) ----------------------------------
    # The legacy engine still runs above for vanna/charm and the deep magnets,
    # but the numbers that gate decisions — flip, headline net, walls, pin —
    # now come from the coach's exact computation. Two gamma sources by
    # design: BS re-solve for flip/net, vendor gamma for walls. Do not unify.
    try:
        from datetime import timezone as _tz
        _scrape = datetime.now(_tz.utc).date()
        _contracts, _skip2 = _mme_contracts_from_chain(chain, _scrape)
        if len(_contracts) < 50:
            raise ValueError(f"only {len(_contracts)} contracts after the expiry filter")
        _flip, _net_B, _meta = mme_flip_net(_contracts, spot, _scrape)
        _walls = mme_walls(_contracts, spot)
        ov = res.setdefault("overview", {})
        lv = res.setdefault("levels", {})
        ov["spx_spot"] = round(spot, 2)
        ov["net_gex_B"] = _net_B
        ov["regime"] = "positive" if _net_B > 0 else "negative"
        if _flip:
            ov["gamma_flip"] = round(_flip, 2)
        lv["call_wall"] = _walls["call_wall"]
        lv["put_wall"] = _walls["put_wall"]
        if _walls["magnets"]:
            lv["pin"] = _walls["magnets"][0]["strike"]
        if _walls.get("profile"):
            res["profile"] = _walls["profile"]
        res["confidence"] = {
            "levels": "high", "regime_sign": "high",
            "note": "Coach's exact spec (10 Aug 2026). Flip/net: BS re-solve. "
                    "Walls: vendor gamma, closest significant.",
        }
        res["mme"] = {
            "method": "MME spec (coach, 10 Aug 2026): BS re-solve flip on a "
                      "120-pt 0.8-1.2x grid, sticky-strike IV, business-day T, "
                      "L^2 dollar term; walls = closest significant on vendor "
                      "gamma, strike%50, floor 25% of side max",
            "spot_source": spot_source,
            "contracts": len(_contracts), "expiries": _meta["expiries"],
            "iv_skipped": _meta.get("iv_skipped", 0),
            "call_tiered": _walls["call_tiered"], "put_tiered": _walls["put_tiered"],
            "magnets": _walls["magnets"],
            "call_wall_pin": _walls["call_wall_pin"],
            "put_wall_pin": _walls["put_wall_pin"],
        }
        # the running acceptance record: one line per day, spec vs briefing
        try:
            import json as _j2
            os.makedirs(os.path.join(OUTPUTS_DIR, "history"), exist_ok=True)
            with open(os.path.join(OUTPUTS_DIR, "history",
                                   "gex_spec_daily.jsonl"), "a") as _f:
                _f.write(_j2.dumps({"date": str(_scrape), "spot": round(spot, 2),
                                    "flip": ov.get("gamma_flip"),
                                    "net_B": _net_B,
                                    "call_wall": _walls["call_wall"],
                                    "put_wall": _walls["put_wall"],
                                    "contracts": len(_contracts)}) + "\n")
        except Exception:
            pass
        print(f"[gexcboe] MME spec: flip {ov.get('gamma_flip')}, net {_net_B:+.2f}B, "
              f"walls {_walls['call_wall']}/{_walls['put_wall']}, "
              f"{len(_contracts)} contracts / {_meta['expiries']} expiries, "
              f"iv-skipped {_meta.get('iv_skipped', 0)} (spot: {spot_source})")
    except Exception as e:
        if legacy:
            print(f"[gexcboe] MME spec FAILED ({e}) — legacy numbers stand for today")
        else:
            print(f"[gexcboe] MME spec FAILED ({e}) and no legacy fallback - "
                  f"keeping previous gex.json")
            return None

    # Open interest only updates overnight, so the LEVELS should too. Without
    # this the flip and walls move on every run purely because spot and IV
    # moved - unusable for a level you are meant to trade against all day.
    res = _gex_freeze_levels(res)
    ov = res.get("overview") or {}
    lv = res.get("levels") or {}
    write_json("gex", res)
    print(f"[gexcboe] net {ov.get('net_gex_B')}B ({ov.get('regime')}), "
          f"flip {ov.get('gamma_flip')}, call wall {_lv('call_wall')}, "
          f"put wall {_lv('put_wall')}, pin {_lv('pin')}, "
          f"{len(res.get('profile') or [])} strikes")
    return len(res.get("profile") or [])


def run_gex_best():
    """
    GEX source of record, in order of trust:
      1. a committed briefing PDF (ground truth when present)
      2. CBOE's real SPX chain
      3. nothing. There is no third source.

    The SPY x10 proxy was REMOVED on 7 Aug 2026. It was not a calibration
    problem: an ETF chain underweights institutional put hedging, so the proxy
    structurally reads positive gamma when SPX is in negative gamma. A fallback
    that produces a confidently WRONG regime is worse than no data, because the
    app renders it identically to a real reading. CBOE now matches the coach's
    published open interest to within a handful of contracts across every
    strike tested, so the proxy has no remaining purpose.
    """
    try:
        import glob as _g
        cand = sorted(_g.glob("briefings/*.pdf"))
        # run_gex_from_briefing does its own newest-by-filename-date selection
        # and returns falsy on a failed parse, so no pre-check is needed here.
        # (A phantom _briefing_is_readable() guard used to sit on this line; it
        # was never defined, so the NameError silently skipped the briefing
        # EVERY day and CBOE ran instead of ground truth.)
        if cand and run_gex_from_briefing():
            print("[gex] source: briefing PDF")
            return True
    except Exception as e:
        print(f"[gex] briefing unavailable ({e})")
    try:
        if run_gex_cboe():
            print("[gex] source: CBOE real SPX chain")
            return True
    except Exception as e:
        print(f"[gex] CBOE failed ({e})")
    # No fallback by design. gex.json keeps yesterday's file, which the app
    # renders with a stale badge — an honest "we don't know today" beats a
    # confident wrong regime.
    print("[gex] NO SOURCE: no briefing and CBOE failed. gex.json NOT rewritten.")
    raise RuntimeError("no GEX source available (briefing absent, CBOE failed)")


def run_gex_from_briefing(path=None):
    """
    Write gex.json from a briefing PDF. If no path is given, use the newest PDF
    in ./briefings/. This is the SPX GEX source of record; CBOE is second.
    """
    import os, glob, re
    from datetime import datetime
    if path is None:
        cands = glob.glob("briefings/*.pdf") + glob.glob("briefings/*.PDF")
        if not cands:
            print("[gexbrief] no briefing in ./briefings/")
            return False
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
    # BOTH chambers. These are flat arrays (one object per transaction); the
    # per-senator files are nested. The parser handles either shape.
    # The House Stock Watcher S3 bucket started returning HTTP 403 in early 2026
    # and is dead. The GitHub mirrors are what still serve. Several path
    # conventions are tried per chamber; a 404 costs one request and is logged,
    # and results are MERGED into the existing file so a partial fetch never
    # wipes verified history.
    "congress_feeds": [
        # --- Senate (confirmed live: aggregate/all_transactions.json) ---------
        "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/master/aggregate/all_transactions.json",
        "https://raw.githubusercontent.com/timothycarambat/senate-stock-watcher-data/main/aggregate/all_transactions.json",
        # --- House (mirror; stale but multi-year history is still useful) -----
        "https://raw.githubusercontent.com/timothycarambat/house-stock-watcher-data/master/data/all_transactions.json",
        "https://raw.githubusercontent.com/timothycarambat/house-stock-watcher-data/main/data/all_transactions.json",
        "https://raw.githubusercontent.com/timothycarambat/house-stock-watcher-data/master/aggregate/all_transactions.json",
        "https://raw.githubusercontent.com/timothycarambat/house-stock-watcher-data/main/aggregate/all_transactions.json",
        # --- community backfill collaborating with Senate Stock Watcher -------
        "https://raw.githubusercontent.com/jeremiak/us-senate-financial-disclosure-data/main/data/transactions.json",
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
        # --- added from the full 13F filer scan (Aug 2026) ---------------
        "VIKING GLOBAL INVESTORS LP": 1103804,
        "Elliott Investment Management L.P.": 1791786,
        "COATUE MANAGEMENT LLC": 1135730,
        "PZENA INVESTMENT MANAGEMENT LLC": 1027796,
        "PARNASSUS INVESTMENTS, LLC": 948669,
        "Castle Hook Partners LP": 1687241,
        "Pentwater Capital Management LP": 1425851,
        "DIAMOND HILL CAPITAL MANAGEMENT INC": 1217541,
        "Winslow Capital Management, LLC": 900973,
        "PineStone Asset Management Inc.": 1904893,
        "Independent Franchise Partners LLP": 1483866,
        "ARK Investment Management LLC": 1697748,
        "Mawer Investment Management Ltd.": 1538449,
        "WCM INVESTMENT MANAGEMENT, LLC": 1061186,
        "Rokos Capital Management LLP": 1666335,
        "MARKEL GROUP INC.": 1096343,
        "Temasek Holdings (Private) Ltd": 1021944,
        "WASATCH ADVISORS LP": 814133,
        "WESTFIELD CAPITAL MANAGEMENT CO LP": 1177719,
        "BROOKFIELD Corp /ON/": 1001085,
    },
    "congress_lookback_days": 1200,  # ~3yr: keep multi-year history for backtests
    "min_trade_value_hint": 1000,    # STOCK Act threshold
}

# Per-daily-run cap on the Yahoo ratings fetch (largest caps first). Names are
# skipped for RATINGS_FRESH_DAYS after a successful fetch, so the run advances
# through the universe and daily load drops once coverage is built.
RATINGS_DAILY_CAP = 400
RATINGS_FRESH_DAYS = 10

# Earnings dates VERIFIED from company IR / SEC filings. These override the
# cadence estimate (which is only +/- about a week because reporting lag varies
# by company). The weekly Yahoo refresh also overwrites estimates with real
# dates; this table is for names we've confirmed by hand.
EARNINGS_CONFIRMED = {
    "NVDA": {"next_date": "2026-08-26"},
    "TSLA": {"last_reported": "2026-07-22"},
}


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
    import re as re
    from datetime import datetime, timedelta

    universe = load_universe_from_csv()
    uni_tickers = set(universe.keys())
    if not uni_tickers:
        print("[congress] universe is EMPTY — keeping every ticker rather than "
              "filtering everything away. Fix the universe CSV to restore filtering.")
    cutoff = datetime.now() - timedelta(days=SMART_MONEY["congress_lookback_days"])
    print(f"[congress] universe has {len(uni_tickers)} tickers; "
          f"keeping trades newer than {cutoff.strftime('%Y-%m-%d')}")

    def norm_side(t):
        t = (t or "").lower()
        if "purchase" in t or t.strip() in ("p", "buy"):
            return "buy"
        if "sale" in t or "sold" in t or t.strip() in ("s", "sell"):
            return "sell"
        if "exchange" in t or t.strip() == "e":
            return "exchange"
        return None

    def parse_date(s):
        s = (s or "").strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
        return None

    _reject = {"no_ticker": 0, "not_in_universe": 0, "no_side": 0, "too_old": 0}

    def emit(tx, member, chamber, reported):
        """Normalise ONE transaction record into our schema, or None."""
        # Senate Stock Watcher has no "ticker" field on most rows - the symbol
        # lives in asset_description as "Apple Inc. (AAPL)" or in a differently
        # named key. Reading only tx["ticker"] threw away all 8,350 records.
        tk = (tx.get("ticker") or tx.get("symbol") or tx.get("asset_ticker")
              or "").strip().upper()
        if tk in ("--", "N/A", "NONE", "-"):
            tk = ""
        if not tk:
            desc = (tx.get("asset_description") or tx.get("asset_name")
                    or tx.get("asset") or tx.get("description") or "")
            m = re.search(r"\(([A-Z][A-Z0-9.\-]{0,5})\)", desc)
            if m:
                tk = m.group(1).upper()
            elif desc:
                tk = (_cusip_ticker_from_universe(desc.split("(")[0].strip(),
                                                  _sen_names) or "")
        if not tk:
            _reject["no_ticker"] += 1
            return None
        # Collect EVERY ticker. Filtering here throws away data we cannot get
        # back later - the universe will grow (EU names, other assets) and the
        # dashboard already shows only what it looks up. Coverage is recorded,
        # not enforced.
        if uni_tickers and tk not in uni_tickers:
            _reject["not_in_universe"] += 1
        side = norm_side(tx.get("type") or tx.get("transaction_type")
                         or tx.get("transaction") or tx.get("order_type"))
        if not side:
            _reject["no_side"] += 1
            return None
        td = parse_date(tx.get("transaction_date") or tx.get("date"))
        if td and td < cutoff:
            _reject["too_old"] += 1
            return None
        return tk, {
            "member": member, "chamber": chamber,
            "date": td.strftime("%Y-%m-%d") if td else (tx.get("transaction_date") or ""),
            "reported": reported or tx.get("disclosure_date") or "",
            "side": side, "amount": tx.get("amount") or "",
            "owner": tx.get("owner") or "",
            "asset_type": tx.get("asset_type") or "Stock",
        }

    _sen_names = _sec_name_ticker_map()
    by_ticker = {}
    n_trades, n_seen = 0, 0
    chambers_done = set()
    for url in SMART_MONEY["congress_feeds"]:
        _ch = "senate" if "senate" in url else "house"
        if _ch in chambers_done:
            continue   # already got this chamber from an earlier mirror
        try:
            r = requests.get(url, timeout=90,
                             headers={"User-Agent": "phoenix-smartmoney/1.0"})
            if r.status_code != 200 or not r.content:
                print(f"[congress] {url.split('/')[-1]}: HTTP {r.status_code}")
                continue
            raw = r.json()
        except Exception as e:
            print(f"[congress] feed failed ({url.split('/')[2]}): {e}")
            continue
        if not isinstance(raw, list):
            print(f"[congress] {url.split('/')[2]}: unexpected payload type")
            continue

        got_here = 0
        for rec in raw:
            if not isinstance(rec, dict):
                continue
            n_seen += 1
            # WHO filed it — field name varies by feed
            member = (rec.get("representative") or rec.get("senator")
                      or rec.get("office") or rec.get("member")
                      or (f"{rec.get('first_name','')} {rec.get('last_name','')}").strip()
                      or "Unknown")
            member = member.replace("Hon. ", "").strip()
            chamber = ("Senate" if (rec.get("senator") or "senator" in member.lower()
                                    or "senate" in url) else "House")
            reported = rec.get("disclosure_date") or rec.get("date_recieved") or ""
            nested = rec.get("transactions")
            if isinstance(nested, list):
                # NESTED shape: filer -> transactions[]
                for tx in nested:
                    if not isinstance(tx, dict):
                        continue
                    out = emit(tx, member, chamber, reported)
                    if out:
                        by_ticker.setdefault(out[0], []).append(out[1])
                        n_trades += 1; got_here += 1
            else:
                # FLAT shape: the record IS the transaction
                out = emit(rec, member, chamber, reported)
                if out:
                    by_ticker.setdefault(out[0], []).append(out[1])
                    n_trades += 1; got_here += 1
        print(f"[congress] {_ch}: {len(raw)} records -> {got_here} usable trades "
              f"({url.split('/')[-2]}/{url.split('/')[-1]})")
        if not got_here and raw:
            print(f"[congress]   rejected: {_reject}")
            _k = raw[0] if isinstance(raw[0], dict) else {}
            print(f"[congress]   first record keys: {sorted(_k)[:14]}")
        if got_here:
            chambers_done.add(_ch)

    if n_seen == 0:
        print("[congress] no feed reachable — keeping existing file")
        return None

    # MERGE with what's already on file (fix 2026-07-23). The committed file may
    # hold hand-verified historical trades that predate the lookback window, and
    # a flaky/partial feed must never wipe them. Dedupe on
    # (member, date, side, amount).
    existing = {}
    ep = os.path.join(OUTPUTS_DIR, "congress_trades.json")
    if os.path.exists(ep):
        try:
            existing = json.load(open(ep)).get("tickers", {})
        except Exception:
            existing = {}
    if n_trades == 0:
        print("[congress] feed returned 0 usable trades — keeping existing file intact")
        return None
    merged = {tk: list(rows) for tk, rows in existing.items()}
    added = 0
    for tk, rows in by_ticker.items():
        seen = {(r.get("member"), r.get("date"), r.get("side"), r.get("amount"))
                for r in merged.get(tk, [])}
        for r in rows:
            key = (r.get("member"), r.get("date"), r.get("side"), r.get("amount"))
            if key not in seen:
                merged.setdefault(tk, []).append(r)
                seen.add(key)
                added += 1
    for tk in merged:
        merged[tk].sort(key=lambda x: x.get("date", ""), reverse=True)
    total = sum(len(v) for v in merged.values())
    payload = {"asof": _now(), "source": "STOCK Act PTR (aggregated public feed + committed history)",
               "note": "45-day disclosure lag; amounts are ranges as filed. "
                       "Merged with committed history so verified older trades persist.",
               "ticker_count": len(merged), "trade_count": total,
               "tickers": merged}
    write_json("congress_trades", payload)
    print(f"[congress] congress_trades.json: {total} trades across {len(merged)} tickers "
          f"({added} new from feed, {total-added} retained)")
    return payload


# 13F issuer names carry filing suffixes our universe names never do:
# "ALPHABET INC CL C", "APPLE INC COM", "META PLATFORMS INC CL A". Strip them
# before matching, otherwise most holdings fall through to a CUSIP key and the
# dashboard - which looks up by TICKER - finds nothing.
_13F_SUFFIXES = ("classa", "classb", "classc", "clsa", "clsb", "clsc",
                 "cla", "clb", "clc", "com", "cmn", "commonstock", "shs",
                 "sharesclassa", "sponsoredadr", "spadr", "adr", "ads",
                 "newcom", "new", "reit", "unit", "units", "del", "md",
                 "par", "usd", "ordshs", "ord")


def _strip_issuer_suffix(key):
    changed = True
    while changed and len(key) > 4:
        changed = False
        for suf in _13F_SUFFIXES:
            if key.endswith(suf) and len(key) - len(suf) > 3:
                key = key[: -len(suf)]
                changed = True
    return key


def _sec_name_ticker_map():
    """
    name -> ticker for ~10k US registrants, from SEC's own free file.

    THIS IS WHY 13F MAPPING WAS 0%: universe.csv has only ticker, sector,
    industry, market_cap - no company name - so the issuer-name map built from
    it was always empty. SEC's company_tickers.json carries the registrant
    titles, which is exactly the naming convention 13F filings use.
    Cached in outputs/ so it is fetched once a day at most.
    """
    import os, json, time, requests
    cache = os.path.join(OUTPUTS_DIR, "sec_name_map.json")
    try:
        if os.path.exists(cache) and (time.time() - os.path.getmtime(cache)) < 86400:
            return json.load(open(cache))
    except Exception:
        pass
    ident = os.environ.get("SEC_IDENTITY", "Phoenix research bot")
    out = {}
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                         headers={"User-Agent": ident}, timeout=60)
        r.raise_for_status()
        data = r.json()
        rows = data.values() if isinstance(data, dict) else data
        for row in rows:
            tk = (row.get("ticker") or "").strip().upper()
            nm = _norm_name(row.get("title"))
            if tk and nm and nm not in out:
                out[nm] = tk
                stripped = _strip_issuer_suffix(nm)
                if stripped and stripped not in out:
                    out[stripped] = tk
        print(f"[secmap] {len(out)} company names from SEC company_tickers.json")
        try:
            json.dump(out, open(cache, "w"))
        except Exception:
            pass
    except Exception as e:
        print(f"[secmap] fetch failed ({e}) - issuer-name mapping will be weak")
    return out


def _cusip_ticker_from_universe(issuer_name, universe_by_name):
    """Best-effort issuer-name -> ticker match against our universe."""
    key = _norm_name(issuer_name)
    if not key:
        return None
    if key in universe_by_name:
        return universe_by_name[key]
    stripped = _strip_issuer_suffix(key)
    if stripped in universe_by_name:
        return universe_by_name[stripped]
    best = None
    for nm, tk in universe_by_name.items():
        if not nm or len(nm) <= 4:
            continue
        if nm == stripped or nm.startswith(stripped) or stripped.startswith(nm):
            # prefer the closest-length match: "alphabetinc" over "alphabetincb"
            if best is None or abs(len(nm) - len(stripped)) < best[0]:
                best = (abs(len(nm) - len(stripped)), tk)
    return best[1] if best else None


def _sec_13f_snaps(cik, quarters, identity, universe_by_name):
    """
    Read a manager's recent 13F-HR information tables straight from SEC EDGAR
    over plain HTTP. No third-party client: edgartools was a single point of
    failure for this whole feature and failed silently per manager.

    Returns [(period_end, {key: {shares, value, ticker, name}}), ...] newest first.
    """
    import requests
    import xml.etree.ElementTree as ET
    from time import sleep

    H = {"User-Agent": identity, "Accept-Encoding": "gzip, deflate"}
    cik_int = int(str(cik).lstrip("CIK").lstrip("0") or 0)
    sub = requests.get(f"https://data.sec.gov/submissions/CIK{cik_int:010d}.json",
                       headers=H, timeout=30)
    sub.raise_for_status()
    rec = (sub.json().get("filings") or {}).get("recent") or {}
    forms = rec.get("form") or []
    accs = rec.get("accessionNumber") or []
    rds = rec.get("reportDate") or []
    picks = [(accs[i], rds[i]) for i, f in enumerate(forms)
             if f in ("13F-HR", "13F-HR/A")][:quarters]

    out = []
    for acc, rd in picks:
        base = (f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_int}/{str(acc).replace('-', '')}")
        try:
            items = requests.get(base + "/index.json", headers=H,
                                 timeout=30).json()["directory"]["item"]
        except Exception as e:
            print(f"[13f]   index failed for {acc}: {e}")
            continue
        table = None
        for it in items:
            n = it.get("name", "")
            if not n.lower().endswith(".xml"):
                continue
            try:
                txt = requests.get(f"{base}/{n}", headers=H, timeout=30).text
            except Exception:
                continue
            if "infoTable" in txt:
                table = txt
                break
            sleep(0.12)
        if not table:
            continue
        try:
            root = ET.fromstring(table)
        except Exception as e:
            print(f"[13f]   xml parse failed for {acc}: {e}")
            continue

        hold = {}
        for el in root.iter():
            if not el.tag.endswith("infoTable"):
                continue
            vals = {}
            for c in el.iter():
                tag = c.tag.split("}")[-1]
                if c.text and c.text.strip():
                    vals.setdefault(tag, c.text.strip())
            name = vals.get("nameOfIssuer", "")
            cusip = vals.get("cusip", "")
            try:
                value = float(vals.get("value") or 0)
            except Exception:
                value = 0.0
            try:
                shares = float(vals.get("sshPrnamt") or 0)
            except Exception:
                shares = 0.0
            tk = _cusip_ticker_from_universe(name, universe_by_name) or ""
            key = tk or cusip
            if not key:
                continue
            cur = hold.setdefault(key, {"shares": 0, "value": 0,
                                        "ticker": tk, "name": name})
            cur["shares"] += shares
            cur["value"] += value
        if hold:
            out.append((str(rd)[:10], hold))
        sleep(0.15)

    out.sort(key=lambda x: x[0], reverse=True)
    return out


def run_house_ptr(year=None, max_filings=None):
    """
    Current-year House PTRs straight from the Clerk of the House. This is the
    official source and it is free: the S3/GitHub mirrors we used before are
    either dead (403 since early 2026) or frozen at mid-2025.

    NOTE: politician trades are NOT in SEC EDGAR. The STOCK Act routes them to
    the Clerk of the House and the Senate Office of Public Records.

      {YEAR}FD.zip  ->  {YEAR}FD.xml  ->  filter FilingType='P'
                    ->  /ptr-pdfs/{YEAR}/{DocID}.pdf  ->  pdftotext  ->  rows

    Each PDF transaction row carries a "(TICKER) [TYPE]" marker, which is what we
    anchor the parse on. Merges into congress_trades.json; never overwrites.
    """
    import io, os, re, json, zipfile, subprocess, requests
    from datetime import datetime, timedelta

    year = year or datetime.now().year
    max_filings = int(max_filings or os.environ.get("HOUSE_PTR_MAX", "400"))
    base = "https://disclosures-clerk.house.gov/public_disc"
    H = {"User-Agent": os.environ.get("SEC_IDENTITY", "Phoenix research bot")}

    try:
        z = requests.get(f"{base}/financial-pdfs/{year}FD.zip", headers=H, timeout=120)
        if z.status_code != 200:
            print(f"[house] {year}FD.zip -> HTTP {z.status_code}")
            return None
        zf = zipfile.ZipFile(io.BytesIO(z.content))
        xml_name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
        if not xml_name:
            print(f"[house] no XML inside {year}FD.zip")
            return None
        import xml.etree.ElementTree as ET
        root = ET.fromstring(zf.read(xml_name))
    except Exception as e:
        print(f"[house] index fetch failed: {e}")
        return None

    filings = []
    for m in root.iter():
        if not m.tag.endswith("Member"):
            continue
        g = {}
        for c in m:
            g[c.tag.split("}")[-1]] = (c.text or "").strip()
        if (g.get("FilingType") or "").upper() != "P":
            continue
        doc = g.get("DocID")
        if not doc:
            continue
        filings.append({
            "doc": doc,
            "member": " ".join(x for x in (g.get("First"), g.get("Last")) if x).strip(),
            "filed": g.get("FilingDate") or "",
            "state": g.get("StateDst") or "",
        })

    def _fd(s_):
        for f_ in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
            try:
                return datetime.strptime(s_, f_)
            except Exception:
                continue
        return None

    seen_path = os.path.join(OUTPUTS_DIR, "house_ptr_seen.json")
    try:
        seen_docs = set(json.load(open(seen_path)))
    except Exception:
        seen_docs = set()
    before = len(filings)
    filings = [f for f in filings if f["doc"] not in seen_docs]
    if before != len(filings):
        print(f"[house] {before - len(filings)} filings already parsed on an "
              f"earlier run - skipping them")
    filings.sort(key=lambda f: (_fd(f["filed"]) or datetime(1900, 1, 1)), reverse=True)
    print(f"[house] {year}FD.xml: {len(filings)} PTR filings; "
          f"parsing the {min(max_filings, len(filings))} most recent")
    filings = filings[:max_filings]

    universe = load_universe_from_csv()
    uni = set(universe.keys())
    TICK = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,5})\)")
    DATE = re.compile(r"(\d{2}/\d{2}/\d{4})")
    MONEY = re.compile(r"\$[\d,]+")
    by_ticker, n_rows, n_pdf, fails = {}, 0, 0, 0

    for f in filings:
        url = f"{base}/ptr-pdfs/{year}/{f['doc']}.pdf"
        try:
            r = requests.get(url, headers=H, timeout=60)
            if r.status_code != 200:
                fails += 1
                continue
            p = subprocess.run(["pdftotext", "-layout", "-", "-"],
                               input=r.content, capture_output=True, timeout=60)
            text = p.stdout.decode("utf-8", "ignore")
        except Exception as e:
            fails += 1
            if fails <= 3:
                print(f"[house]   {f['doc']}: {e}")
            continue
        n_pdf += 1
        lines = text.split("\n")
        for i, ln in enumerate(lines):
            mt = TICK.search(ln)
            if not mt:
                continue
            tk = mt.group(1).upper()
            _in_uni = (not uni) or (tk in uni)   # recorded below, never a filter
            # The ticker sits on the line BELOW the transaction row - the asset
            # name wraps, so "(CCI) [ST]" follows "Crown Castle Inc. Common
            # Stock  S  06/30/2026 ...". Looking forward found nothing; look back.
            window = " ".join(lines[max(0, i - 3): i + 2])
            dts = DATE.findall(window)
            if not dts:
                continue
            _cut = window.find(dts[0])
            _pre, _post = window[:_cut], window[_cut:]
            side = None
            for _m in re.finditer(r"\b(P|S|E)\b(?:\s*\(partial\))?", _pre):
                side = {"P": "buy", "S": "sell", "E": "exchange"}[_m.group(1)]
            if re.search(r"urchase", _pre):
                side = "buy"
            if re.search(r"\bSale\b|\bsold\b", _pre):
                side = "sell"
            _money = MONEY.findall(_post)   # after the date, so the "$200?" header is excluded
            amt = (f"{_money[0]} - {_money[1]}" if len(_money) >= 2 else "")
            if not side or side == "exchange":
                continue
            d = _fd(dts[0])
            if not d:
                continue
            # sanity: a transaction cannot post-date the filing that discloses
            # it, and a pre-2020 date inside a current filing is a bond maturity
            # misread as a trade date
            _rep = _fd(f["filed"])
            if d.year < 2020 or (_rep and d.date() > _rep.date()):
                continue
            by_ticker.setdefault(tk, []).append({
                "in_universe": _in_uni,
                "date": d.strftime("%Y-%m-%d"),
                "member": f["member"],
                "chamber": "House",
                "side": side,
                "amount": amt,
                "reported": (_fd(f["filed"]).strftime("%Y-%m-%d")
                             if _fd(f["filed"]) else ""),
                "source": "House Clerk PTR",
            })
            n_rows += 1

    try:
        seen_docs.update(f["doc"] for f in filings)
        json.dump(sorted(seen_docs), open(seen_path, "w"))
    except Exception:
        pass
    _inuni = sum(1 for v in by_ticker.values() for r in v if r.get("in_universe"))
    print(f"[house] parsed {n_pdf} PDFs -> {n_rows} transactions across "
          f"{len(by_ticker)} tickers ({fails} fetch/parse failures)")
    print(f"[house] {_inuni} rows are in the current universe, "
          f"{n_rows - _inuni} kept for later expansion")
    if not n_rows:
        print("[house] nothing parsed - keeping existing file")
        return None

    ep = os.path.join(OUTPUTS_DIR, "congress_trades.json")
    existing = {}
    if os.path.exists(ep):
        try:
            existing = json.load(open(ep)).get("tickers", {}) or {}
        except Exception:
            existing = {}
    merged = {tk: list(rows) for tk, rows in existing.items()}
    added = 0
    for tk, rows in by_ticker.items():
        seen = {(r.get("member"), r.get("date"), r.get("side"), r.get("amount"))
                for r in merged.get(tk, [])}
        for r in rows:
            k = (r.get("member"), r.get("date"), r.get("side"), r.get("amount"))
            if k in seen:
                continue
            merged.setdefault(tk, []).append(r)
            seen.add(k)
            added += 1
    for tk in merged:
        merged[tk].sort(key=lambda x: x.get("date", ""), reverse=True)
    write_json("congress_trades", {
        "asof": _now(),
        "source": "House Clerk PTR (official) + Senate mirror + committed history",
        "ticker_count": len(merged),
        "trade_count": sum(len(v) for v in merged.values()),
        "tickers": merged,
    })
    # member-keyed index: the basis for politician pages, kept in step with
    # the ticker-keyed file so the daily run never leaves the two out of sync.
    by_member = {}
    for tk, rows in merged.items():
        for r in rows:
            nm = r.get("member")
            if not nm:
                continue
            by_member.setdefault(nm, []).append(dict(r, ticker=tk))
    MID = {"$1,001 - $15,000": 8000, "$15,001 - $50,000": 32500,
           "$50,001 - $100,000": 75000, "$100,001 - $250,000": 175000,
           "$250,001 - $500,000": 375000, "$500,001 - $1,000,000": 750000,
           "$1,000,001 - $5,000,000": 3000000}
    members = {}
    for nm, rows in by_member.items():
        rows.sort(key=lambda x: x.get("date", ""), reverse=True)
        counts = {}
        for r in rows:
            counts[r["ticker"]] = counts.get(r["ticker"], 0) + 1
        members[nm] = {
            "member": nm,
            "chamber": rows[0].get("chamber", ""),
            "state": rows[0].get("state", ""),
            "trade_count": len(rows),
            "buy_notional_est": sum(MID.get(r.get("amount"), 0)
                                    for r in rows if r.get("side") == "buy"),
            "sell_notional_est": sum(MID.get(r.get("amount"), 0)
                                     for r in rows if r.get("side") == "sell"),
            "first_trade": rows[-1].get("date", ""),
            "last_trade": rows[0].get("date", ""),
            "top_tickers": sorted(counts.items(), key=lambda x: -x[1])[:12],
            "trades": rows[:400],
        }
    write_json("politicians", {
        "asof": _now(),
        "source": "House Clerk PTR + Senate mirror",
        "member_count": len(members),
        "members": members,
    })
    print(f"[house] merged {added} new House transactions; "
          f"file now has {len(merged)} tickers, {len(members)} members")
    return added


def run_institutional_13f(identity=None, quarters=5):
    """
    Build a ROLLING HISTORY of institutional position changes from SEC 13F-HR.

    First run walks back `quarters` filings per manager (~1 year) and computes the
    change between each consecutive pair. Later runs only process (manager,
    quarter) pairs that aren't already stored — everything previously computed is
    kept as an archive. So history is built once, then extended incrementally.

    edgartools is a free, no-API-key SEC EDGAR client. SEC's fair-access policy
    wants a real contact in the User-Agent: set SEC_IDENTITY. Non-fatal on failure.
    """
    import json, os
    identity = identity or os.environ.get("SEC_IDENTITY") or "Phoenix Research phoenix@example.com"
    print(f"[13f] identity={identity!r} — reading SEC EDGAR over HTTP")

    universe = load_universe_from_csv()
    universe_by_name = {_norm_name(v.get("name")): tk
                        for tk, v in universe.items() if v.get("name")}
    # universe.csv carries no company name, so this map is normally empty.
    # SEC's own registrant list is the reliable source; keep universe entries
    # on top of it where they exist.
    sec_names = _sec_name_ticker_map()
    if sec_names:
        merged_names = dict(sec_names)
        merged_names.update(universe_by_name)
        universe_by_name = merged_names
    print(f"[13f] issuer-name map has {len(universe_by_name)} entries")

    # ---- load the archive so we only compute what's missing ----
    path = os.path.join(OUTPUTS_DIR, "institutional_holdings.json")
    by_ticker, done = {}, set()
    if os.path.exists(path):
        try:
            prev = json.load(open(path))
            by_ticker = prev.get("tickers", {}) or {}
            done = {tuple(x) for x in (prev.get("_done") or [])}
        except Exception:
            by_ticker, done = {}, set()
    before = sum(len(v) for v in by_ticker.values())

    def holdings_map(obj):
        """{key: {shares, value, ticker, name}} for one 13F filing."""
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
            key = tk or cusip
            if not key:
                continue
            cur = out.setdefault(key, {"shares": 0, "value": 0, "ticker": tk, "name": name})
            cur["shares"] += shares
            cur["value"] += value
        return out

    def qdate(obj):
        for attr in ("report_period", "period_of_report", "periodOfReport"):
            v = getattr(obj, attr, None)
            if v:
                return str(v)[:10]
        return None

    added, mgrs_ok = 0, 0
    for mgr, cik in SMART_MONEY["managers"].items():
        try:
            snaps = _sec_13f_snaps(cik, quarters, identity, universe_by_name)
            _pos = sum(len(h) for _, h in snaps)
            _tk = sum(1 for _, h in snaps for v in h.values() if v.get("ticker"))
            print(f"[13f] {mgr}: {len(snaps)} filings, {_pos} positions, "
                  f"{_tk} mapped to tickers"
                  + ("  <-- LOW, issuer-name matching is failing"
                     if _pos and _tk < _pos * 0.4 else ""))
            if len(snaps) < 2:
                print(f"[13f] {mgr}: SKIPPED — need 2 filings to diff")
                continue
            mgrs_ok += 1
            # diff each consecutive pair -> events dated at the NEWER quarter
            for i in range(len(snaps) - 1):
                qd, cur = snaps[i]
                _, prv = snaps[i + 1]
                if (mgr, qd) in done:
                    continue          # already archived
                for key, h in cur.items():
                    tk = h["ticker"]
                    if not tk or (universe and tk not in universe):
                        continue
                    p = prv.get(key, {"shares": 0, "value": 0})
                    dsh = h["shares"] - p["shares"]
                    if p["shares"] == 0 and h["shares"] > 0:
                        action = "NEW"
                    elif h["shares"] > p["shares"]:
                        action = "ADD"
                    elif h["shares"] < p["shares"]:
                        action = "TRIM"
                    else:
                        continue      # unchanged: not a trade, don't store it
                    by_ticker.setdefault(tk, []).append({
                        "manager": mgr, "action": action,
                        "shares": int(h["shares"]), "value_usd": int(h["value"]),
                        "shares_delta": int(dsh),
                        "pct_change": (round(dsh / p["shares"] * 100, 1) if p["shares"] else None),
                        "quarter": qd,
                    })
                    added += 1
                for key, p in prv.items():
                    if key in cur or not p.get("ticker") or (universe and p["ticker"] not in universe):
                        continue
                    by_ticker.setdefault(p["ticker"], []).append({
                        "manager": mgr, "action": "EXIT",
                        "shares": 0,
                        # keep the value they held so the exit has a real size
                        "value_usd": int(p.get("value") or 0),
                        "prior_value_usd": int(p.get("value") or 0),
                        "shares_delta": -int(p["shares"]), "pct_change": -100.0,
                        "quarter": qd,
                    })
                    added += 1
                done.add((mgr, qd))
        except Exception as e:
            print(f"[13f] {mgr} (CIK {cik}) failed: {e}")
            continue

    if not by_ticker:
        print("[13f] nothing fetched and no archive — leaving file untouched")
        return None
    # dedupe + sort newest-first within each ticker
    prio = {"NEW": 0, "ADD": 1, "TRIM": 2, "EXIT": 3}
    for tk in by_ticker:
        seen, uniq = set(), []
        for h in by_ticker[tk]:
            k = (h.get("manager"), h.get("quarter"), h.get("action"), h.get("shares_delta"))
            if k in seen:
                continue
            seen.add(k); uniq.append(h)
        uniq.sort(key=lambda x: (x.get("quarter") or "", -prio.get(x.get("action"), 9)), reverse=True)
        by_ticker[tk] = uniq

    total = sum(len(v) for v in by_ticker.values())
    write_json("institutional_holdings", {
        "asof": _now(),
        "source": "SEC Form 13F-HR (quarterly institutional holdings)",
        "note": f"Rolling ~{quarters}-quarter history of position CHANGES. Built once, "
                f"extended incrementally; prior quarters are archived, never refetched.",
        "managers_tracked": len(SMART_MONEY["managers"]),
        "managers_fetched": mgrs_ok,
        "quarters_span": quarters,
        "ticker_count": len(by_ticker), "change_count": total,
        "_done": sorted([list(x) for x in done]),
        "tickers": by_ticker})
    print(f"[13f] institutional_holdings.json: {total} changes across {len(by_ticker)} tickers "
          f"({added} new this run, {before} archived) from {mgrs_ok}/{len(SMART_MONEY['managers'])} managers")
    return by_ticker



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



# ---- OGE 278-T / 278e: EXECUTIVE BRANCH disclosures -------------------------
# The executive-branch twin of a congressional PTR. Same STOCK Act regime, same
# $1,000 threshold, 30-45 day deadline — but filed with the Office of Government
# Ethics, and published as PDFs with NO structured feed. So we parse the PDF,
# exactly like the GEX briefings: drop files in ./oge/ and commit.
#   OGE Form 278-T  = Periodic Transaction Report (individual trades)  <- the useful one
#   OGE Form 278e   = Annual Public Financial Disclosure (holdings + some trades)
OGE = {
    # Everything lives under outputs/, which already exists in the repo — the
    # operator never has to create a folder. Drop a filing PDF straight into
    # outputs/ (or outputs/oge/, which downloads auto-create) and it's parsed.
    "sources_file": "oge_sources.txt",   # inside OUTPUTS_DIR
    "pdf_subdir": "oge",                 # inside OUTPUTS_DIR, auto-created
    "legacy_dir": "oge",                 # repo-root folder, still honoured
    # transaction-type letters used in the filings
    "types": {"P": "buy", "S": "sell", "S (partial)": "sell", "E": "exchange"},
}


def _oge_paths():
    """Where to look for the URL list and for filing PDFs, in priority order."""
    import os
    out = OUTPUTS_DIR
    src_candidates = [
        os.path.join(out, OGE["sources_file"]),                    # outputs/oge_sources.txt  <- preferred
        os.path.join(out, OGE["pdf_subdir"], "sources.txt"),       # outputs/oge/sources.txt
        os.path.join(OGE["legacy_dir"], "sources.txt"),            # oge/sources.txt (legacy)
    ]
    pdf_dirs = [
        os.path.join(out, OGE["pdf_subdir"]),   # outputs/oge/   (downloads land here)
        out,                                    # outputs/       (drop a PDF here directly)
        OGE["legacy_dir"],                      # oge/           (legacy)
    ]
    return src_candidates, pdf_dirs


def parse_oge_278t(path, universe_by_name=None):
    """
    Parse an OGE 278-T (or the transactions part of a 278e) into trades.

    These PDFs are frequently SCANNED and OCR'd, so the text is noisy ("Yos" for
    "Yes", broken columns). The parser is deliberately tolerant: it anchors on
    rows that contain BOTH a date and a filed dollar range, then attributes the
    nearest preceding asset name. Anything it can't resolve to a universe ticker
    is dropped rather than guessed at.

    Returns {"filer":..., "trades":[...]} or None.
    """
    import subprocess, os, re
    if not os.path.exists(path):
        return None
    txt = ""
    try:
        r = subprocess.run(["pdftotext", "-layout", path, "-"],
                           capture_output=True, text=True, timeout=180)
        if r.returncode == 0:
            txt = r.stdout
    except Exception as e:
        print(f"[oge] pdftotext failed on {os.path.basename(path)}: {e}")
        return None
    if len(txt) < 200:
        print(f"[oge] {os.path.basename(path)}: no extractable text "
              f"(likely a scan without an OCR layer) — skipping")
        return None

    # filer name: from the form fields, else the filename
    filer = None
    m = re.search(r"Last\s*Nam[eo]\s*[:\-]?\s*([A-Z][A-Za-z'\-]+)", txt)
    if m:
        last = m.group(1)
        m2 = re.search(r"First\s*Nam[eo]\s*[:\-]?\s*([A-Z][A-Za-z'\-]+)", txt)
        filer = (m2.group(1) + " " + last) if m2 else last
    if not filer:
        base = os.path.basename(path)
        m3 = re.match(r"([A-Za-z]+),?\s*([A-Za-z]*)", base)
        filer = (m3.group(1) if m3 else base).replace("_", " ").strip()

    AMT = r"\$[\d,]+(?:\s*[\-\u2013]\s*\$[\d,]+)?"
    DATE = r"(\d{1,2}/\d{1,2}/\d{2,4})"
    trades, last_asset = [], None
    for raw in txt.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        # a row with a date AND an amount range is a transaction row
        dm = re.search(DATE, line)
        am = re.search(AMT, line)
        if dm and am:
            # transaction type: a lone P/S/E column, or the spelled-out word
            typ = None
            if re.search(r"\bPurchas", line, re.I):
                typ = "buy"
            elif re.search(r"\bSale|\bSold", line, re.I):
                typ = "sell"
            elif re.search(r"\bExchang", line, re.I):
                typ = "exchange"
            else:
                # The type is a lone P/S/E COLUMN sitting between the asset name
                # and the date. Require whitespace on both sides (so the "S" in
                # "S&P 500" isn't read as a Sale) and take the last one before
                # the date, which is the column itself.
                seg = line[:dm.start()]
                cands = re.findall(r"(?:^|\s)([PSE])(?=\s|$)", seg)
                if cands:
                    typ = OGE["types"].get(cands[-1])
            asset = last_asset
            # the asset may sit at the start of this same row
            head = line[:dm.start()].strip(" |\t")
            head = re.sub(r"^[\d.\)\s]+", "", head)
            if len(head) > 3 and not re.match(r"^[\$\d]", head):
                asset = head
            if not asset:
                continue
            d = dm.group(1)
            try:
                mo, dy, yr = d.split("/")
                yr = ("20" + yr) if len(yr) == 2 else yr
                iso = f"{int(yr):04d}-{int(mo):02d}-{int(dy):02d}"
            except Exception:
                continue
            amt = re.sub(r"\s*[\-\u2013]\s*", " - ", am.group(0))
            trades.append({"asset": asset[:80], "date": iso,
                           "side": typ or "buy", "amount": amt})
        else:
            # asset-name rows: text, no date, no money
            cand = line.strip(" |\t")
            if (3 < len(cand) < 90 and re.search(r"[A-Za-z]{3}", cand)
                    and not re.search(r"\$|\d{1,2}/\d{1,2}/", cand)
                    and not re.match(r"^(Note|Page|OGE|Filer|Comments|Signature|Date|Summary|If you)", cand, re.I)):
                last_asset = cand
    if not trades:
        print(f"[oge] {os.path.basename(path)}: no transaction rows recognised")
        return None
    return {"filer": filer, "trades": trades, "file": os.path.basename(path)}


def fetch_oge_sources():
    """
    Download any filing listed in oge/sources.txt that we don't already have.

    There is NO bulk feed or API for OGE 278-T/278e filings — they're posted as
    individual PDFs. So this is the practical middle ground: keep a plain list of
    URLs (one per line, # for comments) and every daily run pulls anything new.
    Add a line when a new filing is published and it's picked up automatically
    from then on; already-downloaded files are never refetched.
    """
    import os, requests
    srcs, _ = _oge_paths()
    src_file = next((p for p in srcs if os.path.exists(p)), None)
    if not src_file:
        return 0
    d = os.path.join(OUTPUTS_DIR, OGE["pdf_subdir"])
    os.makedirs(d, exist_ok=True)
    urls = []
    for line in open(src_file):
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    got = 0
    for u in urls:
        name = u.split("/")[-1].split("?")[0] or "filing.pdf"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        dest = os.path.join(d, name)
        if os.path.exists(dest):
            continue                     # already have it
        try:
            r = requests.get(u, timeout=90, headers={
                "User-Agent": "phoenix-research/1.0 (+public disclosure archival)"})
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                with open(dest, "wb") as f:
                    f.write(r.content)
                print(f"[oge] downloaded {name} ({len(r.content)//1024}KB)")
                got += 1
            else:
                print(f"[oge] {name}: HTTP {r.status_code} or not a PDF — skipped")
        except Exception as e:
            print(f"[oge] fetch failed for {name}: {e}")
    if got:
        print(f"[oge] {got} new filing(s) downloaded from sources.txt")
    return got


def run_oge_disclosures():
    """
    Parse every OGE PDF in ./oge/ into executive-branch trades and MERGE them
    into congress_trades.json alongside the congressional PTRs, tagged
    branch="Executive". Never overwrites: dedupes on (member,date,side,amount).
    Pulls any new filings listed in oge/sources.txt first.
    """
    import os, glob, json, re
    try:
        fetch_oge_sources()
    except Exception as e:
        print(f"[oge] source fetch skipped: {e}")
    _, pdf_dirs = _oge_paths()
    files, seen_names = [], set()
    for pd in pdf_dirs:
        for f in sorted(glob.glob(os.path.join(pd, "*.pdf")) + glob.glob(os.path.join(pd, "*.PDF"))):
            b = os.path.basename(f)
            if b in seen_names:
                continue
            seen_names.add(b)
            files.append(f)
    if not files:
        print(f"[oge] no filing PDFs found — drop a 278-T/278e into "
              f"{OUTPUTS_DIR}/ (or list its URL in {OUTPUTS_DIR}/{OGE['sources_file']})")
        return None

    universe = load_universe_from_csv()
    by_name = {_norm_name(v.get("name")): tk for tk, v in universe.items() if v.get("name")}
    # universe.csv carries no company-name column, so by_name is normally empty
    # and every asset string fails to resolve - that is why the last run said
    # "29 assets outside the universe". SEC's registrant list fixes it, exactly
    # as it did for the 13F reader.
    sec_names = _sec_name_ticker_map()
    if sec_names:
        merged = dict(sec_names)
        merged.update(by_name)
        by_name = merged
    print(f"[oge] name map has {len(by_name)} entries")
    if not universe:
        print("[oge] universe is EMPTY — only assets with an explicit (TICKER) "
              "in the filing can be matched.")
    # asset strings also often contain the ticker in parens: "Comcast Corp (CMCSA)"
    tick_re = re.compile(r"\(([A-Z]{1,5})\)")

    def to_ticker(asset):
        m = tick_re.search(asset or "")
        if m and (not universe or m.group(1) in universe):
            return m.group(1)
        a = (asset or "")
        # OGE asset lines carry holding-type noise the issuer name never has:
        # "Apple Inc. - Common Stock (NYSE)", "Vanguard 500 Index Fund Adm"
        for junk in (" - Common Stock", " Common Stock", " Ordinary Shares",
                     " Class A", " Class B", " Class C", " (NYSE)", " (NASDAQ)",
                     " ADR", " Corp.", " Inc.", " Co."):
            a = a.replace(junk, "")
        a = a.split("(")[0].split(",")[0].strip()
        return _cusip_ticker_from_universe(a, by_name)

    path = os.path.join(OUTPUTS_DIR, "congress_trades.json")
    existing = {}
    if os.path.exists(path):
        try:
            existing = json.load(open(path)).get("tickers", {})
        except Exception:
            existing = {}
    merged = {tk: list(rows) for tk, rows in existing.items()}

    added, parsed, unmapped = 0, 0, []
    for f in files:
        got = parse_oge_278t(f)
        if not got:
            continue
        parsed += 1
        for t in got["trades"]:
            tk = to_ticker(t["asset"])
            if not tk:
                unmapped.append(t["asset"])
                continue
            rec = {"member": got["filer"], "chamber": "Executive", "branch": "Executive",
                   "date": t["date"], "reported": "", "side": t["side"],
                   "amount": t["amount"], "owner": "", "asset_type": "Stock",
                   "source": "OGE 278-T"}
            seen = {(r.get("member"), r.get("date"), r.get("side"), r.get("amount"))
                    for r in merged.get(tk, [])}
            key = (rec["member"], rec["date"], rec["side"], rec["amount"])
            if key in seen:
                continue
            merged.setdefault(tk, []).append(rec)
            added += 1
    if unmapped:
        print(f"[oge] {len(unmapped)} unmapped assets, first few:")
        for u in sorted(set(unmapped))[:12]:
            print(f"[oge]   ? {u[:70]}")
    if not added:
        print(f"[oge] parsed {parsed}/{len(files)} file(s), 0 new mappable trades "
              f"({len(unmapped)} assets outside the universe)")
        return None
    for tk in merged:
        merged[tk].sort(key=lambda x: x.get("date", ""), reverse=True)
    total = sum(len(v) for v in merged.values())
    write_json("congress_trades", {
        "asof": _now(),
        "source": "STOCK Act disclosures — congressional PTR + OGE 278-T (executive branch)",
        "note": "45-day lag; amounts are filed ranges. Executive-branch rows come "
                "from OGE Form 278-T PDFs parsed from ./oge/.",
        "ticker_count": len(merged), "trade_count": total, "tickers": merged})
    print(f"[oge] +{added} executive-branch trades from {parsed} filing(s) "
          f"({len(unmapped)} assets not in universe) — congress_trades.json now {total} trades")
    return merged


def run_full():
    """
    ONE job, every step. Each step is non-fatal and independently timed, and
    meta.json is rewritten after every one of them — so if the run is cut short
    you can see exactly which step it reached and how long each took, instead of
    inferring it from file timestamps.

    Order matters: cheap, high-signal steps run before the slow accumulators, so
    a truncated run still refreshes the things the dashboard reads first.
    """
    import time as _t
    print("=== Phoenix full run ===")
    warnings, flags, progress = [], {}, []
    t0 = _t.time()
    budget = float(os.environ.get("PHOENIX_BUDGET_MIN", "300")) * 60.0

    def step(label, fn, optional=False):
        elapsed = _t.time() - t0
        if optional and elapsed > budget:
            print(f"[{label}] SKIPPED — {elapsed/60:.1f}m used of "
                  f"{budget/60:.0f}m budget")
            progress.append({"step": label, "status": "skipped_budget"})
            return
        st = _t.time()
        try:
            fn()
            progress.append({"step": label, "status": "ok",
                             "seconds": round(_t.time() - st, 1)})
            print(f"[{label}] ok in {_t.time()-st:.1f}s")
        except Exception as e:
            warnings.append(f"{label} failed: {e}")
            progress.append({"step": label, "status": "failed",
                             "seconds": round(_t.time() - st, 1), "error": str(e)[:300]})
            print(f"[{label}] FAILED (non-fatal): {e}")
        # rewrite meta after EVERY step: a killed run still leaves a trail
        try:
            write_meta(source_flags=flags, warnings=warnings, progress=progress)
        except Exception:
            pass

    # --- correctness gate: cheap, and everything downstream trusts the book --
    step("trades",         run_trades)

    # --- core: what the dashboard reads first -------------------------------
    step("macro",          run_macro)
    step("spx_daily",      run_spx_daily)
    step("macro_series",   run_macro_series_daily)   # daily, for the brief
    step("macro_daily",    run_macro_daily,   optional=True)   # AFTER macro_series: reads its HY OAS for the credit line
    step("vix_term",       run_vix_term)
    step("gex",            run_gex_best)
    # AFTER gex on purpose: the score reads gex.json, and running before it
    # would grade every morning on yesterday's gamma and self-cap at 45.
    step("dayscore",       run_dayscore,      optional=True)
    step("wire",           run_wire,          optional=True)
    step("calendar",       run_calendar)
    step("sectors",        run_sectors)          # yfinance: keep it next to the
                                                 # other Yahoo pulls, before the
                                                 # heavy CBOE loop
    step("gex_universe",   run_gex_universe)   # feeds the screener - never optional
    step("gex_stocks",     run_gex_stocks_cboe)   # real per-ticker OI, feeds the screener
    step("universe_charts", run_universe_charts, optional=True)
    step("stocks",         run_stocks)

    # --- smart money: cheap, and it was starving at the end of the run ------
    step("perf_series",    run_perf_series)   # needs industry.json from stocks
    step("rotation_daily", run_rotation_daily, optional=True)
    step("rotation_nav",   run_rotation_nav,  optional=True)
    step("congress",       run_congress_trades)
    step("house_ptr",      run_house_ptr)
    step("senate_efd",     run_senate_efd,    optional=True)
    step("oge",            run_oge_disclosures)
    step("institutional_13f", run_institutional_13f)

    # --- earnings + fundamentals -------------------------------------------
    step("earnings_refresh", run_earnings_refresh)
    step("financials_all", run_financials_all)
    step("detail_bundle",  run_detail_bundle)

    # --- the expensive accumulators, budget-aware ---------------------------
    step("research",       run_research,      optional=True)
    step("ratings_all",    lambda: run_ratings_all(limit=RATINGS_DAILY_CAP), optional=True)
    step("theses",         run_theses,        optional=True)
    step("alerts",         run_alerts,        optional=True)

    if PUBLISH_HOLDS:
        print("=== PUBLISH GATE HELD BACK %d FILE(S) ===" % len(PUBLISH_HOLDS))
        for h in PUBLISH_HOLDS:
            print(f"    !! {h}")
        print("    (old data kept on purpose - fix the source, not the gate)")
    ok = sum(1 for p in progress if p["status"] == "ok")
    print(f"=== done — {ok}/{len(progress)} steps ok in {(_t.time()-t0)/60:.1f}m ===")
    for p in progress:
        if p["status"] != "ok":
            print(f"    !! {p['step']}: {p['status']} {p.get('error','')}")

def run_engine(name):
    print(f"=== Phoenix engine: {name} ===")
    if name == "gex":
        run_gex_best()
    elif name == "trades":
        run_trades()
    elif name == "gexsweep":
        run_gex_sweep()
    elif name == "gexverify":
        run_gex_verify()
    elif name == "wire":
        run_wire()
    elif name == "dayscore":
        run_dayscore()
    elif name == "signals":
        run_signals_index()
    elif name == "macro":
        run_macro()
    elif name == "macro_daily":
        run_macro_daily()
    elif name == "rotation_nav":
        run_rotation_nav()
    elif name == "rotation_daily":
        run_rotation_daily()
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
    elif name == "oge":
        run_oge_disclosures()
    elif name == "ogefetch":
        fetch_oge_sources()
    elif name == "backtest":
        backtest_smart_money()
    elif name == "financialsall":
        run_financials_all()
    elif name == "ratingsall":
        run_ratings_all()
    elif name == "universe_charts":
        run_universe_charts()
    elif name == "senate":
        run_senate_efd()
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
