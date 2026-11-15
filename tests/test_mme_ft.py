"""MME forward test engine: the rule functions against the worked examples in MME Trading Rules v1.4.3, and one
decide -> morning cycle on fake Phoenix data. Run: python -m pytest tests/test_mme_ft.py -q"""
import os, sys
from datetime import date, datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trading import mme_ft as M

R = M.load_rules()


def test_spx_em_example():                       # v1.4.3 section 5: SPX 7,619, VIX 17.5 -> about 59 points
    assert round(M.spx_em(7619, 17.5, R)) == 59


def test_example_b_sizing_and_account_table():   # paid $201, stop $196, 1% tier
    for acct, qty, cost, risk in ((2000, 3.4, 683.40, 17.00), (5000, 8.7, 1748.70, 43.50), (10000, 17.4, 3497.40, 87.00)):
        s = M.size_position(201, 196, 4, acct, 0, R)
        assert s["qty"] == qty and s["cost"] == cost and s["risk_usd"] == risk and s["tier_pct"] == 1.0


def test_tiers_and_em_cap():
    assert M.size_position(201, 196, 4, 10000, 1, R)["tier_pct"] == 1.5
    assert M.size_position(201, 196, 4, 10000, 3, R)["tier_pct"] == 2.0
    assert M.size_position(201, 196, 9, 10000, 3, R)["tier_pct"] == 1.0      # EM above 4% of the price stays at 1%
    s = M.size_position(201, 171, 30, 2000, 0, R)                              # one share risks $30 > $20: next tier
    assert s["tier_pct"] == 1.5
    assert M.size_position(201, 150, 4, 2000, 0, R)["qty"] == 0                # $51 per share > 2%: no order
    assert M.size_position(201, 196, 4, 2000, 0, R, cost_open=1900)["qty"] == 0.4   # R15 room $100


def test_example_c_and_d_stop_climb():           # EM $4, walls 192 200 206 210 224, first stop 196
    W = [192, 200, 206, 210, 224]
    s, r = M.review_stop(196, W, 205, 206, 4, R)
    assert (s, r) == (196, None)                  # day 2: 205 has not held 206 + 1
    s, r = M.review_stop(s, W, 208, 209, 4, R)
    assert (s, r) == (202, "R10")                 # day 3: R10, 206 - 4
    s, r = M.review_stop(s, W, 211.5, 213, 4, R)
    assert (s, r) == (209, "R11")                 # day 4: gap 210-224 = 3.5 EM: high 213 - 4 = 209 beats R10's 206
    s, r = M.review_stop(s, W, 221, 222, 4, R)
    assert (s, r) == (218, "R11")                 # example D gap up: 222 - 4
    s2, _ = M.review_stop(218, W, 205, 206, 4, R)
    assert s2 == 218                              # R14: never lowered


def test_t1_trailing():
    assert M.t1_stop(201, 196, 205.9, 4, R) is None
    assert M.t1_stop(201, 196, 206, 4, R) == 202  # +1R reached: highest high 206 - 1 EM


def test_calendar_r06_and_holidays():
    assert not M.r06_blocked(date(2026, 11, 16), R)
    assert all(M.r06_blocked(date(2026, 11, d), R) for d in (17, 18, 19, 20))
    assert M.r06_blocked(date(2026, 12, 15), R) and not M.r06_blocked(date(2026, 12, 14), R)
    assert not M.is_trading_day(date(2026, 11, 26), R)
    assert M.prev_trading_day(date(2026, 11, 30), R) == date(2026, 11, 27)
    assert M.add_trading_days(date(2026, 11, 23), 5, R) == date(2026, 12, 1)


def test_commission():
    assert M.commission(3.4, "buy", R) == 1.00 and M.commission(3.4, "sell", R) == 1.02
    assert M.commission(400, "buy", R) == 2.00


