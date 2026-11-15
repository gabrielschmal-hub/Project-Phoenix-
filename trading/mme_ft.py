"""
MME forward test (FT-1.3) — Phoenix 2.0, Trading module.

The strategy "MME walls, long only" under MME Trading Rules v1.4.3, run as a forward test with seven arms
(A, B, C, D, E, G, H) on two simulated accounts ($2,000 and $10,000). Everything comes from Phoenix:
  * levels (walls, flip, EM): outputs/mme_map.json and outputs/mme_chains.json (CBOE delayed chains), the
    morning run's commit
  * SPX flip: outputs/gex.json;  SPX, VIX and every name at 10:00 ET and every 5 minutes: Supabase prices_intraday
  * daily bars: outputs/charts/<TK>.json;  earnings: outputs/earnings_state.json, outputs/financials_all.json
The rules are not in this file: they are the Controls rule set controls/mme_rules_v1.4.3.json. This file only
applies them. Results go to Supabase (mme_ft_trades, mme_ft_days, mme_ft_state) and a contract file
outputs/mme_ft.json (schema_version, asof, source).

Two entry points:
  morning(today)  — in the daily run, after mme_map: settle the previous session (limit fills, stop touches from
                    the 5-minute snapshots), then R10, R11, T1 with today's walls and today's EM, the EM log, marks,
                    equity per arm and account.
  decide(today)   — the 10:00 ET job: data check, SPX colour, R01 cuts on open trades, every rule for every name
                    (the funnel), entries for every arm.
Until the rule set says approved=true, and before its start date, every row is written with dry=true.
"""
import json, math, os, random
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                      # pragma: no cover
    ET = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RULES_PATH = os.path.join(ROOT, "controls", "mme_rules_v1.4.3.json")
OUT = os.environ.get("PHOENIX_OUTPUTS", os.path.join(ROOT, "outputs"))
SCHEMA = 1


# ============================================================ rules (pure functions, unit-tested)

def load_rules(path=RULES_PATH):
    return json.load(open(path))


def floor_step(x, step=0.1):
    return math.floor(x / step + 1e-9) * step


def spx_em(spx, vix, R):
    """v1.4.3 section 5: SPX x VIX/100 / 16 x 0.71 (vol-adjusted)."""
    return spx * vix / 100.0 / R["spx"]["em_div"] * R["spx"]["em_scale"]


def spx_colour(spx, flip, em, R):
    if spx is None or flip is None or not em:
        return None, None
    d = (spx - flip) / em
    if d < 0:
        return "RED", d
    return ("AMBER" if d <= R["spx"]["amber_em"] else "GREEN"), d


def major_walls(rows, price, support=None, resist=None, R=None):
    """MME Walls Indicator method: gamma x OI per strike summed across expiries, calls and puts separately; a strike is
    kept if either side is >= 35% of that side's maximum; strikes more than 25% from the price are dropped; the Phoenix
    put and call walls are always added. rows: [expiry, cp, K, oi, iv, gamma, ...]."""
    share = (R or {}).get("walls", {}).get("min_share", 0.35)
    maxd = (R or {}).get("walls", {}).get("max_dist", 0.25)
    c, p = {}, {}
    for r in rows or []:
        try:
            K, oi, g = float(r[2]), float(r[3]), float(r[5])
        except Exception:
            continue
        v = abs(g) * oi
        (c if r[1] == "C" else p)[K] = (c if r[1] == "C" else p).get(K, 0.0) + v
    mc, mp = max(c.values() or [0]), max(p.values() or [0])
    keep = set()
    for K in set(c) | set(p):
        if abs(K / price - 1) > maxd:
            continue
        if (mc and c.get(K, 0) >= share * mc) or (mp and p.get(K, 0) >= share * mp):
            keep.add(K)
    for w in (support, resist):
        if w:
            keep.add(float(w))
    return sorted(keep)


def closest3_spacing(walls, price):
    """R04: the smallest distance between the 3 walls closest to the price (None if fewer than 3)."""
    if not walls or len(walls) < 3:
        return None
    near = sorted(sorted(walls, key=lambda w: abs(w - price))[:3])
    return min(near[1] - near[0], near[2] - near[1])


def size_position(fill, stop, em, acct, points, R, cost_open=0.0, risk_floor=None):
    """R09 + R15. Returns dict(qty, tier_pct, risk_usd, cost) or dict(qty=0, why=...)."""
    tiers = R["risk"]["tiers_pct"]
    idx = 0 if points <= 0 else (1 if points == 1 else 2)
    if em / fill * 100 > R["risk"]["em_pct_cap_for_1pct"]:
        idx = 0
    rps = fill - stop
    if rps <= 0:
        return {"qty": 0, "why": "stop not below the fill"}
    while idx < len(tiers) and rps > acct * tiers[idx] / 100.0:          # one share risks more than the tier
        idx += 1
    if idx >= len(tiers):
        return {"qty": 0, "why": "one share risks more than 2% of the account"}
    tier = tiers[idx]
    qty = floor_step(acct * tier / 100.0 / rps, R["risk"]["qty_step"])
    cap_pos = acct * R["risk"]["position_cap_pct"] / 100.0
    room = acct * R["risk"]["total_cap_pct"] / 100.0 - cost_open
    lim = min(cap_pos, room)
    if qty * fill > lim:
        qty = floor_step(max(lim, 0) / fill, R["risk"]["qty_step"])
    qty = round(qty, 1)
    if qty < R["risk"]["qty_step"]:
        return {"qty": 0, "why": "no room under the caps (R15)" if lim < fill * 0.1 else "below 0.1 share"}
    risk = round(qty * rps, 2)
    if risk_floor and risk < risk_floor:
        return {"qty": 0, "why": f"initial risk ${risk:.2f} below ${risk_floor}"}
    return {"qty": qty, "tier_pct": tier, "risk_usd": risk, "cost": round(qty * fill, 2)}


