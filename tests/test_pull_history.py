import sys, os, json; sys.path.insert(0, os.path.dirname(os.path.dirname(__file__))); sys.path.insert(0, os.path.dirname(__file__))
import pandas as pd

def test_pull_end_to_end_with_modern_yfinance_shapes(tmp_path, monkeypatch):
    import fake_yf; sys.modules["yfinance"] = fake_yf              # modern MultiIndex shapes, zero print, NaN gap
    monkeypatch.chdir(tmp_path)
    pd.DataFrame({"ticker": ["AAPL", "ZERO", "GAPPY", "ENEL.MI", "NESN.SW", "HSBA.L"]}).to_csv("universe.csv", index=False)
    import importlib, pull_history as P; importlib.reload(P)
    sys.argv = ["pull_history.py", "--universe", "universe.csv", "--years", "3", "--limit", "6", "--dry"]; P.main()
    df = pd.read_csv("prices_history.csv") if os.path.exists("prices_history.csv") else pd.read_parquet("prices_history.parquet")
    assert df.close_eur.notna().all() and (df.close_eur > 0).all()
    assert df.ret.abs().max() < 20                                   # zero print no longer produces inf
    assert set(df.ccy) == {"USD", "EUR", "CHF"}                      # .SW is CHF, not EUR
    assert df[df.ticker == "NESN.SW"].ccy.iloc[0] == "CHF"
    assert "HSBA.L" in pd.read_csv("pull_failed.csv").symbol.tolist()   # pence trap: skipped, not mis-converted
    assert df.month.str.match(r"^\d{4}-\d{2}-01$").all()
    json.dumps(df.astype(object).where(df.notna(), None).to_dict("records"), allow_nan=False)
    for t in ("VWCE", "AGGH", "SGLD", "BTCE"): assert t in set(df.ticker)
