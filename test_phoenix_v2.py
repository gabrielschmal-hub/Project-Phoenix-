#!/usr/bin/env python3
"""
Tests for Scoring System v2 (two-book engine) + promotion spec.
Synthetic data only — no network, no real files except a temp outputs dir.

Run:  python test_phoenix_v2.py
Exit code 0 = all pass. Wire into the Action as a pre-pipeline step:
    python test_phoenix_v2.py && python phoenix.py --full
"""
import json, os, shutil, sys, tempfile

import phoenix as px

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


# ------------------------------------------------------------
# Synthetic builders
# ------------------------------------------------------------
def weekly(closes, vols=None):
    """[(date, close, vol)] with fake ascending dates."""
    vols = vols or [1_000_000] * len(closes)
    return [(f"2024-{(i // 50) + 1:02d}-{(i % 28) + 1:02d}", c, v)
            for i, (c, v) in enumerate(zip(closes, vols))]


def uptrend(n=110, start=50.0, step=0.6):
    return [start + i * step for i in range(n)]


def quarters(rev_yoy=(12, 14, 15, 16), margins=(10, 11, 11, 12, 12),
             roe=15.0, fcf=(1.0, 1.2, 1.1, 1.3), n=8):
    """Build a quarterly list shaped like load_quarterly_fundamentals output."""
    qs = []
    for i in range(n):
        qs.append({
            "q": f"202{4 + i // 4}-{(i % 4) * 3 + 1:02d}-28",
            "rev_yoy": rev_yoy[i - (n - len(rev_yoy))] if i >= n - len(rev_yoy) else 10,
            "net_margin": margins[i - (n - len(margins))] if i >= n - len(margins) else 9,
            "roe": roe, "fcf_B": fcf[i % len(fcf)],
            "fcf_margin": 12.0, "revenue_B": 5.0,
        })
    return qs


def build_world():
    """
    8-ticker world covering every gate path:
      GOOD  - clean uptrend near high, big industry, everything passes
      EXTD  - same but 40% extended above 10wk MA -> ext hard cap blocks it
      THIN  - passes trends but tiny dollar volume -> fails T6
      BASE  - long tight base near high (base_quality high)
      DOWN  - downtrend, passes nothing
      INV1  - steady compounder, full I-gates pass
      INV2  - compounder with decelerating margins -> fails I3
      MONO  - sole member of a dead-breadth industry handled separately
    """
    up = uptrend()
    stock_data = {
        "GOOD": weekly(up, [1_000_000] * 95 + [3_000_000] * 15),
        "EXTD": weekly(up[:-1] + [up[-1] * 1.45]),
        "THIN": weekly(up, [3_000] * 110),
        "BASE": weekly(up[:80] + [up[79]] * 30),
        "DOWN": weekly(list(reversed(up))),
        "INV1": weekly(uptrend(n=110, start=40, step=0.35)),
        "INV2": weekly(uptrend(n=110, start=40, step=0.35)),
        "PEER1": weekly(uptrend()), "PEER2": weekly(uptrend()),
        "PEER3": weekly(uptrend()),
    }
    universe = {
        tk: {"sector": "Tech", "industry": "Software", "market_cap": 5e9, "name": tk}
        for tk in stock_data
    }
    universe["THIN"]["market_cap"] = 2e9
    quarterly = {
        "INV1": quarters(),
        "INV2": quarters(margins=(15, 14, 12, 10, 8)),   # margin collapse -> I3 fails
        "GOOD": quarters(),
    }
    return stock_data, universe, quarterly


# ------------------------------------------------------------
print("== unit: helpers ==")
check("max_drawdown flat", px._max_drawdown_pct([10, 10, 10]) == 0.0)
check("max_drawdown 50%", abs(px._max_drawdown_pct([10, 20, 10, 15]) + 50) < 1e-9)
check("complete_weeks drops last", px._complete_weeks([1, 2, 3]) == [1, 2])
check("complete_weeks single", px._complete_weeks([1]) == [1])

