"""MME forward test engine: the rule functions against the worked examples in MME Trading Rules v1.4.3/v1.5.0, one
decide -> morning cycle on fake Phoenix data, and the v1.5.0 hourly entry checks. Run: python -m pytest tests/test_mme_ft.py -q"""
import os, sys
from datetime import date, datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trading import mme_ft as M

R = M.load_rules()
R143 = M.load_rules(os.path.join(M.ROOT, "controls", "mme_rules_v1_4_3.json"))


def test_loads_v150_first():
    assert R["version"] == "1.5.0" and R["approved"] and M.recheck_above_band(R) and M.hourly_checks(R) == [11, 12, 13, 14, 15]
    assert R143["version"] == "1.4.3" and not M.recheck_above_band(R143) and M.hourly_checks(R143) == []


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


def test_index_rows_are_not_tradeable(tmp_path, monkeypatch):
    import json
    m = {"date": "2026-11-16", "groups": {"green": [{"ticker": "AAA"}, {"ticker": "SPX", "index": True},
                                                    {"ticker": "JPM", "watch": True}, {"ticker": "XOM", "watch": False}],
                                          "amber": [], "red": []}}
    monkeypatch.setattr(M, "_j", lambda name: m if name == "mme_map.json" else None)
    rows, d, _ = M.Data().map()
    assert "AAA" in rows and "SPX" not in rows
    assert "JPM" not in rows and "XOM" in rows            # Sector Top 5: watch rows stay out until they join


def test_second_morning_run_leaves_todays_orders_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "OUT", str(tmp_path))
    d1, d2 = date(2026, 11, 23), date(2026, 11, 24)
    rows = {"AAA": {"ticker": "AAA", "em": 4.0, "em_src": "atm_iv", "flip": 190.0, "support": 200.0, "resist": 206.0,
                    "support_tiered": [200.0, 192.0], "stale_days": 0}}
    S = {d1.isoformat(): {"SPX": [(_ts(d1, 15, 0), 7600.0)], "VIX": [(_ts(d1, 15, 0), 16.0)], "AAA": [(_ts(d1, 15, 0), 201.5)]}}
    F = Fake(d1, S, rows)
    M.morning(d1, R143, F)                                  # the 06:00 review
    M.decide(d1, R143, F)                                   # 10:00 ET: limit orders placed today (v1.4.3 only)
    pend = [t for t in F.trs if t["status"] == "pending"]
    assert pend
    out = M.morning(d1, R143, F)                            # Phoenix run again in the evening
    assert out.get("skipped")
    F.st.pop("morning_done")                             # even without the guard, today's orders are not touched
    M.morning(d1, R143, F)
    assert all(t["status"] == "pending" for t in F.trs if t["id"] in {p["id"] for p in pend})


def test_quiet_days_are_pushed(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "OUT", str(tmp_path))
    sent = []
    monkeypatch.setattr(M, "notify", lambda *a, **k: sent.append(a) or True)
    d1, d2 = date(2026, 11, 23), date(2026, 11, 24)
    S = {d1.isoformat(): {"SPX": [(_ts(d1, 15, 0), 7600.0)], "VIX": [(_ts(d1, 15, 0), 16.0)]}}
    F = Fake(d1, S, {})                                             # nothing on the map qualifies
    M.decide(d1, R, F)
    assert sent[0][0] == "MME test" and sent[0][1] == ["No orders today"]
    F.T = d2
    M.morning(d2 + timedelta(days=1), R, F)
    assert sent[1][0] == "MME test" and sent[1][1] == ["No fills, stops or exits"]


ROWS = {"AAA": {"ticker": "AAA", "em": 4.0, "em_src": "atm_iv", "flip": 190.0, "support": 200.0, "resist": 206.0,
                "support_tiered": [200.0, 192.0], "stale_days": 0}}