def commission(qty, side, R):
    c = R["costs"]
    base = c["buy"] if side == "buy" else c["sell"]
    return round(max(base, qty * c["per_share_above_200"] + (base - 1.0)) if qty > 200 else base, 2)


def r10_stop(walls, prev_close, em, R):
    """R10: the highest wall the previous close holds (close >= wall + 0.25 EM), minus 1 EM."""
    held = [w for w in walls if prev_close >= w + R["manage"]["r10_hold_em"] * em]
    return (max(held) - em) if held else None


def r11_stop(walls, prev_close, prev_high, em, R):
    """R11: if the nearest wall at/below the close and the nearest above are more than 2 EM apart, the stop follows
    the previous session high - 1 EM."""
    below = [w for w in walls if w <= prev_close]
    above = [w for w in walls if w > prev_close]
    if not below or not above:
        return None
    if min(above) - max(below) > R["manage"]["r11_gap_em"] * em:
        return prev_high - em
    return None


def t1_stop(fill, stop0, highest_high, em, R):
    """T1 (arms B-H): once the highest high since entry is >= +1R above the fill, stop = highest high - 1 EM."""
    r = fill - stop0
    if r > 0 and highest_high >= fill + R["manage"]["t1_trigger_r"] * r:
        return highest_high - R["manage"]["t1_em"] * em
    return None


def review_stop(stop, walls, prev_close, prev_high, em, R, trail=False, fill=None, stop0=None, hh=None):
    """Morning review: R10, R11 (and T1 for the trailing arms) with today's walls and EM; the stop only rises (R14)."""
    cands, why = [], []
    a = r10_stop(walls, prev_close, em, R)
    if a is not None:
        cands.append((a, "R10"))
    b = r11_stop(walls, prev_close, prev_high, em, R)
    if b is not None:
        cands.append((b, "R11"))
    if trail and fill is not None and hh is not None:
        t = t1_stop(fill, stop0, hh, em, R)
        if t is not None:
            cands.append((t, "T1"))
    best = max(cands, key=lambda x: x[0]) if cands else None
    if best and best[0] > stop + 1e-9:
        return round(best[0], 4), best[1]
    return stop, None


# ============================================================ calendar

def is_trading_day(d, R):
    return d.weekday() < 5 and d.isoformat() not in set(R.get("holidays", []))


def prev_trading_day(d, R):
    x = d - timedelta(days=1)
    while not is_trading_day(x, R):
        x -= timedelta(days=1)
    return x


def add_trading_days(d, n, R):
    x = d
    while n > 0:
        x += timedelta(days=1)
        if is_trading_day(x, R):
            n -= 1
    return x


def third_friday(y, m):
    d = date(y, m, 15)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def r06_blocked(d, R):
    ex = third_friday(d.year, d.month)
    return ex - timedelta(days=R["entry"]["r06_days_before_expiry"]) <= d <= ex


def is_live(d, R):
    return bool(R.get("approved")) and d >= date.fromisoformat(R["start"])


# ============================================================ data (adapters: the only code that reads files or Supabase)

def _j(name):
    try:
        return json.load(open(os.path.join(OUT, name)))
    except Exception:
        return None