# B1 fix: an inflated final week must NOT move the v2 surge
vols_flat = [1_000_000.0] * 20
vols_spike_last = vols_flat[:-1] + [5_000_000.0]     # fake Monday x5 week
s_flat = px._surge_complete_week(vols_flat)
s_spik = px._surge_complete_week(vols_spike_last)
check("B1: partial-week spike ignored", abs(s_flat - s_spik) < 1e-9,
      f"{s_flat} vs {s_spik}")

print("== unit: investment gates ==")
f = px._investment_gate_check(quarters(), 5e9, 40)
check("I-gates all pass on compounder", all(f.values()), str(f))
f = px._investment_gate_check(quarters(margins=(15, 14, 12, 10, 8)), 5e9, 40)
check("I3 fails on margin collapse", not f["margins"] and f["growth"], str(f))
f = px._investment_gate_check(None, 5e9, 40)
check("missing fundamentals fail gates", not all(f.values()))
f = px._investment_gate_check(quarters(), 1e9, 40)
check("I6 size floor enforced", not f["size"])
f = px._investment_gate_check(quarters(), 5e9, 10)
check("I5 trend age enforced", not f["trend_age"])


print("== unit: real-data adaptations ==")
# shallow history: 6 quarters -> only last 2 have rev_yoy; both positive must pass
shallow = quarters(n=6)
for i, q in enumerate(shallow):
    q["rev_yoy"] = None if i < 4 else 12.0        # only 2 known readings
    q["roe"] = 3.0                                 # quarterly ~= 12% TTM
f = px._investment_gate_check(shallow, 5e9, 40)
check("I1 adaptive: 2-of-2 positive passes", f["growth"], str(f))
check("I4 annualized: 3.0%% quarterly (12%% TTM) passes", f["returns"])
for q in shallow:
    q["roe"] = 2.0                                 # 8% TTM -> below floor
f = px._investment_gate_check(shallow, 5e9, 40)
check("I4 annualized: 2.0%% quarterly (8%% TTM) fails", not f["returns"])
shallow[-1]["rev_yoy"] = -1.0                      # 1 of 2 known positive
f = px._investment_gate_check(shallow, 5e9, 40)
check("I1 adaptive: negative reading fails", not f["growth"])
only_one = quarters(n=6)
for i, q in enumerate(only_one):
    q["rev_yoy"] = None if i < 5 else 12.0         # single known reading
f = px._investment_gate_check(only_one, 5e9, 40)
check("I1 adaptive: 1 known positive passes (data-depth reality)", f["growth"])
only_one[-1]["rev_yoy"] = -3.0
f = px._investment_gate_check(only_one, 5e9, 40)
check("I1 adaptive: 1 known negative fails", not f["growth"])
none_known = quarters(n=6)
for q in none_known:
    q["rev_yoy"] = None
f = px._investment_gate_check(none_known, 5e9, 40)
check("I1 adaptive: 0 known readings fails", not f["growth"])

print("== engine: two-book ==")
stock_data, universe, quarterly = build_world()
dv = {tk: 50e6 for tk in stock_data}
dv["THIN"] = 50_000.0                       # fails T6 via dollar volume
v2 = px.stock_engine_v2(stock_data, universe, quarterly=quarterly,
                        daily_ret={"GOOD": 1.2}, dollar_vol=dv,
                        atr14={"GOOD": 2.5})
tr = {c["ticker"]: c for c in v2["trade_ranked"]}
iv = {c["ticker"]: c for c in v2["invest_ranked"]}

check("GOOD is a trade passer", tr.get("GOOD", {}).get("passer") is True)
check("EXTD blocked by ext hard cap", "EXTD" not in tr and v2["meta"]["ext_hard_capped"] >= 1)
check("THIN fails tradability", ("THIN" not in tr) or
      (tr["THIN"]["missing_gate"] == "tradability" and not tr["THIN"]["passer"]))
check("DOWN excluded everywhere", "DOWN" not in tr and "DOWN" not in iv)
check("INV1 in invest book", "INV1" in iv)
check("INV2 fails I3, not in invest book", "INV2" not in iv)
check("trade entries carry ATR from daily", tr.get("GOOD", {}).get("atr14_pct") == 2.5)
check("ranks are 1..n", [c["rank"] for c in v2["trade_ranked"]] ==
      list(range(1, len(v2["trade_ranked"]) + 1)))