def test_v150_above_band_waits_then_hourly_check_enters(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "OUT", str(tmp_path))
    sent = []
    monkeypatch.setattr(M, "notify", lambda *a, **k: sent.append(a) or True)
    d1 = date(2026, 11, 23)
    S = {d1.isoformat(): {"SPX": [(_ts(d1, 15, 0), 7600.0)], "VIX": [(_ts(d1, 15, 0), 16.0)],
                          "AAA": [(_ts(d1, 15, 0), 201.5), (_ts(d1, 16, 0), 200.6), (_ts(d1, 17, 0), 200.4)]}}
    F = Fake(d1, S, ROWS)
    out = M.decide(d1, R, F)                                    # 10:00 ET: 201.5 is above the 200-201 band
    f = {e["ticker"]: e for e in out["funnel"]}
    assert f["AAA"]["blocked"] == "R07" and f["AAA"]["band"].startswith("above the band: wait") and F.trs == []
    assert sent[-1][1] == ["No orders today"]
    out = M.check(d1, R, F, at=_ts(d1, 16, 5))                 # 11:05 ET: back inside the band -> a new setup
    assert sorted({t["arm"] for t in F.trs}) == ["A", "B", "C", "D", "G", "H"] and all(t["status"] == "open" for t in F.trs)
    a2 = [t for t in F.trs if t["arm"] == "A" and t["acct"] == 2000][0]
    assert a2["fill"] == round(200.6 * 1.0005, 4) and a2["stop"] == 196.0 and a2["entry"]["check"] == "11:00 ET"
    assert a2["entry"]["next_wall"] == 206.0
    title, lines = sent[-1][0], sent[-1][1]
    assert "11:00 ET" in title and "1 new order" in title
    assert lines[0].startswith("AAA \u00b7 entry 200.70 \u00b7 SL 196.00 \u00b7 TP next wall 206.00 \u00b7 +1R 205.40")
    n = len(F.trs); k = len(sent)
    M.check(d1, R, F, at=_ts(d1, 17, 5))                       # 12:05 ET: still in the band, signal in force: no repeat
    assert len(F.trs) == n and len(sent) == k
    assert M.check(d1, R, F, at=_ts(d1, 17, 6)).get("skipped")   # the same hour twice: once only


def test_v150_checks_only_inside_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "OUT", str(tmp_path))
    d1 = date(2026, 11, 23)
    F = Fake(d1, {}, {})
    assert M.check(d1, R, F, at=_ts(d1, 15, 5)).get("skipped")   # 10:05 ET belongs to decide
    assert M.check(d1, R, F, at=_ts(d1, 21, 5)).get("skipped")   # 16:05 ET: after the last check
    assert M.check(d1, R143, F, at=_ts(d1, 17, 5)).get("skipped")  # v1.4.3 has no hourly entries


def test_v150_r15_reserves_cash_of_todays_signals(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "OUT", str(tmp_path))
    monkeypatch.setattr(M, "notify", lambda *a, **k: True)
    d1 = date(2026, 11, 23)
    rows = dict(ROWS)
    rows["CCC"] = dict(ROWS["AAA"], ticker="CCC")
    S = {d1.isoformat(): {"SPX": [(_ts(d1, 15, 0), 7600.0)], "VIX": [(_ts(d1, 15, 0), 16.0)],
                          "AAA": [(_ts(d1, 15, 0), 200.6)], "CCC": [(_ts(d1, 15, 0), 203.0), (_ts(d1, 17, 0), 200.6)],
                          "DDD": []}}
    rows["DDD"] = dict(ROWS["AAA"], ticker="DDD")
    S[d1.isoformat()]["DDD"] = [(_ts(d1, 15, 0), 203.0), (_ts(d1, 18, 0), 200.6)]
    class F3(Fake):
        def chains(self):
            ch, x = Fake.chains(self)
            ch["CCC"] = ch["DDD"] = ch["AAA"]
            return ch, x
    F = F3(d1, S, rows)
    M.decide(d1, R, F)                                          # AAA at 10:00: $683 of $2,000 in arm A
    M.check(d1, R, F, at=_ts(d1, 17, 5))                       # CCC at 12:00: another $683
    M.check(d1, R, F, at=_ts(d1, 18, 5))                       # DDD at 13:00: 35% cap would be $700, room is ~$633
    a = {t["ticker"]: t for t in F.trs if t["arm"] == "A" and t["acct"] == 2000}
    used = sum(t["qty"] * t["fill"] for t in a.values())
    assert set(a) == {"AAA", "CCC", "DDD"} and a["DDD"]["qty"] < a["AAA"]["qty"] and used <= 2000


