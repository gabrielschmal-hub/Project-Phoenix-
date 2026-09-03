import sys, os, datetime as dt, json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pandas as pd
import signal_log as S

T = dt.date(2026, 9, 2)


def trig(t, **k):
    return {"ticker": t, "trigger": 100.0, "atr_pct": 2.0, "opp_score": 80, "rank": 1,
            "asof": "2026-09-02T20:00:00Z", **k}


def bars(closes, highs=None, lows=None, start="2026-09-02"):
    """Daily OHLC from the signal date INCLUSIVE: bar 0 is the entry bar."""
    n = len(closes)
    idx = pd.date_range(start, periods=n, freq="B")
    return pd.DataFrame({"Close": closes,
                         "High": highs if highs else [c for c in closes],
                         "Low": lows if lows else [c for c in closes]}, index=idx)


# ---------------------------------------------------------------- append semantics

def test_first_appearance_is_new_event():
    rows = S.build_rows([trig("AAPL")], prior=[], today=T, universe={}, profit={})
    assert rows[0]["is_new"] is True and rows[0]["age_days"] == 1


def test_same_name_next_day_is_not_new():                 # the "same names every day" problem
    prior = [{"date": "2026-09-01", "ticker": "AAPL", "age_days": 1}]
    rows = S.build_rows([trig("AAPL")], prior, T, {}, {})
    assert rows[0]["is_new"] is False and rows[0]["age_days"] == 2


def test_reappearance_after_window_is_new_again():
    prior = [{"date": "2026-08-15", "ticker": "AAPL", "age_days": 3}]
    assert S.build_rows([trig("AAPL")], prior, T, {}, {})[0]["is_new"] is True


def test_stop_pct_is_2_5_atr_and_freshness_recorded():
    r = S.build_rows([trig("AAPL")], [], T, {}, {}, now=dt.datetime(2026, 9, 2, 20, 30))[0]
    assert r["stop_pct"] == 5.0 and r["data_age_min"] == 30.0


def test_sector_join_never_guesses():
    r = S.build_rows([trig("AAPL"), trig("ZZZ")], [], T,
                     {"AAPL": {"sector": "Tech", "industry": "HW"}}, {})
    assert r[0]["sector"] == "Tech"
    assert r[1]["sector"] is None and r[1]["profitable_ocf"] is None      # unknown stays unknown


def _snap(tmp_path, day, rows):
    d = tmp_path / "outputs" / "history"; d.mkdir(parents=True, exist_ok=True)
    (d / f"signals_{day}.json").write_text(json.dumps({"date": day, "trade": rows, "invest": []}))


def test_profitability_reads_engine_snapshots_not_earnings_state(tmp_path):
    """Day one: null on all 163 rows because the loader read earnings_state.json (a cursor file)."""
    _snap(tmp_path, "2026-09-01", [{"ticker": "AAPL", "profitability": "profitable"},
                                   {"ticker": "META", "profitability": "investing"},
                                   {"ticker": "RIVN", "profitability": "lossmaking"},
                                   {"ticker": "THIN", "profitability": "marginal"},
                                   {"ticker": "NODATA", "profitability": "unknown"}])
    P = S.load_profitability(history_dir=str(tmp_path / "outputs" / "history"),
                             stocks_path=str(tmp_path / "outputs" / "stocks.json"))
    rows = S.build_rows([trig(t, asof="2026-09-01T20:00:00Z") for t in ("AAPL", "META", "RIVN", "THIN", "NODATA", "NEVER")],
                        [], T, {}, P)
    by = {r["ticker"]: (r["profitability"], r["profitable_ocf"]) for r in rows}
    assert by["AAPL"] == ("profitable", True)
    assert by["META"] == ("investing", True), "OCF positive with heavy capex is profitable, not marginal"
    assert by["RIVN"] == ("lossmaking", False) and by["THIN"] == ("marginal", False)
    assert by["NODATA"] == ("unknown", None), "the engine's unknown is a real answer, not a guess"
    assert by["NEVER"] == (None, None)


def test_profitability_prefers_the_signals_own_day_then_latest(tmp_path):
    _snap(tmp_path, "2026-08-25", [{"ticker": "X", "profitability": "lossmaking"}])
    _snap(tmp_path, "2026-09-01", [{"ticker": "X", "profitability": "profitable"}])
    P = S.load_profitability(history_dir=str(tmp_path / "outputs" / "history"),
                             stocks_path=str(tmp_path / "nope.json"))
    assert S.profit_lookup(P, "2026-08-25", "X") == "lossmaking"      # its own day
    assert S.profit_lookup(P, "2026-08-28", "X") == "profitable"      # no snapshot that day: latest known
    assert S.profit_lookup(P, "2026-09-01", "Y") is None


