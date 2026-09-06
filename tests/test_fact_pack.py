"""The fact pack: every module's view in one typed summary, archived and diffed.

Mission Control is a document Phoenix writes, and this is the only thing the writer may read.
The tests pin the three properties that make it trustworthy: one definition of every number,
provenance on every block, and a diff that reports material change rather than every decimal.
"""
import json, os, sys, types, pathlib, pytest
ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(tmp):
    """Exec the pack functions with OUTPUTS_DIR pointed at a temp dir."""
    src = (ROOT / "phoenix.py").read_text()
    i = src.index("# ============================ THE FACT PACK")
    j = src.index("\ndef backtest_smart_money")
    ns = {"os": os, "json": json, "OUTPUTS_DIR": str(tmp), "_now": lambda: "2026-09-05 06:00 UTC",
          "write_json": lambda n, d: json.dump(d, open(os.path.join(str(tmp), n + ".json"), "w"))}
    exec(src[i:j], ns)
    return ns


def _write(tmp, name, obj):
    os.makedirs(tmp, exist_ok=True)
    json.dump(obj, open(os.path.join(str(tmp), name + ".json"), "w"))


def _seed(tmp, **over):
    _write(tmp, "macro_series", over.get("macro_series", {"series": [
        {"date": "d%d" % i, "spx": 7600 + i, "vix": 15 - i * 0.02, "dxy": 99 + i * 0.01,
         "tnx": 4.70 + i * 0.002, "us02": 4.23, "real10": 2.40 - i * 0.001, "hy": 270 - i,
         "gold": 4400 + i, "wti": 85 + i * 0.2, "btc": 78000 + i * 50, "cpi_yoy": 3.3}
        for i in range(22)]}))
    _write(tmp, "macro", over.get("macro", {"asof": "2026-09-05 05:33 UTC", "regime": "POLICY_TIGHTENING",
                                            "explain": {"held_weeks": 1},
                                            "inputs": {"vix": 14.32, "dxy": 98.98, "us10y": 4.77, "gold": 4539.9, "wti": 91.3}}))
    _write(tmp, "gex", over.get("gex", {"asof": "2026-09-05 05:31 UTC",
                                        "overview": {"spx_spot": 7747.71, "net_gex_B": -2.1, "regime": "negative",
                                                     "flip": 7723, "dist_to_flip_pct": 0.31, "call_wall": 7800, "put_wall": 7700}}))
    _write(tmp, "spx_daily", over.get("spx_daily", {"bars": [{"date": "d%d" % i, "c": 7000 + i * 3} for i in range(219)]
                                                    + [{"date": "d219", "c": 7747.71}]}))
    _write(tmp, "stocks", over.get("stocks", {"asof": "2026-09-05 05:40 UTC", "counts": {"trade": 635},
        "trade_ranked": [{"ticker": "CRNX", "sector": "Health technology", "industry": "Biotechnology",
                          "trade_score": 71, "atr14_pct": 1.64, "mcap_B": 4.2, "breakout": True, "price": 22.5,
                          "days_on_list": 1, "profitability": "profitable"},
                         {"ticker": "DINO", "sector": "Energy minerals", "industry": "Oil refining",
                          "trade_score": 68, "atr14_pct": 2.7, "mcap_B": 8.1, "price": 43.9},
                         {"ticker": "GOOG", "sector": "Technology services", "trade_score": 65, "price": 339.08}],
        "invest_ranked": [{"ticker": "BAC", "sector": "Finance"}]}))
    _write(tmp, "institutional_holdings", over.get("inst", {"asof": "2026-09-04 10:27 UTC", "latest_quarter": "2026-06-30",
        "tickers": {"CRNX": [{"manager": "Appaloosa", "action": "NEW", "value_usd": 3.1e8, "quarter": "2026-06-30"}],
                    "DINO": [{"manager": "Duquesne", "action": "ADD", "value_usd": 1.0e8, "quarter": "2026-06-30"}],
                    "GOOG": [{"manager": "Tiger", "action": "TRIM", "value_usd": 4.0e8, "quarter": "2026-06-30"}],
                    "BAC": [{"manager": "Viking", "action": "HOLD", "value_usd": 9.9e9, "quarter": "2026-06-30"}]}}))
    _write(tmp, "congress_trades", over.get("cong", {"asof": "2026-09-05", "status": {"quiet": [], "absent_chambers": ["Senate"]},
        "tickers": {"CRNX": [{"date": "2026-09-01", "reported": "2026-09-04", "member": "Nancy Pelosi",
                              "side": "buy", "amount": "$250,001 - $500,000"}]}}))
    _write(tmp, "wire", over.get("wire", {"generated": "2026-09-05 06:00 UTC", "accounts": {"gabriel": {
        "date": "2026-09-05", "headline": "Payrolls decide the week",
        "standfirst": "A hot print flips the index into negative gamma.",
        "themes": [{"title": "Rates"}, {"title": "AI capex"}], "ondeck": "CPI 11 Sep"}}}))
    _write(tmp, "trades", over.get("trades", {"balance": 100000, "trades": [
        {"ticker": "GOOG", "status": "open", "entry": 325.86, "stop": 305.67, "qty": 30, "account_size": 100000},
        {"ticker": "BAC", "status": "open", "entry": 64.38, "stop": 60.39, "qty": 150, "account_size": 100000},
        {"ticker": "PLTR", "status": "open", "entry": 184.0, "qty": 10, "account_size": 100000}]}))