def test_v150_spx_warning_once_per_change(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "OUT", str(tmp_path))
    sent = []
    monkeypatch.setattr(M, "notify", lambda *a, **k: sent.append(a) or True)
    d1 = date(2026, 11, 23)
    S = {d1.isoformat(): {"SPX": [(_ts(d1, 15, 0), 7600.0), (_ts(d1, 16, 0), 7420.0), (_ts(d1, 17, 0), 7425.0),
                                  (_ts(d1, 18, 0), 7380.0), (_ts(d1, 19, 0), 7600.0)],
                          "VIX": [(_ts(d1, 15, 0), 16.0)], "AAA": [(_ts(d1, 15, 0), 200.6), (_ts(d1, 16, 0), 230.0)]}}
    F = Fake(d1, S, ROWS)
    M.decide(d1, R, F); k = len(sent)
    M.check(d1, R, F, at=_ts(d1, 16, 5))                       # 11:00: SPX 7420, flip 7400, EM ~53 -> AMBER
    assert len(sent) == k + 1 and "AMBER warning" in sent[-1][0] and "AAA" in sent[-1][1][0]
    M.check(d1, R, F, at=_ts(d1, 17, 5))                       # 12:00: still AMBER: no repeat
    assert len(sent) == k + 1
    M.check(d1, R, F, at=_ts(d1, 18, 5))                       # 13:00: RED is a new warning
    assert len(sent) == k + 2 and "RED warning" in sent[-1][0]
    M.check(d1, R, F, at=_ts(d1, 19, 5))                       # 14:00: GREEN again: silent, record cleared
    assert len(sent) == k + 2 and F.st["spx_warned"] == d1.isoformat() + ":"


def test_v150_quiet_check_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "OUT", str(tmp_path))
    sent = []
    monkeypatch.setattr(M, "notify", lambda *a, **k: sent.append(a) or True)
    d1 = date(2026, 11, 23)
    S = {d1.isoformat(): {"SPX": [(_ts(d1, 17, 0), 7600.0)], "VIX": [(_ts(d1, 17, 0), 16.0)]}}
    F = Fake(d1, S, {})
    M.check(d1, R, F, at=_ts(d1, 17, 5))
    assert sent == [] and (d1, "check_12") in F.dayrows


# ------------------------------------------------------------------ S5 v1.0 (frozen 24 Nov 2026), its own arm

def _bars(upto, n=30, extra=None):
    """n flat daily bars before `upto` (high - low = 1, so ATR(14) = 1.0), plus optional {iso: bar} overrides."""
    out, d = {}, upto
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() != "2026-11-26":
            out[d.isoformat()] = {"o": 200.0, "h": 200.5, "l": 199.5, "c": 200.0}
    out.update(extra or {})
    return out


class FakeS5(Fake):
    def __init__(self, today, snaps, rows, bars):
        Fake.__init__(self, today, snaps, rows); self.B = bars
    def bars(self, tk): return self.B


def test_s5_loads_as_its_own_arm():
    assert "S5" in R["arms"] and R["arms"]["S5"]["s5"] and R["arms"]["S5"]["start"] == "2026-11-30"
    assert "S5" in R["regime_cuts"]["exempt_arms"] and R["s5"]["version"] == "1.0"
    assert "S5" not in R143["arms"]                                  # an explicit rule file stays as written
    assert M.arm_live(date(2026, 11, 27), R, "S5") is False and M.arm_live(date(2026, 11, 30), R, "S5") is True


def test_s5_wilder_atr_and_stop_review():
    b = _bars(date(2026, 11, 30))
    assert abs(M.wilder_atr(b, "2026-11-27") - 1.0) < 1e-9
    assert M.wilder_atr({}, "2026-11-27") is None
    assert M.wilder_atr(b, "2026-11-30") is None                                  # bars stop on 27 Nov: stale, no ATR
    cfg = R["arms"]["S5"]; W = [192, 200, 206, 210, 224]
    assert M.s5_stop(W, 200, 201.5, 202, 4, 1.0, cfg, R) == (None, None)        # only 200 held: that is the entry wall
    assert M.s5_stop(W, 200, 207.5, 208, 4, 1.0, cfg, R) == (204.0, "R10")      # 206 held: 206 - 2 ATR
    assert M.s5_stop(W, 200, 212.0, 213, 4, 1.0, cfg, R) == (211.0, "R11")      # 210-224 gap 3.5 EM: high 213 - 2 ATR


def test_s5_account_steps_only_when_flat():
    cfg = R["arms"]["S5"]
    closed = [{"arm": "S5", "acct": 2000, "status": "closed", "pnl": 1500.0, "dry": False}]
    assert M.s5_account(closed, "S5", 2000, cfg, False) == 3000.0
    assert M.s5_account(closed + [{"arm": "S5", "acct": 2000, "status": "closed", "pnl": -800.0, "dry": False}], "S5", 2000, cfg, False) == 2000.0
    opn = [{"arm": "S5", "acct": 2000, "status": "open", "dry": False, "entry": {"acct_size": 2000.0}}]
    assert M.s5_account(closed + opn, "S5", 2000, cfg, False) == 2000.0          # not flat: the size stamped at entry holds


def _s5_setup(d1):
    S = {d1.isoformat(): {"SPX": [(_ts(d1, 15, 0), 7600.0)], "VIX": [(_ts(d1, 15, 0), 16.0)], "AAA": [(_ts(d1, 15, 0), 200.8)]}}
    return S, FakeS5(d1, S, dict(ROWS), _bars(d1))