def test_profitability_falls_back_to_stocks_json(tmp_path):
    (tmp_path / "outputs").mkdir()
    (tmp_path / "outputs" / "stocks.json").write_text(json.dumps({"trade_ranked": [{"ticker": "Q", "profitability": "profitable"}]}))
    P = S.load_profitability(history_dir=str(tmp_path / "outputs" / "history"),
                             stocks_path=str(tmp_path / "outputs" / "stocks.json"))
    assert S.profit_lookup(P, "2026-09-01", "Q") == "profitable"
    assert S.load_profitability(history_dir=str(tmp_path / "x"), stocks_path=str(tmp_path / "y")) == {"by_date": {}, "latest": {}}


def test_rows_are_json_safe():
    json.dumps(S.build_rows([trig("AAPL", atr_pct=None)], [], T, {}, {}), allow_nan=False)


# ---- B2: screener_triggers accumulates. Rows must be dated by their own asof, not by today.

def test_stale_trigger_is_dated_by_its_own_asof_not_today():
    rows = S.build_rows([trig("OLD", asof="2026-08-25T05:41:00Z"),
                         trig("NEW", asof="2026-09-02T20:00:00Z")], [], T, {}, {})
    by = {r["ticker"]: r["date"] for r in rows}
    assert by["OLD"] == "2026-08-25", "a trigger from 25 Aug must not be logged as a 2 Sep signal"
    assert by["NEW"] == "2026-09-02"


def test_append_is_idempotent_across_reruns():
    """The accumulating table returns the same stale rows every day; they must be logged once."""
    first = S.build_rows([trig("OLD", asof="2026-08-25T05:41:00Z")], [], T, {}, {})
    prior = [{"date": r["date"], "ticker": r["ticker"], "age_days": r["age_days"]} for r in first]
    again = S.build_rows([trig("OLD", asof="2026-08-25T05:41:00Z")], prior, T, {}, {})
    assert first and again == [], "re-appending an already-logged (date,ticker) duplicates the log"


def test_age_days_builds_forward_when_several_days_arrive_at_once():
    rows = S.build_rows([trig("X", asof="2026-08-31T20:00:00Z"),
                         trig("X", asof="2026-09-01T20:00:00Z"),
                         trig("X", asof="2026-09-02T20:00:00Z")], [], T, {}, {})
    assert [r["age_days"] for r in rows] == [1, 2, 3]
    assert [r["is_new"] for r in rows] == [True, False, False]


def test_future_asof_is_clamped_to_today():
    r = S.build_rows([trig("X", asof="2027-01-01T00:00:00Z")], [], T, {}, {})[0]
    assert r["date"] == T.isoformat()


# ---- B1: entry price. close comes from the daily bar at mark time, never from the screener.

def test_close_is_null_at_append_because_the_screener_has_no_close_column():
    assert S.build_rows([trig("AAPL")], [], T, {}, {})[0]["close"] is None


def test_entry_ref_only_taken_from_a_same_day_quote():
    live = {"AAPL": {"last": 101.5, "quote_ts": "2026-09-02T19:57:00Z"},
            "STALE": {"last": 50.0, "quote_ts": "2026-08-20T19:57:00Z"}}
    r = S.build_rows([trig("AAPL"), trig("STALE")], [], T, {}, {}, live=live)
    by = {x["ticker"]: x for x in r}
    assert by["AAPL"]["entry_ref"] == 101.5
    assert "entry_ref" not in by["STALE"], "a stale quote must not become an entry price"


def test_mark_row_sets_entry_from_the_signal_bar_when_close_is_null():
    """The day-one bug: every row had close=None, so mark_row returned None and nothing marked."""
    px = bars([100.0] + [100.0 + i for i in range(1, 21)],
              highs=[101.0] + [101.0 + i for i in range(1, 21)],
              lows=[99.0] + [99.0 + i for i in range(1, 21)])
    m = S.mark_row({"close": None, "atr_pct": 2.0, "stop_pct": 5.0}, px)
    assert m is not None, "a null close must no longer skip the row"
    assert m["close"] == 100.0 and m["close_src"] == "daily" and m["bars_marked"] == 20