def test_major_walls():
    rows = [["2026-12-18", "P", 95, 1000, .3, .02], ["2026-12-18", "P", 90, 200, .3, .02], ["2026-12-18", "C", 105, 900, .3, .02],
            ["2026-12-18", "C", 110, 100, .3, .02], ["2026-12-18", "C", 140, 5000, .3, .02]]
    w = M.major_walls(rows, 100, 97.5, 102.5, R)
    # each side against its own maximum, taken over the whole chain before the 25% cut (the method's order): the call at
    # 140 sets the call maximum, so 105 and 110 fall below 35% of it; 140 itself is then dropped as more than 25% away
    assert w == [95.0, 97.5, 102.5]
    assert M.closest3_spacing(w, 100) == 2.5


# ------------------------------------------------------------------ one cycle on fake data

class Fake(M.Data):
    def __init__(self, today, snaps, rows):
        self.T, self.S, self.rows = today, snaps, rows
        self.trs, self.dayrows, self.st, self.nid = [], {}, {}, 1
    def map(self): return self.rows, self.T.isoformat(), "x"          # Phoenix dates the map by the morning run
    def chains(self):
        ch = {"AAA": [["2026-12-18", "P", 200, 5000, .3, .02], ["2026-12-18", "C", 206, 4000, .3, .02], ["2026-12-18", "C", 210, 4000, .3, .02],
                      ["2026-12-18", "P", 192, 4000, .3, .02], ["2026-12-18", "C", 224, 4000, .3, .02]],
              "BBB": [["2026-12-18", "P", 95, 5000, .3, .02], ["2026-12-18", "C", 105, 5000, .3, .02], ["2026-12-18", "C", 110, 4000, .3, .02]]}
        return ch, "x"
    def spx_flip(self): return 7400.0, "x"
    def earnings(self, tk): return "2027-02-01", False
    def bars(self, tk): return {}
    def commit(self): return "test"
    def snaps(self, tickers, day): return {k: v for k, v in self.S.get(day.isoformat(), {}).items() if k in tickers}
    def trades(self, st=("open", "pending")): return [dict(t) for t in self.trs if t["status"] in st]
    def all_trades(self): return [dict(t) for t in self.trs]
    def insert_trade(self, row):
        row = dict(row); row["id"] = self.nid; self.nid += 1; self.trs.append(row); return row
    def update_trade(self, tid, patch):
        for t in self.trs:
            if t["id"] == tid: t.update(patch)
    def put_day(self, d, kind, data, dry): self.dayrows[(d, kind)] = data
    def get_day(self, d, kind): return self.dayrows.get((d, kind))
    def state(self, k, default=None): return self.st.get(k, default)
    def set_state(self, k, v): self.st[k] = v


def _ts(day, hh, mm):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc)


def test_r00_map_date_is_the_morning_run(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "OUT", str(tmp_path))
    d = date(2026, 11, 16)
    class Stale(Fake):
        def map(self): return self.rows, "2026-11-13", "x"         # Friday's run = Thursday's close: stale on Monday
    S = {d.isoformat(): {"SPX": [(_ts(d, 15, 0), 7600.0)], "VIX": [(_ts(d, 15, 0), 16.0)]}}
    out = M.decide(d, R, Stale(d, S, {}))
    assert out["data_ok"] is False
    out = M.decide(d, R, Fake(d, S, {}))
    assert out["data_ok"] is True