check("no blend: separate scores", "trade_score" in tr.get("GOOD", {}) and
      "invest_score" in iv.get("INV1", {}) and
      "opp_score" not in tr.get("GOOD", {}))
check("meta validated flag is False", v2["meta"]["validated"] is False)

print("== engine: industry breadth (B3) ==")
# 1 mega-cap up, 3 small members down -> cap-weighted passes, breadth kills it
down = list(reversed(uptrend()))
sd = {"MEGA": weekly(uptrend()),
      "S1": weekly(down), "S2": weekly(down), "S3": weekly(down)}
un = {"MEGA": {"sector": "X", "industry": "Chips", "market_cap": 900e9, "name": "MEGA"},
      "S1": {"sector": "X", "industry": "Chips", "market_cap": 2e9, "name": "S1"},
      "S2": {"sector": "X", "industry": "Chips", "market_cap": 2e9, "name": "S2"},
      "S3": {"sector": "X", "industry": "Chips", "market_cap": 2e9, "name": "S3"}}
_sc, pass_v2, breadth = px.compute_industry_breadth(sd, un)
_sc1, pass_v1 = px.compute_industry_scores(sd, un)
check("v1 fooled by mega-cap", "Chips" in pass_v1)
check("v2 breadth kills it", "Chips" not in pass_v2, f"breadth={breadth}")

print("== promotions ==")
tmp = tempfile.mkdtemp()
old_out = px.OUTPUTS_DIR
px.OUTPUTS_DIR = tmp
try:
    entry, stop = 80.0, 74.0                        # R unit = 6
    last = stock_data["GOOD"][-1][1]                # deep in profit vs 80
    trades = {"trades": [
        {"id": 1, "ticker": "GOOD", "entry": entry, "stop": stop,
         "entry_date": "2025-06-01", "status": "open"},
        {"id": 2, "ticker": "INV2", "entry": 60, "stop": 55,
         "entry_date": "2025-06-01", "status": "open"},
        {"id": 3, "ticker": "GONE", "status": "closed"},
    ]}
    with open(os.path.join(tmp, "trades_log.json"), "w") as fh:
        json.dump(trades, fh)

    # GOOD must be in the invest book for P3 streaks to tick; give it 4 weeks
    quarterly["GOOD"] = quarters()
    v2b = px.stock_engine_v2(stock_data, universe, quarterly=quarterly,
                             dollar_vol=dv)
    check("GOOD qualifies for invest book (test precondition)",
          any(c["ticker"] == "GOOD" for c in v2b["invest_ranked"]))

    # simulate 4 distinct ISO weeks of streak
    st = {"streaks": {"GOOD": {"n": 4, "week": "2000-W01"}}}
    px._promo_state_save(st)
    payload = px.evaluate_promotions(v2b, stock_data, universe, quarterly)
    evals = {e["ticker"]: e for e in payload["evaluations"]}

    check("closed trade skipped", "GONE" not in evals)
    g = evals["GOOD"]
    check("P1 +1R detected", g["checks"]["P1_plus_1R"], g["notes"])
    check("P3 streak honored (4wk + this run = 5)",
          g["checks"]["P3_score_streak"], g["notes"])
    check("P4 industry passing", g["checks"]["P4_industry"])
    check("P5 stage2 age", g["checks"]["P5_stage2_age"])
    # P2: entry 2025-06-01; quarters() has quarters after that with rev accel
    check("P2 evaluated from post-entry quarters",
          isinstance(g["checks"]["P2_fundamental_confirm"], bool))
    check("eligible == all checks", g["eligible"] == all(g["checks"].values()))
    check("INV2 not eligible (no invest score)", not evals["INV2"]["eligible"])
    check("promotions.json written",
          os.path.exists(os.path.join(tmp, "promotions.json")))

    # unknown never promotes: trade with no entry/stop
    with open(os.path.join(tmp, "trades_log.json"), "w") as fh:
        json.dump({"trades": [{"id": 9, "ticker": "GOOD", "status": "open"}]}, fh)
    payload = px.evaluate_promotions(v2b, stock_data, universe, quarterly)
    e = payload["evaluations"][0]
    check("missing entry/stop -> P1 False, not eligible",
          not e["checks"]["P1_plus_1R"] and not e["eligible"])