def test_mark_row_forward_returns_are_measured_after_the_entry_bar():
    px = bars([100.0] + [100.0 + i for i in range(1, 21)],
              highs=[101.0] + [101.0 + i for i in range(1, 21)],
              lows=[99.0] + [99.0 + i for i in range(1, 21)])
    m = S.mark_row({"close": None, "atr_pct": 2.0}, px)
    assert abs(m["r_1d"] - 0.01) < 1e-9 and abs(m["r_20d"] - 0.20) < 1e-9


def test_mark_row_needs_at_least_one_forward_bar():
    assert S.mark_row({"close": None, "atr_pct": 2.0}, bars([100.0])) is None


# ---------------------------------------------------------------- three-ATR path walk

def test_tight_stop_is_hit_where_the_wide_stop_survives():
    """atr 2%: stops sit at 97.0 / 96.0 / 95.0. A dip to 96.5 takes the tight one only.

    96.00 exactly is deliberately avoided: a touch counts as a hit, so it would take 2.0x too.
    """
    px = bars([100.0, 100.0, 100.0], highs=[100.0, 100.0, 100.0], lows=[100.0, 96.5, 100.0])
    m = S.mark_row({"close": None, "atr_pct": 2.0}, px)
    assert m["stop15_hit"] is True and m["stop15_day"] == 1
    assert m["stop20_hit"] is False and m["stop25_hit"] is False
    assert m["hit_stop_20d"] is False                      # legacy column mirrors 2.5x


def test_target_before_stop_is_recorded_in_order_not_by_mfe():
    """MFE 3R with MAE -1R cannot say which came first. The walk can."""
    px = bars([100.0, 112.0, 90.0],
              highs=[100.0, 112.0, 112.0],
              lows=[100.0, 100.0, 90.0])                   # +2.4R on day 1, stopped on day 2
    m = S.mark_row({"close": None, "atr_pct": 2.0}, px)    # 2.5x -> risk 5.0, tp2 110, tp3 115
    assert m["stop25_tp2"] is True and m["stop25_tp3"] is False
    assert m["stop25_hit"] is True and m["stop25_day"] == 2


def test_stop_wins_inside_a_bar_that_touches_both():
    px = bars([100.0, 100.0], highs=[100.0, 120.0], lows=[100.0, 90.0])
    m = S.mark_row({"close": None, "atr_pct": 2.0}, px)
    assert m["stop25_hit"] is True and m["stop25_tp2"] is False, "same-bar ties must go to the stop"


def test_stopped_signal_is_minus_one_R_and_survivor_is_scaled_by_its_own_risk():
    px = bars([100.0, 90.0], highs=[100.0, 100.0], lows=[100.0, 90.0])
    assert S.mark_row({"close": None, "atr_pct": 2.0}, px)["stop25_r"] == -1.0
    up = bars([100.0, 105.0], highs=[100.0, 105.0], lows=[100.0, 100.0])
    m = S.mark_row({"close": None, "atr_pct": 2.0}, up)
    assert abs(m["stop25_r"] - 1.0) < 1e-9 and abs(m["stop15_r"] - (5 / 3)) < 1e-4   # stored 4dp


def test_walk_path_is_inert_without_an_atr():
    assert S.walk_path([(1, 1, 1)], 100.0, None, 2.5) == {}


# ---------------------------------------------------------------- scorecard

def _row(**k):
    base = {"is_new": True, "r_20d": 0.05, "atr_pct": 2.0, "stop_pct": 5.0, "mfe_20d": 0.10, "profitability": "profitable",
            "stop15_hit": False, "stop15_tp2": False, "stop15_tp3": False, "stop15_r": 1.0,
            "stop20_hit": False, "stop20_tp2": False, "stop20_tp3": False, "stop20_r": 1.0,
            "stop25_hit": False, "stop25_tp2": False, "stop25_tp3": False, "stop25_r": 1.0}
    base.update(k)
    return base


def test_kill_criteria_insufficient_then_verdicts():
    assert S.scorecard([_row()] * 10)["verdict"] == "INSUFFICIENT"
    assert S.scorecard([_row()] * 30)["verdict"] == "CONTINUE"
    bad = _row(r_20d=-0.02, mfe_20d=0.01, stop25_hit=True, stop25_r=-1.0)
    sc = S.scorecard([bad] * 30)
    assert sc["verdict"] == "STOP" and len(sc["fails"]) == 3


def test_continuations_never_count_as_events():
    assert S.scorecard([_row(is_new=False)] * 40)["verdict"] == "INSUFFICIENT"


