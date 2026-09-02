import sys, os, datetime as dt, json; sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pandas as pd, signal_log as S

T = dt.date(2026, 9, 2)
def trig(t, **k): return {"ticker": t, "trigger": 100.0, "close": 101.0, "atr_pct": 2.0, "opp_score": 80, "rank": 1, "asof": "2026-09-02T20:00:00Z", **k}

def test_first_appearance_is_new_event():
    rows = S.build_rows([trig("AAPL")], prior=[], today=T, universe={}, profit={})
    assert rows[0]["is_new"] is True and rows[0]["age_days"] == 1

def test_same_name_next_day_is_not_new():          # the "same names every day" problem
    prior = [{"date": "2026-09-01", "ticker": "AAPL", "age_days": 1}]
    rows = S.build_rows([trig("AAPL")], prior, T, {}, {})
    assert rows[0]["is_new"] is False and rows[0]["age_days"] == 2

def test_reappearance_after_window_is_new_again():
    prior = [{"date": "2026-08-15", "ticker": "AAPL", "age_days": 3}]
    assert S.build_rows([trig("AAPL")], prior, T, {}, {})[0]["is_new"] is True

def test_stop_pct_is_2_5_atr_and_freshness_recorded():
    r = S.build_rows([trig("AAPL")], [], T, {}, {}, now=dt.datetime(2026, 9, 2, 20, 30))[0]
    assert r["stop_pct"] == 5.0 and r["data_age_min"] == 30.0

def test_sector_and_profitability_join_never_guess():
    r = S.build_rows([trig("AAPL"), trig("ZZZ")], [], T, {"AAPL": {"sector": "Tech", "industry": "HW"}}, {"AAPL": True})
    assert r[0]["sector"] == "Tech" and r[0]["profitable_ocf"] is True
    assert r[1]["sector"] is None and r[1]["profitable_ocf"] is None      # unknown stays unknown

def test_rows_are_json_safe():
    json.dumps(S.build_rows([trig("AAPL", atr_pct=None)], [], T, {}, {}), allow_nan=False)

def test_mark_row_forward_returns_and_stop():
    idx = pd.date_range("2026-09-03", periods=20, freq="B")
    px = pd.DataFrame({"Close": [100 + i for i in range(20)], "High": [102 + i for i in range(20)], "Low": [99 + i for i in range(20)]}, index=idx)
    m = S.mark_row({"close": 100.0, "stop_pct": 5.0}, px)
    assert abs(m["r_1d"] - 0.0) < 1e-9 and abs(m["r_20d"] - 0.19) < 1e-9 and m["hit_stop_20d"] is False
    m2 = S.mark_row({"close": 100.0, "stop_pct": 0.5}, px); assert m2["hit_stop_20d"] is True

def test_kill_criteria_insufficient_then_verdicts():
    def row(r20, stop_hit, mfe): return {"is_new": True, "r_20d": r20, "stop_pct": 5.0, "hit_stop_20d": stop_hit, "mfe_20d": mfe}
    assert S.scorecard([row(0.1, False, 0.1)] * 10)["verdict"] == "INSUFFICIENT"
    good = [row(0.05, False, 0.10)] * 30            # +1R, stop never hit, MFE 2R
    assert S.scorecard(good)["verdict"] == "CONTINUE"
    bad = [row(-0.02, True, 0.01)] * 30             # -0.4R, stop always hit, MFE 0.2R
    sc = S.scorecard(bad); assert sc["verdict"] == "STOP" and len(sc["fails"]) == 3
    only_old = [{**row(0.05, False, 0.1), "is_new": False}] * 40   # continuing names never count
    assert S.scorecard(only_old)["verdict"] == "INSUFFICIENT"