def test_macro_block_gives_yields_in_bp_and_names_what_is_absent(tmp_path):
    """A brief that says "yields rose 1.2%" when it means 5bp is unusable. And USDJPY is not
    fetched by Phoenix at all, so it must be named rather than filled in from memory."""
    _seed(tmp_path); ns = _load(tmp_path)
    m = ns["run_fact_pack"]()["macro"]
    assert m["tnx"]["last"] == 4.74 and m["tnx"]["d1_bp"] == 0 or "d1_bp" in m["tnx"]
    assert "d1_bp" in m["hy"] and "d1_pct" not in m["hy"], "spreads move in basis points"
    assert "d1_pct" in m["spx"] and "d1_bp" not in m["spx"], "prices move in percent"
    assert m["curve_2s10s_bp"] == round((m["tnx"]["last"] - m["us02"]["last"]) * 100)
    assert any("usdjpy" in a for a in m["absent"]), "an untracked asset is named, never guessed"
    assert m["rows"] == 22


def test_pack_assembles_every_module(tmp_path):
    _seed(tmp_path); ns = _load(tmp_path)
    p = ns["run_fact_pack"]()
    assert p["schema"] == "phoenix-fact-pack/1" and p["missing"] == []
    m = p["market"]
    assert m["regime"] == "POLICY_TIGHTENING" and m["regime_held_weeks"] == 1
    assert m["spx"]["last"] == 7747.71 and m["spx"]["vs_200d_pct"] is not None
    assert m["gex"]["net_B"] == -2.1 and m["gex"]["flip"] == 7723
    assert p["screener"]["n_candidates"] == 3
    assert p["screener"]["top"][0]["ticker"] == "CRNX"
    assert p["screener"]["top"][0]["stop_pct_2_5atr"] == 4.1, "the stop the policy would use, computed once"
    assert p["news"]["headline"] == "Payrolls decide the week"


def test_heat_has_one_definition_risk_over_one_percent(tmp_path):
    """Two pages divided by different denominators and disagreed by 100x on the same book."""
    _seed(tmp_path); ns = _load(tmp_path)
    p = ns["run_fact_pack"]()["portfolio"]
    assert p["heat_R"] == 1.2, p           # GOOG 0.61R + BAC 0.60R
    assert p["unsized"] == 1 and p["over_cap"] is False
    goog = [r for r in p["open"] if r["ticker"] == "GOOG"][0]
    assert goog["R"] == 0.65 and goog["unsized"] is False
    pltr = [r for r in p["open"] if r["ticker"] == "PLTR"][0]
    assert pltr["unsized"] is True and pltr["R"] is None, "no stop means no R, never a guessed one"