def test_s5_entry_stop_and_target(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "OUT", str(tmp_path))
    sent = []
    monkeypatch.setattr(M, "notify", lambda *a, **k: sent.append(a) or True)
    d1 = date(2026, 11, 30)
    S, F = _s5_setup(d1)
    M.decide(d1, R, F)
    s5 = [t for t in F.trs if t["arm"] == "S5"]
    assert sorted(t["acct"] for t in s5) == [2000, 10000] and all(t["dry"] is False for t in s5)
    t = [x for x in s5 if x["acct"] == 2000][0]; fill = round(200.8 * 1.0005, 4)
    assert t["fill"] == fill and t["stop"] == 198.0 and t["entry"]["tp"] == round(fill + 2 * (fill - 198.0), 4)
    assert t["entry"]["acct_size"] == 2000.0 and t["entry"]["atr"] == 1.0
    a = [x for x in F.trs if x["arm"] == "A" and x["acct"] == 2000][0]
    assert a["stop"] == 196.0                                         # the MME arms are unchanged
    assert any(l.startswith("S5 · AAA · entry 200.90 · SL 198.00") and "TP 2R 206.70" in l for l in sent[-1][1])


def test_s5_take_profit_then_no_regime_cut(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "OUT", str(tmp_path))
    sent = []
    monkeypatch.setattr(M, "notify", lambda *a, **k: sent.append(a) or True)
    d1, d2, d3 = date(2026, 11, 30), date(2026, 12, 1), date(2026, 12, 2)
    S, F = _s5_setup(d1)
    M.decide(d1, R, F)
    # 1 Dec: SPX turns RED at 10:00 ET: arm A is closed (R01), S5 holds; then AAA trades through the 2R target
    S[d2.isoformat()] = {"SPX": [(_ts(d2, 15, 0), 7300.0)], "VIX": [(_ts(d2, 15, 0), 20.0)],
                         "AAA": [(_ts(d2, 14, 30), 203.0), (_ts(d2, 15, 0), 203.5), (_ts(d2, 17, 0), 207.0), (_ts(d2, 18, 0), 196.0)]}
    F.T = d2
    M.morning(d2, R, F)                                               # reviews 30 Nov: nothing hit
    out = M.decide(d2, R, F)
    assert out["spx"]["colour"] == "RED" and not any(c.startswith("S5/") for c in out["cuts"])
    assert [x for x in F.trs if x["arm"] == "A" and x["acct"] == 2000][0]["status"] == "closed"
    F.B = _bars(d3, extra={d2.isoformat(): {"o": 203.0, "h": 207.0, "l": 196.0, "c": 197.0}})
    F.T = d3
    M.morning(d3, R, F)                                               # 1 Dec: 207 reached the target before 196
    t = [x for x in F.trs if x["arm"] == "S5" and x["acct"] == 2000][0]
    assert t["status"] == "closed" and t["exits"][0]["px"] == t["entry"]["tp"] and t["exit_reason"].startswith("take-profit 2R")
    assert 1.85 < t["r"] < 2.0                                        # 2R less commissions
    assert any("take-profit 2R" in l and "arms S5" in l for l in sent[-1][1])
    eq = F.dayrows[(d3, "morning")]["equity"]["S5"]["2000"]
    assert eq["n_closed"] == 1 and eq["realised"] > 0


def test_s5_not_before_its_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "OUT", str(tmp_path))
    monkeypatch.setattr(M, "notify", lambda *a, **k: True)
    d0 = date(2026, 11, 24)
    S = {d0.isoformat(): {"SPX": [(_ts(d0, 15, 0), 7600.0)], "VIX": [(_ts(d0, 15, 0), 16.0)], "AAA": [(_ts(d0, 15, 0), 200.8)]}}
    F = FakeS5(d0, S, dict(ROWS), _bars(d0))
    M.decide(d0, R, F)
    assert not [t for t in F.trs if t["arm"] == "S5"] and [t for t in F.trs if t["arm"] == "B"]
    d1 = date(2026, 11, 25)                                           # dry run from 25 Nov: rows written, marked dry
    S = {d1.isoformat(): {"SPX": [(_ts(d1, 15, 0), 7600.0)], "VIX": [(_ts(d1, 15, 0), 16.0)], "AAA": [(_ts(d1, 15, 0), 200.8)]}}
    F = FakeS5(d1, S, dict(ROWS), _bars(d1))
    M.decide(d1, R, F)
    assert all(t["dry"] for t in F.trs if t["arm"] == "S5") and [t for t in F.trs if t["arm"] == "S5"]