def test_decide_then_morning_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "OUT", str(tmp_path))
    d1, d2 = date(2026, 11, 23), date(2026, 11, 24)            # 10:00 ET = 15:00 UTC in November
    rows = {"AAA": {"ticker": "AAA", "em": 4.0, "em_src": "atm_iv", "flip": 190.0, "support": 200.0, "resist": 206.0,
                    "support_tiered": [200.0, 192.0], "stale_days": 0},
            "BBB": {"ticker": "BBB", "em": 2.0, "em_src": "atm_iv", "flip": 99.0, "support": 95.0, "resist": 105.0,
                    "support_tiered": [95.0], "stale_days": 0}}
    S = {d1.isoformat(): {"SPX": [(_ts(d1, 15, 0), 7600.0)], "VIX": [(_ts(d1, 15, 0), 16.0)],
                          "AAA": [(_ts(d1, 14, 30), 200.5), (_ts(d1, 15, 0), 200.8), (_ts(d1, 18, 0), 203.0)],
                          "BBB": [(_ts(d1, 15, 0), 100.6)]},
         d2.isoformat(): {"AAA": [(_ts(d2, 14, 30), 202.0), (_ts(d2, 16, 0), 196.4), (_ts(d2, 17, 0), 195.9)]}}
    F = Fake(d1, S, rows)
    out = M.decide(d1, R, F, now=None)
    assert out["spx"]["colour"] == "GREEN"
    f = {e["ticker"]: e for e in out["funnel"]}
    assert f["AAA"]["blocked"] is None and f["AAA"]["band"].startswith("in the band")
    assert f["BBB"]["blocked"] in ("R03", "R04")
    arms = sorted({t["arm"] for t in F.trs if t["ticker"] == "AAA"})
    assert arms == ["A", "B", "C", "D", "G", "H"]                 # same entry in every wall arm; E has no partner here
    a2 = [t for t in F.trs if t["arm"] == "A" and t["acct"] == 2000][0]
    assert a2["stop"] == 196.0 and a2["qty"] == 3.4 and a2["dry"] is (not R["approved"])   # live from the start date once approved
    # next morning: the 196 stop was touched on day 2 -> closed about -1R after costs
    F.T = d2
    M.morning(d2 + timedelta(days=1), R, F)                     # reviews the session of 24 Nov
    a2 = [t for t in F.trs if t["arm"] == "A" and t["acct"] == 2000][0]
    assert a2["status"] == "closed" and a2["r"] < -0.95
    assert F.dayrows[(d2 + timedelta(days=1), "morning")]["equity"]["A"]["2000"]["n_closed"] == 1


def test_push_messages_group_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "OUT", str(tmp_path))
    sent = []
    monkeypatch.setattr(M, "notify", lambda *a, **k: sent.append(a) or True)
    d1, d2 = date(2026, 11, 23), date(2026, 11, 24)
    rows = {"AAA": {"ticker": "AAA", "em": 4.0, "em_src": "atm_iv", "flip": 190.0, "support": 200.0, "resist": 206.0,
                    "support_tiered": [200.0, 192.0], "stale_days": 0}}
    S = {d1.isoformat(): {"SPX": [(_ts(d1, 15, 0), 7600.0)], "VIX": [(_ts(d1, 15, 0), 16.0)], "AAA": [(_ts(d1, 15, 0), 200.8)]},
         d2.isoformat(): {"AAA": [(_ts(d2, 14, 30), 199.0), (_ts(d2, 17, 0), 195.9)]}}
    F = Fake(d1, S, rows)
    M.decide(d1, R, F)
    title, lines = sent[0][0], sent[0][1]
    assert "1 new order" in title and lines[0].startswith("AAA") and "arms A B C D G H" in lines[0]   # one line, not 12
    F.T = d2
    M.morning(d2 + timedelta(days=1), R, F)
    title, lines = sent[1][0], sent[1][1]
    assert "1 closed" in title and "stopped out" in lines[0] and "R (" in lines[0]


def test_notify_queues_web_push(monkeypatch):
    import requests
    calls = []
    class R: status_code = 201; text = ""
    monkeypatch.setattr(requests, "post", lambda u, **k: calls.append((u, k)) or R())
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co"); monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.delenv("MME_NTFY_TOPIC", raising=False); monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert M.notify("MME test · 1 new order", ["AAA · bought 200.80"]) is True
    assert len(calls) == 1 and calls[0][0].endswith("/rest/v1/push_outbox")
    assert calls[0][1]["json"]["url"] == "/?pane=mmetest#mission" and "AAA" in calls[0][1]["json"]["body"]