def test_verdict_is_the_pre_registered_25x_column_not_the_best_of_three():
    """A tight stop that looks better must not rescue a failing 2.5x. Multiple comparisons."""
    rows = [_row(r_20d=-0.02, mfe_20d=0.01, stop25_hit=True, stop25_r=-1.0,
                 stop15_r=2.0, stop15_tp2=True)] * 30
    sc = S.scorecard(rows)
    assert sc["verdict"] == "STOP"
    assert sc["atr_grid"]["1.5x"]["E_stop_R"] > S.HURDLE_R      # the flattering column exists
    assert sc["e_price_R"] == sc["atr_grid"]["2.5x"]["E_stop_R"]
    assert "2.5" in sc["decision_variable"]


def test_grid_reports_all_three_multiples_side_by_side():
    g = S.scorecard([_row()] * 30)["atr_grid"]
    assert set(g) == {"1.5x", "2.0x", "2.5x"}
    assert all({"E_stop_R", "E_tp2_R", "E_tp3_R", "stop_hit", "tp2_first",
                "tp3_first", "median_mfe_R"} <= set(v) for v in g.values())


def test_target_policy_pays_2R_when_the_target_came_first():
    rows = [_row(stop25_tp2=True, stop25_hit=True, stop25_r=-1.0)] * 30
    g = S.scorecard(rows)["atr_grid"]["2.5x"]
    assert g["E_stop_R"] == -1.0 and g["E_tp2_R"] == 2.0     # same signal, different exit rule
    assert g["E_tp3_R"] == -1.0                              # 3R never reached: stopped instead




# ---------------------------------------------------------------- lenses: snapshot join, target days, SPX latch

def _snap_full(tmp_path, day, cands, market=None):
    d = tmp_path / "outputs" / "history"; d.mkdir(parents=True, exist_ok=True)
    doc = {"date": day, "context": {"market": market or {}}, "trade": cands, "invest": []}
    (d / f"signals_{day}.json").write_text(json.dumps(doc))
    return str(d)


def test_lens_fields_come_from_the_signals_own_day_snapshot(tmp_path):
    hd = _snap_full(tmp_path, "2026-09-01",
                    [{"ticker": "AAPL", "mcap_B": 3200.5, "breakout": True, "days_on_list": 1,
                      "pos_vs_high": -1.2, "surge": 44, "industry_mom_3m": 6.1, "dollar_vol_M": 900.0, "trade_score": 71}],
                    market={"regime": "ENERGY_SHOCK", "regime_held_weeks": 3, "gex_regime": "Negative Gamma",
                            "spx_close": 6400.0, "spx_vs_50d_pct": 0.8, "spx_vs_200d_pct": 3.2, "vix": 18.4})
    snap = S.load_snapshots(history_dir=hd)
    r = S.build_rows([trig("AAPL", asof="2026-09-01T20:00:00Z")], [], T, {}, {}, snap=snap)[0]
    assert r["mcap_b"] == 3200.5 and r["breakout"] is True and r["days_on_list"] == 1
    assert r["regime"] == "ENERGY_SHOCK" and r["gex_regime"] == "Negative Gamma" and r["vix"] == 18.4
    assert r["spx_vs_200d_pct"] == 3.2


def test_regime_is_not_reconstructed_when_the_snapshot_lacked_it(tmp_path):
    """The six days before the engine fix: context.regime was null. It stays null."""
    hd = _snap_full(tmp_path, "2026-08-25", [{"ticker": "X", "mcap_B": 5.0}], market={})
    snap = S.load_snapshots(history_dir=hd)
    r = S.build_rows([trig("X", asof="2026-08-25T20:00:00Z")], [], T, {}, {}, snap=snap)[0]
    assert r["mcap_b"] == 5.0 and "regime" not in r and "spx_close" not in r


def test_legacy_context_keys_still_read(tmp_path):
    d = tmp_path / "outputs" / "history"; d.mkdir(parents=True)
    (d / "signals_2026-09-03.json").write_text(json.dumps(
        {"date": "2026-09-03", "context": {"regime": "GOLDILOCKS", "spx_close": 6500}, "trade": [], "invest": []}))
    m = S.load_snapshots(history_dir=str(d))["market"]["2026-09-03"]
    assert m["regime"] == "GOLDILOCKS" and m["spx_close"] == 6500