finally:
    px.OUTPUTS_DIR = old_out
    shutil.rmtree(tmp, ignore_errors=True)


print("== v1 engines: regime + GEX (C2 coverage) ==")
def macro_rows(n=110, spx0=4000.0, drift=1.0, vix=14.0, cpi=2.5, hy=350.0):
    rows=[]
    for i in range(n):
        rows.append({"date": f"2024-{i//50+1:02d}-{i%28+1:02d}",
                     "spx": spx0+i*drift, "vix": vix, "wti": 75.0, "cpi_yoy": cpi,
                     "us02": 4.0, "real10": 1.8, "hy": hy, "dxy": 103.0, "gold": 2300.0})
    return rows
r = px.macro_engine(macro_rows(drift=10.0))          # ~+2.7% per 13wk: real uptrend
check("goldilocks fires on calm uptrend", r["regime"]=="GOLDILOCKS", r["regime"])
check("A4 note present", "confidence_note" in r)
rows = macro_rows(vix=40.0)
for i,row in enumerate(rows):                          # flat, then -25% crash in last 10wk
    row["spx"] = 4400.0 if i < len(rows)-10 else 4400.0 - (i-(len(rows)-11))*110.0
r = px.macro_engine(rows)
check("crisis fires on drawdown+VIX40", r["regime"]=="CRISIS_PEAK", r["regime"])
r = px.macro_engine(macro_rows(n=5))
check("insufficient history -> UNKNOWN", r["regime"]=="UNKNOWN")

def synth_chain(spot=5000.0, n=40, oi=5000):
    ch=[]
    for i in range(n):
        K = spot*0.90 + i*(spot*0.20/n)
        for kind in ("call","put"):
            ch.append({"strike":K,"T_years":0.1,"kind":kind,"open_interest":oi,"iv":0.2})
    return ch
g = px.gex_engine(synth_chain(), 5000.0, 1.0)
check("gex computes on healthy chain", g.get("overview",{}).get("net_gex_B") is not None)
check("gex flags degenerate chain", px.gex_engine(synth_chain(n=5,oi=10),5000.0,1.0).get("error")=="degenerate_chain")

print("== C3 publish gate ==")
tmp2 = tempfile.mkdtemp(); old_out2 = px.OUTPUTS_DIR; px.OUTPUTS_DIR = tmp2
try:
    good = {"stocks":[{"ticker":"A"}]*60, "meta":{}}
    bad  = {"stocks":[{"ticker":"A"}]*3,  "meta":{}}
    check("guard writes valid payload", px.write_json_guarded("stocks", good, px._validate_stocks))
    check("guard holds back broken payload", not px.write_json_guarded("stocks", bad, px._validate_stocks))
    kept = json.load(open(os.path.join(tmp2,"stocks.json")))
    check("previous good file preserved", len(kept["stocks"])==60)
    check("no previous file -> writes anyway", px.write_json_guarded("macro", {"regime":"UNKNOWN"}, px._validate_macro))
finally:
    px.OUTPUTS_DIR = old_out2; shutil.rmtree(tmp2, ignore_errors=True)

print("== C1/E2 skip cleanly without secrets ==")
os.environ.pop("ANTHROPIC_API_KEY", None); os.environ.pop("NTFY_TOPIC", None)
check("theses skips without key", px.run_theses() is None)
check("alerts skips without topic", px.run_alerts() is None)

print("== regression: v1 untouched ==")
r1 = px.stock_engine(stock_data, universe, None, daily_ret={})
check("v1 still runs and scores", "stocks" in r1 and "meta" in r1)
check("v1 keeps opp_score", all("opp_score" in c for c in r1["stocks"]))

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES: {FAILS}")
    sys.exit(1)
print("ALL TESTS PASS")