def test_smart_money_nets_by_sector_and_ignores_hold(tmp_path):
    _seed(tmp_path); ns = _load(tmp_path)
    sm = ns["run_fact_pack"]()["smart_money"]
    acc = {x["sector"] for x in sm["accumulating"]}
    dis = {x["sector"] for x in sm["distributing"]}
    assert "Health technology" in acc and "Energy minerals" in acc
    assert "Technology services" in dis
    assert "Finance" not in acc and "Finance" not in dis, "holding is not a decision"
    assert [n["ticker"] for n in sm["notable"]] == ["GOOG", "CRNX"], "largest filings first"
    assert sm["congress_recent"][0]["member"] == "Nancy Pelosi"


def test_missing_inputs_are_named_not_silently_dropped(tmp_path):
    _seed(tmp_path)
    os.remove(os.path.join(str(tmp_path), "gex.json"))
    os.remove(os.path.join(str(tmp_path), "wire.json"))
    ns = _load(tmp_path)
    p = ns["run_fact_pack"]()
    assert "gex.json absent" in p["missing"] and "wire.json absent" in p["missing"]
    assert p["market"]["gex"]["net_B"] is None, "absent is null, never zero"


def test_diff_reports_material_change_only(tmp_path):
    _seed(tmp_path); ns = _load(tmp_path)
    a = ns["run_fact_pack"]()
    assert a["changed"]["first_pack"] is True and a["changed"]["changes"] == []
    # a day later: regime flips, gamma flips, VIX jumps, GOOG closes, a sector reverses
    # backdate yesterday's archive: the filename AND the date inside it, as a real run would have
    old = json.load(open(os.path.join(str(tmp_path), "history", "pack_%s.json" % a["date"])))
    old["date"] = "2026-09-04"
    json.dump(old, open(os.path.join(str(tmp_path), "history", "pack_2026-09-04.json"), "w"))
    os.remove(os.path.join(str(tmp_path), "history", "pack_%s.json" % a["date"]))
    _seed(tmp_path,
          macro={"asof": "x", "regime": "CRISIS_PEAK", "explain": {"held_weeks": 1},
                 "inputs": {"vix": 22.0, "dxy": 99.1, "us10y": 4.79}},
          gex={"asof": "x", "overview": {"spx_spot": 7700, "net_gex_B": 1.4, "regime": "positive", "flip": 7723}},
          inst={"latest_quarter": "2026-06-30", "tickers": {
              "CRNX": [{"manager": "Appaloosa", "action": "EXIT", "value_usd": 3.1e8, "quarter": "2026-06-30"}]}},
          trades={"balance": 100000, "trades": [
              {"ticker": "BAC", "status": "open", "entry": 64.38, "stop": 60.39, "qty": 150, "account_size": 100000}]})
    b = ns["run_fact_pack"]()
    kinds = {(c["kind"], c["what"]) for c in b["changed"]["changes"]}
    assert ("regime", "engine regime") in kinds
    assert ("gex", "index gamma") in kinds
    assert ("market", "vix") in kinds
    assert ("market", "us10y") not in kinds, "a 2bp move is not a change"
    assert ("market", "dxy") not in kinds
    assert ("portfolio", "GOOG") in kinds
    assert ("smart_money", "Health technology") in kinds
    assert b["changed"]["prev_date"] == "2026-09-04"


def test_pack_is_archived_per_day(tmp_path):
    _seed(tmp_path); ns = _load(tmp_path)
    p = ns["run_fact_pack"]()
    hist = os.path.join(str(tmp_path), "history", "pack_%s.json" % p["date"])
    assert os.path.exists(hist), "yesterday's pack is what makes 'what changed' computable"
    assert os.path.exists(os.path.join(str(tmp_path), "fact_pack.json"))
    json.dumps(p, allow_nan=False)


def test_provenance_records_age_of_every_block(tmp_path):
    _seed(tmp_path); ns = _load(tmp_path)
    pr = ns["run_fact_pack"]()["provenance"]
    assert set(pr) >= {"macro", "gex", "stocks", "institutional_holdings", "congress_trades", "wire"}
    assert pr["institutional_holdings"]["quarter"] == "2026-06-30"
    assert pr["congress_trades"]["status"]["absent_chambers"] == ["Senate"], \
        "a quiet feed must reach the writer as a fact, not as silence"