def test_target_days_are_recorded_for_time_in_trade():
    px = bars([100.0, 108.0, 112.0, 116.0],
              highs=[100.0, 108.0, 112.0, 116.0], lows=[100.0, 100.0, 105.0, 110.0])
    m = S.mark_row({"close": None, "atr_pct": 2.0}, px)          # 2.5x: risk 5 -> tp2 110, tp3 115
    assert m["stop25_tp2_day"] == 2 and m["stop25_tp3_day"] == 3 and m["stop25_day"] is None
    assert m["stop15_tp2_day"] == 1                              # 1.5x: risk 3 -> tp2 106, hit on day 1


def test_spx_state_latches_only_when_missing():
    idx = pd.date_range("2025-06-01", periods=260, freq="B")
    spx = pd.DataFrame({"Close": [6000 + i for i in range(260)]}, index=idx)
    day = idx[-1].date().isoformat()
    st = S.spx_state(spx, day)
    assert st["spx_close"] == 6259 and st["spx_vs_50d_pct"] > 0 and st["spx_vs_200d_pct"] > 0
    px = bars([100.0, 101.0], start=day)
    m = S.mark_row({"close": None, "atr_pct": 2.0, "spx_close": None, "date": day}, px, spx=spx)
    assert m["spx_close"] == 6259, "missing SPX state is latched from the series"
    m2 = S.mark_row({"close": None, "atr_pct": 2.0, "spx_close": 6100.0, "date": day}, px, spx=spx)
    assert "spx_close" not in m2, "present SPX state must not be rewritten"
    assert S.spx_state(spx, "2020-01-01") == {}


def test_lenses_cut_by_group_and_flag_thin_cells():
    rows = ([_row(sector="Tech", mcap_b=50.0, regime="GOLDILOCKS", stop25_r=1.5, stop25_day=None)] * 25
            + [_row(sector="Energy", mcap_b=1.0, regime="ENERGY_SHOCK", stop25_r=-1.0, stop25_hit=True, stop25_day=2)] * 5)
    L = S.lenses(rows)
    sec = {c["group"]: c for c in L["sector"]}
    assert sec["Tech"]["n"] == 25 and sec["Tech"]["thin"] is False and sec["Tech"]["E_stop_R"] == 1.5
    assert sec["Energy"]["n"] == 5 and sec["Energy"]["thin"] is True and sec["Energy"]["stop_hit"] == 1.0
    assert {c["group"] for c in L["mcap"]} == {">50B", "<2B"}
    assert {c["group"] for c in L["time_in_trade"]} == {"held 20", "stopped d1-3"}
    assert "regime" in L and "spx_vs_200d" in L and "days_on_list" in L


def test_scorecard_publishes_lenses_even_while_insufficient():
    sc = S.scorecard([_row(sector="Tech")] * 5)
    assert sc["verdict"] == "INSUFFICIENT" and "lenses" in sc and sc["lenses"]["sector"][0]["thin"] is True
    assert sc["min_cell"] == S.MIN_CELL and sc["logged_new"] == 5


def test_column_names_are_lower_case_because_postgres_folds_identifiers():
    """3 Sep: the migration created mcap_B unquoted, so Postgres stored mcap_b. The append sent
    mcap_B and PostgREST rejected the entire batch (PGRST204) — 60 signals lost for a day."""
    for c in S.CAND_FIELDS + S.MARKET_FIELDS:
        assert c == c.lower(), f"{c} will not match the folded column name"
    for m in S.STOP_MULTS:
        for f in ("_hit", "_day", "_tp2", "_tp2_day", "_tp3", "_tp3_day", "_r"):
            col = S.mkey(m) + f
            assert col == col.lower(), col


def test_snapshot_keys_map_to_the_folded_columns(tmp_path):
    """The engine writes mcap_B in signals_<date>.json; the column is mcap_b. Both must work."""
    d = tmp_path / "h"; d.mkdir()
    (d / "signals_2026-09-01.json").write_text(json.dumps(
        {"date": "2026-09-01", "context": {"market": {"regime": "POLICY_TIGHTENING"}},
         "trade": [{"ticker": "AAPL", "mcap_B": 3200.5, "dollar_vol_M": 900.0, "breakout": True}], "invest": []}))
    f = S.snapshot_fields(S.load_snapshots(history_dir=str(d)), "2026-09-01", "AAPL")
    assert f["mcap_b"] == 3200.5 and f["dollar_vol_m"] == 900.0 and f["breakout"] is True
    assert f["regime"] == "POLICY_TIGHTENING"
    assert "mcap_B" not in f, "the mixed-case key must never reach the upsert"