class Data:
    """Phoenix inputs. Tests swap this for a fake."""

    def __init__(self):
        self.url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
        self.key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY") or ""
        self._map = self._chains = None

    # ---- repo files
    def map(self):
        if self._map is None:
            m = _j("mme_map.json") or {}
            rows = {r["ticker"]: r for g in (m.get("groups") or {}).values() for r in g}
            self._map = (rows, str(m.get("date") or "")[:10], m.get("asof"))
        return self._map

    def chains(self):
        if self._chains is None:
            c = _j("mme_chains.json") or {}
            self._chains = ({t: v.get("rows") or [] for t, v in (c.get("names") or {}).items()}, str(c.get("close_of") or "")[:10])
        return self._chains

    def spx_flip(self):
        g = _j("gex.json") or {}
        return (g.get("overview") or {}).get("gamma_flip"), str(g.get("asof") or "")[:10]

    def earnings(self, tk):
        st = ((_j("earnings_state.json") or {}).get("next_dates") or {}).get(tk)
        if st:
            return st, False
        e = (((_j("financials_all.json") or {}).get("tickers") or {}).get(tk) or {}).get("earnings") or {}
        return e.get("next_date"), bool(e.get("estimated"))

    def bars(self, tk):
        d = ((_j(os.path.join("charts", tk + ".json")) or {}).get("chart") or {}).get("daily") or {}
        out = {}
        for i, dd in enumerate(d.get("d") or []):
            try:
                out[dd] = {"o": d["o"][i], "h": d["h"][i], "l": d["l"][i], "c": d["c"][i]}
            except Exception:
                pass
        return out

    def commit(self):
        return os.environ.get("GITHUB_SHA", "")[:10] or None

    # ---- Supabase
    def _h(self, extra=None):
        h = {"apikey": self.key, "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        h.update(extra or {})
        return h

    def ok(self):
        return bool(self.url and self.key)

    def get(self, table, params):
        import requests
        out, off = [], 0
        while True:
            q = dict(params); q.update({"limit": "1000", "offset": str(off)})
            r = requests.get(f"{self.url}/rest/v1/{table}", headers=self._h(), params=q, timeout=60)
            if r.status_code != 200:
                raise RuntimeError(f"{table} HTTP {r.status_code}: {r.text[:200]}")
            rows = r.json() or []
            out += rows
            if len(rows) < 1000:
                return out
            off += 1000

    def snaps(self, tickers, day):
        """5-minute prices for the session of `day` (UTC timestamps): {tk: [(datetime_utc, last), ...]}."""
        lo = datetime(day.year, day.month, day.day, 13, 0, tzinfo=timezone.utc)
        hi = lo + timedelta(hours=9)
        rows = self.get("prices_intraday", {"select": "ticker,ts,last", "ticker": "in.(" + ",".join(sorted(set(tickers))) + ")",
                                             "ts": f"gte.{lo.isoformat()}", "and": f"(ts.lt.{hi.isoformat()})", "order": "ts.asc"})
        out = {}
        for r in rows:
            try:
                ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
                out.setdefault(r["ticker"], []).append((ts, float(r["last"])))
            except Exception:
                pass
        return out

    def trades(self, statuses=("open", "pending")):
        return self.get("mme_ft_trades", {"select": "*", "status": "in.(" + ",".join(statuses) + ")", "order": "id.asc"})

    def all_trades(self):
        return self.get("mme_ft_trades", {"select": "*", "order": "id.asc"})

    def insert_trade(self, row):
        import requests
        r = requests.post(f"{self.url}/rest/v1/mme_ft_trades", headers=self._h({"Prefer": "return=representation"}),
                          data=json.dumps(row, default=str), timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"insert trade HTTP {r.status_code}: {r.text[:200]}")
        return (r.json() or [{}])[0]

    def update_trade(self, tid, patch):
        import requests
        patch = dict(patch); patch["updated_at"] = datetime.now(timezone.utc).isoformat()
        r = requests.patch(f"{self.url}/rest/v1/mme_ft_trades", headers=self._h({"Prefer": "return=minimal"}),
                           params={"id": f"eq.{tid}"}, data=json.dumps(patch, default=str), timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"update trade HTTP {r.status_code}: {r.text[:200]}")

    def put_day(self, d, kind, data, dry):
        import requests
        body = {"d": d.isoformat(), "kind": kind, "dry": dry, "data": data, "updated_at": datetime.now(timezone.utc).isoformat()}
        r = requests.post(f"{self.url}/rest/v1/mme_ft_days", headers=self._h({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                          params={"on_conflict": "d,kind"}, data=json.dumps(body, default=str), timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"day HTTP {r.status_code}: {r.text[:200]}")

    def get_day(self, d, kind):
        rows = self.get("mme_ft_days", {"select": "*", "d": f"eq.{d.isoformat()}", "kind": f"eq.{kind}"})
        return rows[0] if rows else None

    def days(self, kind):
        return self.get("mme_ft_days", {"select": "d,kind,dry,data", "kind": f"eq.{kind}", "order": "d.asc"})

    def state(self, k, default=None):
        rows = self.get("mme_ft_state", {"select": "v", "k": f"eq.{k}"})
        return rows[0]["v"] if rows else default

    def heartbeat(self, lane, ok, note=""):
        """One row per lane in Supabase `heartbeats` (the table the app's system checks read). Best effort."""
        import requests
        try:
            body = {"lane": lane, "ts": datetime.now(timezone.utc).isoformat(), "ok": bool(ok), "note": str(note)[:400]}
            requests.post(f"{self.url}/rest/v1/heartbeats", headers=self._h({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                          params={"on_conflict": "lane"}, data=json.dumps(body), timeout=30)
        except Exception:
            pass

    def set_state(self, k, v):
        import requests
        body = {"k": k, "v": v, "updated_at": datetime.now(timezone.utc).isoformat()}
        r = requests.post(f"{self.url}/rest/v1/mme_ft_state", headers=self._h({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                          params={"on_conflict": "k"}, data=json.dumps(body, default=str), timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"state HTTP {r.status_code}: {r.text[:200]}")


# ============================================================ helpers

def et_10(day):
    """10:00 ET of `day` as a UTC datetime."""
    if ET:
        return datetime(day.year, day.month, day.day, 10, 0, tzinfo=ET).astimezone(timezone.utc)
    return datetime(day.year, day.month, day.day, 15, 0, tzinfo=timezone.utc)


def first_at_or_after(series, t):
    for ts, px in series or []:
        if ts >= t:
            return ts, px
    return None, None


def book_cost(trades, arm, acct):
    """Cost of open positions plus working entry orders in one book (R15)."""
    tot = 0.0
    for t in trades:
        if t["arm"] == arm and int(t["acct"]) == int(acct) and t["status"] in ("open", "pending"):
            q = float(t.get("qty") or 0)
            px = float(t.get("fill") or (t.get("entry") or {}).get("limit") or 0)
            tot += q * px
    return tot


# ============================================================ the funnel: every rule for every name

def evaluate(tk, row, price, chain_rows, spx, R, earn, today, data_ok):
    """All entry checks for one name at 10:00 ET. Returns a dict with each check, the first blocking rule for the
    base rules (arms A, B, G, H) and what arms C and D see."""
    ent = R["entry"]
    chk = {}
    em = row.get("em")
    em_ok = bool(em) and row.get("em_src") == "atm_iv"
    flip = row.get("flip")
    chk["R00"] = data_ok and (row.get("stale_days") or 0) <= 1
    dist = (price - flip) / em if (flip is not None and em and price) else None
    chk["R03"] = bool(em_ok and dist is not None and dist > ent["r03_min_em"])
    chk["R03c"] = bool(em_ok and dist is not None and dist > 0)                       # arm C
    walls = major_walls(chain_rows, price, row.get("support"), row.get("resist"), R) if price else []
    sp = closest3_spacing(walls, price) if walls else None
    chk["R04"] = bool(em and sp is not None and sp >= ent["r04_min_spacing_em"] * em)
    etf = tk in set(R.get("etf_no_earnings", []))
    ed, est = earn
    if etf:
        chk["R05"], r05_note = True, "ETF, exempt"
    else:
        known = bool(ed) and not est and ed >= today.isoformat()
        if not known:
            chk["R05"], r05_note = False, ("estimated date" if est else ("no date" if not ed else f"date {ed} already past"))
        else:
            lim = add_trading_days(today, ent["r05_trading_days"], R)
            chk["R05"] = not (today.isoformat() <= ed <= lim.isoformat())
            r05_note = f"next earnings {ed}"
    chk["R06"] = not r06_blocked(today, R)
    tiers = row.get("support_tiered") or []
    wall = tiers[0] if tiers else None
    band_hi = (wall + ent["band_em"] * em) if (wall and em) else None
    if wall is None or not em or not price:
        band, chk["R07"] = "no wall list", False
    elif price < wall:
        band, chk["R07"] = "below the wall: no setup", False
    elif price <= band_hi:
        band, chk["R07"] = "in the band: buy now", True
    else:
        band, chk["R07"] = "above the band: limit at the band top", True
    stop = (wall - ent["stop_em"] * em) if (wall and em) else None
    chk["R08"] = bool(stop is not None and stop < wall)
    pts = 0
    if dist is not None and dist > ent["points_stock_em"]:
        pts += 1
    if sp is not None and em and sp >= ent["points_walls_em"] * em:
        pts += 1
    if spx.get("dist") is not None and spx["dist"] > ent["points_spx_em"]:
        pts += 1
    order = ["R00", "R03", "R04", "R05", "R06", "R07", "R08"]
    first = next((k for k in order if not chk[k]), None)
    base_spx = spx.get("colour") == "GREEN"
    blocked = first or (None if base_spx else "R01")
    c_order = ["R00", "R03c", "R04", "R05", "R06", "R07", "R08"]
    first_c = next((k for k in c_order if not chk[k]), None)
    d_order = ["R00", "R03", "R04", "R06", "R07", "R08"]
    first_d = next((k for k in d_order if not chk[k]), None) or (None if base_spx else "R01")
    return {"ticker": tk, "price": price, "em": em, "em_src": row.get("em_src"), "flip": flip,
            "dist_em": round(dist, 2) if dist is not None else None, "walls": walls[:12], "spacing_em": round(sp / em, 2) if (sp and em) else None,
            "wall": wall, "band_hi": round(band_hi, 2) if band_hi else None, "band": band, "stop": round(stop, 2) if stop else None,
            "points": pts, "earnings": r05_note, "checks": chk, "blocked": blocked, "blocked_c": first_c, "blocked_d": first_d}


# ============================================================ trade lifecycle

def _exit(t, px, qty, reason, d, R, gap=False):
    """Close qty shares of trade t at px (slippage applied) — returns the exit record."""
    slip = R["costs"]["slip_gap"] if gap else R["costs"]["slip"]
    xp = round(px * (1 - slip), 4)
    com = commission(qty, "sell", R)
    pnl = round((xp - float(t["fill"])) * qty - com, 2)
    return {"d": d.isoformat(), "qty": qty, "px": xp, "reason": reason, "commission": com, "pnl": pnl}


def _finalise(t, R):
    """P&L in $ and R once a trade is fully closed (entry commission included)."""
    pnl = sum(x["pnl"] for x in t.get("exits") or []) - commission(float(t["qty"]), "buy", R)
    risk = float((t.get("entry") or {}).get("risk_usd") or 0)
    return round(pnl, 2), (round(pnl / risk, 3) if risk else None)


def remaining(t):
    return round(float(t.get("qty") or 0) - sum(float(x["qty"]) for x in (t.get("exits") or [])), 1)


# ============================================================ morning review

def morning(today, R=None, data=None, write=True):
    """Settle the previous session, then R10/R11/T1 with today's levels, the EM log, marks and equity."""
    R = R or load_rules(); data = data or Data()
    if not is_trading_day(today, R):
        return {"skipped": "not a trading day"}
    if today < date.fromisoformat(R.get("dry_run_from", R["start"])):
        return {"skipped": "before the dry run"}
    dry = not is_live(today, R)
    prev = prev_trading_day(today, R)
    rows, map_date, _ = data.map()
    chains, _cd = data.chains()
    trades = data.trades(("open", "pending"))
    tks = sorted({t["ticker"] for t in trades})
    snaps = data.snaps(tks, prev) if tks else {}
    log = {"settled": [], "raised": [], "stops": [], "fills": [], "deviations": []}
    for t in trades:
        t["entry"] = t.get("entry") or {}; t["exits"] = t.get("exits") or []; t["stop_hist"] = t.get("stop_hist") or []
        t["marks"] = t.get("marks") or []; t["emlog"] = t.get("emlog") or []
        tk, s = t["ticker"], snaps.get(t["ticker"], [])
        bars = data.bars(tk); bar = bars.get(prev.isoformat())
        dec_t = datetime.fromisoformat(t["entry"]["decided_at"]) if t["entry"].get("decided_at") else et_10(prev)
        patch = {}
        # 1. pending limit at the band top: filled if a snapshot after the decision reaches it on the decision day
        if t["status"] == "pending":
            if str(t["d_open"]) == prev.isoformat():
                hit = next(((ts, px) for ts, px in s if ts >= dec_t and px <= t["entry"]["limit"]), None)
                if hit:
                    t["status"] = "open"; t["fill"] = t["entry"]["limit"]
                    t["entry"]["filled_at"] = hit[0].isoformat()
                    patch.update({"status": "open", "fill": t["fill"], "entry": t["entry"]})
                    log["fills"].append(f"{t['arm']}/{t['acct']} {tk} limit filled at {t['fill']}")
                    dec_t = hit[0]
                else:
                    patch.update({"status": "cancelled", "exit_reason": "limit not reached (cancelled at the close)", "d_close": prev.isoformat()})
                    t["status"] = "cancelled"
            else:
                patch.update({"status": "cancelled", "exit_reason": "stale pending order"}); t["status"] = "cancelled"
        if t["status"] != "open":
            if write and patch:
                data.update_trade(t["id"], patch)
            continue
        # 2. stop touches during the previous session (R13): first snapshot at or below the stop
        stop = float(t["stop"])
        after = [(ts, px) for ts, px in s if ts >= dec_t]
        if s and str(t["d_open"]) != prev.isoformat() and s[0][1] < stop:            # opened below the stop: gap exit
            x = _exit(t, s[0][1], remaining(t), "stop (gap through at the open)", prev, R, gap=True)
        else:
            hit = next(((ts, px) for ts, px in after if px <= stop), None)
            x = _exit(t, stop, remaining(t), "stop hit", prev, R) if hit else None
        if x is None and bar and bar["l"] <= stop:
            log["deviations"].append(f"{t['arm']}/{t['acct']} {tk}: the daily low {bar['l']} touched the stop {stop:.2f} between snapshots; snapshots decide (FT-1.3 4.4)")
        if x:
            t["exits"].append(x)
            pnl, rr = _finalise(t, R)
            level = (t["stop_hist"][-1]["rule"] + " stop") if t["stop_hist"] else "initial stop"
            patch.update({"status": "closed", "exits": t["exits"], "pnl": pnl, "r": rr, "d_close": prev.isoformat(),
                          "exit_reason": f"{x['reason']} \u00b7 {level}"})
            log["stops"].append(f"{t['arm']}/{t['acct']} {tk} {x['reason']} at {x['px']}: {pnl:+.2f} ({rr}R)")
            if write:
                data.update_trade(t["id"], patch)
            continue
        # 3. morning review with today's walls and EM (R10, R11, T1); estimated EM: no raise
        row = rows.get(tk) or {}
        em = row.get("em"); em_ok = bool(em) and row.get("em_src") == "atm_iv"
        close = bar["c"] if bar else (s[-1][1] if s else None)
        high = bar["h"] if bar else (max(px for _, px in s) if s else None)
        day_high = (max([px for _, px in after], default=0) if str(t["d_open"]) == prev.isoformat() else (high or 0))
        hh = max([float(t["entry"].get("hh") or 0), day_high])
        t["entry"]["hh"] = hh
        if em_ok and close is not None:
            walls = major_walls(chains.get(tk, []), close, row.get("support"), row.get("resist"), R)
            arm = R["arms"][t["arm"]]
            new, rule = review_stop(stop, walls, close, high, em, R, trail=arm.get("trail"), fill=float(t["fill"]),
                                    stop0=float(t["entry"]["stop0"]), hh=hh)
            if rule:
                t["stop_hist"].append({"d": today.isoformat(), "from": stop, "to": new, "rule": rule, "em": em})
                patch.update({"stop": new, "stop_hist": t["stop_hist"]})
                log["raised"].append(f"{t['arm']}/{t['acct']} {tk} {rule}: {stop:.2f} -> {new:.2f}")
        elif not em_ok:
            log["deviations"].append(f"{t['arm']}/{t['acct']} {tk}: EM estimated or missing today, stop not reviewed")
        # 4. EM log (R12, log only) and the mark at the previous close
        em0 = float(t["entry"].get("em") or 0)
        if em and em0 and close:
            t["emlog"].append({"d": today.isoformat(), "em": em, "em_src": row.get("em_src"), "widen_pct": round((em / em0 - 1) * 100, 1),
                               "em_pct_price": round(em / close * 100, 2), "close": close, "stop": float(patch.get("stop", stop))})
        if close:
            q = remaining(t)
            t["marks"].append({"d": prev.isoformat(), "close": close, "open_pnl": round((close - float(t["fill"])) * q, 2)})
        patch.update({"entry": t["entry"], "emlog": t["emlog"], "marks": t["marks"]})
        if write:
            data.update_trade(t["id"], patch)
    eq = equity(data.all_trades() if write else trades, R)
    day = {"prev_session": prev.isoformat(), "map_date": map_date, "log": log, "equity": eq, "bench": bench(data, R)}
    if write:
        data.put_day(today, "morning", day, dry)
        write_contract(data, R, today, dry)
    return day


def equity(trades, R):
    """Per arm and account: realised, open (at the last mark), equity, counts."""
    out = {}
    for arm in R["arms"]:
        for a in R["accounts"]:
            ts = [t for t in trades if t["arm"] == arm and int(t["acct"]) == a and t["status"] in ("open", "closed")]
            real = sum(float(t.get("pnl") or 0) for t in ts if t["status"] == "closed")
            opn = 0.0
            for t in ts:
                if t["status"] == "open":
                    m = (t.get("marks") or [])
                    part = sum(float(x["pnl"]) for x in (t.get("exits") or []))
                    opn += (float(m[-1]["open_pnl"]) if m else 0.0) + part - commission(float(t["qty"] or 0), "buy", R)
            closed = [t for t in ts if t["status"] == "closed"]
            rs = [float(t["r"]) for t in closed if t.get("r") is not None]
            out.setdefault(arm, {})[str(a)] = {"realised": round(real, 2), "open": round(opn, 2), "equity": round(a + real + opn, 2),
                                               "n_open": sum(1 for t in ts if t["status"] == "open"), "n_closed": len(closed),
                                               "sum_r": round(sum(rs), 2), "wins": sum(1 for r in rs if r > 0)}
    return out


def bench(data, R):
    out = {}
    for tk in R.get("benchmarks", []):
        b = data.bars(tk)
        if b:
            d = max(b); out[tk] = {"d": d, "c": b[d]["c"]}
    return out


# ============================================================ 10:00 ET decision

def decide(today, R=None, data=None, write=True, now=None):
    R = R or load_rules(); data = data or Data()
    if not is_trading_day(today, R):
        return {"skipped": "not a trading day"}
    if today < date.fromisoformat(R.get("dry_run_from", R["start"])):
        return {"skipped": "before the dry run"}
    dry = not is_live(today, R)
    t10 = et_10(today)
    if write and data.get_day(today, "decide"):
        return {"skipped": "already decided today"}
    rows, map_date, map_asof = data.map()
    chains, chain_date = data.chains()
    prev = prev_trading_day(today, R)
    # R00: the map must come from a run after the previous close (Phoenix dates the map by the morning run that
    # computed it, so Monday's map carries Monday's date and Friday's close)
    data_ok = bool(map_date) and prev.isoformat() < map_date <= today.isoformat()
    names = sorted(rows)
    snaps = data.snaps(names + ["SPX", "VIX"], today)
    px = {tk: first_at_or_after(snaps.get(tk), t10)[1] for tk in names}
    spx_px = first_at_or_after(snaps.get("SPX"), t10)[1]
    vix = first_at_or_after(snaps.get("VIX"), t10)[1]
    flip, flip_d = data.spx_flip()
    sem = spx_em(spx_px, vix, R) if (spx_px and vix) else None
    colour, sdist = spx_colour(spx_px, flip, sem, R)
    spx = {"price": spx_px, "vix": vix, "flip": flip, "flip_asof": flip_d, "em": round(sem, 2) if sem else None,
           "dist": round(sdist, 2) if sdist is not None else None, "colour": colour}
    if colour is None:
        data_ok = False
    trades = data.trades(("open", "pending"))
    actions = []
    # ---- R01 cuts on open trades
    cut_state = data.state("cuts", {}) if write else {}
    for arm, cfg in R["arms"].items():
        if arm in R["regime_cuts"]["exempt_arms"]:
            continue
        last = cut_state.get(arm)
        if colour == "GREEN":
            cut_state[arm] = None; continue
        if colour not in ("AMBER", "RED") or last == colour or (last == "RED" and colour == "AMBER"):
            continue
        for t in [x for x in trades if x["arm"] == arm and x["status"] == "open"]:
            p = px.get(t["ticker"])
            if not p:
                continue
            t["exits"] = t.get("exits") or []
            q = remaining(t)
            if colour == "RED" and arm not in R["regime_cuts"]["red_keep_arms"]:
                x = _exit(t, p, q, "R01 RED: close", today, R); t["exits"].append(x)
                pnl, rr = _finalise(t, R)
                patch = {"status": "closed", "exits": t["exits"], "pnl": pnl, "r": rr, "d_close": today.isoformat(), "exit_reason": "R01 RED close"}
                t["status"] = "closed"
            elif colour == "AMBER" and last is None:
                sq = round(max(R["risk"]["qty_step"], floor_step(q * R["regime_cuts"]["amber_sell_pct"] / 100.0)), 1)
                if sq >= q:
                    continue
                x = _exit(t, p, sq, "R01 AMBER: sell 25%", today, R); t["exits"].append(x)
                patch = {"exits": t["exits"]}
            else:
                continue
            actions.append(f"{arm}/{t['acct']} {t['ticker']} {x['reason']} {x['qty']} @ {x['px']}")
            if write:
                data.update_trade(t["id"], patch)
        cut_state[arm] = colour if colour in ("AMBER", "RED") else None
    # ---- the funnel
    funnel = []
    for tk in names:
        p = px.get(tk)
        if p is None:
            funnel.append({"ticker": tk, "blocked": "R00", "note": "no 10:00 ET price"}); continue
        ev = evaluate(tk, rows[tk], p, chains.get(tk, []), spx, R, data.earnings(tk), today, data_ok)
        funnel.append(ev)
    ok = {e["ticker"]: e for e in funnel if "checks" in e}
    held = {(t["arm"], t["ticker"]) for t in trades if t["status"] in ("open", "pending")}
    # ---- entries per arm (same candidate list; only the variable under test changes)
    plan = {}
    for arm, cfg in R["arms"].items():
        if cfg.get("random"):
            continue
        if cfg.get("spx_any"):
            cands = [e for e in ok.values() if e["blocked_c"] is None]
        elif cfg.get("r05_off"):
            cands = [e for e in ok.values() if e["blocked_d"] is None]
        else:
            cands = [e for e in ok.values() if e["blocked"] is None]
        cands.sort(key=lambda e: (-e["points"], -(e["dist_em"] or 0)))                  # ranking: tier, then flip distance
        plan[arm] = [e for e in cands if (arm, e["ticker"]) not in held]
    new_rows = []

    def open_trade(arm, e, acct, stop, limit=None, risk_floor=None, tier_from=None, kind="wall"):
        mkt = limit is None
        fill = round(e["price"] * (1 + R["costs"]["slip"]), 4) if mkt else limit
        if not (stop < fill):
            return None, "stop not below the fill"
        cost_open = book_cost(trades + new_rows, arm, acct)
        pts = e["points"] if tier_from is None else tier_from
        sz = size_position(fill, stop, e["em"], acct, pts, R, cost_open, risk_floor)
        if not sz["qty"]:
            return None, sz["why"]
        entry = {"kind": kind, "decided_at": t10.isoformat(), "price_10": e["price"], "wall": e.get("wall"), "band_hi": e.get("band_hi"),
                 "limit": limit, "stop0": round(stop, 4), "em": e["em"], "em_src": e.get("em_src"), "flip": e.get("flip"), "dist_em": e.get("dist_em"),
                 "spacing_em": e.get("spacing_em"), "walls": e.get("walls"), "points": pts, "tier_pct": sz["tier_pct"], "risk_usd": sz["risk_usd"],
                 "cost": sz["cost"], "spx": spx, "earnings": e.get("earnings"), "map_date": map_date, "map_asof": map_asof,
                 "chain_date": chain_date, "commit": data.commit(), "hh": e["price"],
                 "why": (f"{e['ticker']} {e['dist_em']} EM above its flip; heaviest support wall {e.get('wall')}; {e['band']}; "
                         f"{pts} point(s) -> {sz['tier_pct']}% risk; SPX {spx['colour']} ({spx['dist']} EM)") if kind == "wall" else
                        (f"random dip benchmark: {e['ticker']} {e['dist_em']} EM above its flip, not in a wall band; stop 1 EM")}
        row = {"arm": arm, "acct": acct, "ticker": e["ticker"], "d_open": today.isoformat(), "status": "open" if mkt else "pending",
               "dry": dry, "entry": entry, "qty": sz["qty"], "fill": fill if mkt else None, "stop": round(stop, 4),
               "stop_hist": [], "exits": [], "marks": [], "emlog": []}
        new_rows.append(row)
        return row, None

    placed, skipped = [], []
    for arm, cands in plan.items():
        cfg = R["arms"][arm]
        for e in cands:
            limit = e["band_hi"] if e["band"].startswith("above") else None
            for acct in R["accounts"]:
                row, why = open_trade(arm, e, acct, e["stop"], limit, cfg.get("min_risk"))
                (placed if row else skipped).append(f"{arm}/{acct} {e['ticker']}" + ("" if row else f": {why}"))
    # ---- arm E: one random dip entry per arm-B entry, same day, another name
    E = R["arms"].get("E")
    if E:
        rng = random.Random(E["seed"] * 10000 + today.toordinal())
        b_today = [e for e in plan.get("B", [])]
        pool = []
        for e in ok.values():
            d = e.get("dist_em")
            in_band = any(w <= e["price"] <= w + R["entry"]["band_em"] * e["em"] for w in (e.get("walls") or []) if e["em"])
            if d is not None and E["dist_lo"] <= d <= E["dist_hi"] and not in_band and e["checks"]["R00"] and e["em_src"] == "atm_iv":
                pool.append(e)
        for be in b_today:
            choices = [e for e in pool if e["ticker"] != be["ticker"] and ("E", e["ticker"]) not in held
                       and all(r["ticker"] != e["ticker"] or r["arm"] != "E" for r in new_rows)]
            if not choices:
                skipped.append(f"E: no random partner for {be['ticker']}"); continue
            pick = rng.choice(sorted(choices, key=lambda x: x["ticker"]))
            for acct in R["accounts"]:
                row, why = open_trade("E", pick, acct, pick["price"] - pick["em"], None, None, be["points"], kind="random")
                (placed if row else skipped).append(f"E/{acct} {pick['ticker']} (for {be['ticker']})" + ("" if row else f": {why}"))
    if write:
        for r in new_rows:
            data.insert_trade(r)
        data.set_state("cuts", cut_state)
    day = {"decided_at": t10.isoformat(), "data_ok": data_ok, "map_date": map_date, "chain_date": chain_date, "spx": spx,
           "r06_blocked": r06_blocked(today, R), "cuts": actions, "placed": placed, "skipped": skipped,
           "funnel": [{k: v for k, v in e.items() if k != "walls"} for e in funnel], "dry": dry}
    if write:
        data.put_day(today, "decide", day, dry)
    return day


# ============================================================ contract output (Phoenix 2.0 envelope)

def write_contract(data, R, today, dry):
    try:
        trades = data.all_trades()
    except Exception:
        trades = []
    live = [t for t in trades if not t.get("dry")]
    use = live if live else trades
    body = {"schema_version": SCHEMA, "asof": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "source": "trading/mme_ft.py · rules " + R["version"] + " · protocol " + R["protocol"],
            "freshness": {"date": today.isoformat()}, "approved": bool(R.get("approved")), "start": R["start"],
            "mode": "live" if live else "dry run", "equity": equity(use, R),
            "open": sum(1 for t in use if t["status"] == "open"), "pending": sum(1 for t in use if t["status"] == "pending"),
            "closed": sum(1 for t in use if t["status"] == "closed")}
    try:
        with open(os.path.join(OUT, "mme_ft.json"), "w") as f:
            json.dump(body, f, separators=(",", ":"), default=str)
    except Exception as e:
        print(f"[mme_ft] contract file not written: {e}")


# ============================================================ entry points

def today_et():
    now = datetime.now(timezone.utc)
    return (now.astimezone(ET) if ET else now).date(), now


def run_morning():
    R = load_rules(); d = Data()
    if not d.ok():
        print("[mme_ft] no Supabase credentials — skipped"); return None
    day, _ = today_et()
    try:
        out = morning(day, R, d)
    except Exception as e:
        d.heartbeat("mme_test_morning", False, f"{type(e).__name__}: {e}"); raise
    lg = (out or {}).get("log") or {}
    d.heartbeat("mme_test_morning", True, (out or {}).get("skipped") or
                f"{'dry run' if not is_live(day, R) else 'live'} · stops {len(lg.get('stops', []))} · raises {len(lg.get('raised', []))} · fills {len(lg.get('fills', []))}")
    print(f"[mme_ft] morning {day}: {json.dumps(out.get('log') if isinstance(out, dict) and 'log' in out else out, default=str)[:600]}")
    return out


def run_decide(force=False):
    """The 10:00 ET job. Runs only between 10:00 and 11:30 ET (the workflow fires at two UTC times to cover DST)."""
    R = load_rules(); d = Data()
    if not d.ok():
        print("[mme_ft] no Supabase credentials — skipped"); return None
    day, now = today_et()
    et = now.astimezone(ET) if ET else now
    if not force and not (et.hour == 10 or (et.hour == 11 and et.minute <= 30)):
        print(f"[mme_ft] {et:%H:%M} ET is outside the 10:00-11:30 ET window — nothing to do"); return None
    try:
        out = decide(day, R, d)
    except Exception as e:
        d.heartbeat("mme_test_decide", False, f"{type(e).__name__}: {e}"); raise
    if isinstance(out, dict) and "spx" in out:
        d.heartbeat("mme_test_decide", bool(out.get("data_ok")), f"{'dry run' if out.get('dry') else 'live'} · SPX {out['spx'].get('colour')} · "
                    f"placed {len(out.get('placed', []))} · cuts {len(out.get('cuts', []))}" + ("" if out.get("data_ok") else " · DATA CHECK FAILED"))
    print(f"[mme_ft] decide {day}: spx {out.get('spx')} · placed {len(out.get('placed', []))} · cuts {out.get('cuts')}"
          if isinstance(out, dict) and "spx" in out else f"[mme_ft] decide {day}: {out}")
    return out


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "decide":
        run_decide(force="--force" in sys.argv)
    elif cmd == "morning":
        run_morning()
    else:
        print("usage: python -m trading.mme_ft decide|morning [--force]")
