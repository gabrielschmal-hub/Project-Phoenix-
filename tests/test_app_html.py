"""Browser tests for phoenix_app.html + portfolio_builder.html. Network is blocked: layout only.

2 Sep 2026: the iPhone sweep used to visit '#trade', which is not a route — the page is '#trades' —
so the Trade page was never inside the overflow check. Fixed, and the Screener tab is now covered
at 390 and 1280 with mocked signal_log rows in every state the card can render.
"""
import json, pathlib, pytest
from playwright.sync_api import sync_playwright
ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "phoenix_app.html"; BUILDER = ROOT / "portfolio_builder.html"
PAGES = ("home", "launch", "markets", "screeners", "trades", "portfolio", "smart", "research")


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(); yield b; b.close()


def _page(browser, w, h, mobile):
    pg = browser.new_page(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    pg.route("**/*", lambda r: r.abort() if not r.request.url.startswith("file://") else r.continue_())
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e))); return pg, errs


def _bad(errs):
    return [e for e in errs if "SyntaxError" in e or "ReferenceError" in e or "TypeError" in e]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_app_iphone_layout(browser):
    pg, errs = _page(browser, 390, 844, True); pg.goto(APP.as_uri()); pg.wait_for_timeout(1500)
    assert 'name="viewport"' in pg.content()
    pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(1000)
    for hsh in PAGES:
        pg.evaluate(f"location.hash='#{hsh}'"); pg.wait_for_timeout(900)
        assert pg.evaluate("document.documentElement.scrollWidth") <= 392, f"#{hsh} overflows"
    bar = pg.locator("#pxnav").bounding_box(); assert bar["y"] > 700 and bar["width"] >= 388, "rail must be a bottom bar on phones"
    assert pg.locator("#pxnav .pxlinks a").count() == 8      # + Smart Money and Launch Control
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_app_desktop_unchanged(browser):
    pg, errs = _page(browser, 1280, 900, False); pg.goto(APP.as_uri()); pg.wait_for_timeout(1400)
    pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(900)
    r = pg.locator("#pxnav").bounding_box(); assert r["x"] == 0 and r["height"] > 700, "desktop rail stays on the left"
    assert pg.evaluate("document.documentElement.scrollWidth") <= 1280


# ---------------------------------------------------------------- Screener tab (signal_log read side)

def _sig(t, **k):
    base = dict(ticker=t, date="2026-09-01", age_days=1, trigger=100.0, close=None, entry_ref=None,
                atr_pct=2.0, stop_pct=5.0, sector="Tech", industry="Software", opp_score=71.5, rank=3,
                r_20d=None, mfe_20d=None, mae_20d=None, bars_marked=None, data_age_min=30, profitable_ocf=None)
    for m in ("stop15", "stop20", "stop25"):
        for f in ("_hit", "_day", "_tp2", "_tp3", "_r"): base[m + f] = None
    base.update(k); return base


SIGNALS = [
    _sig("PROV", entry_ref=100.0),                                                    # open, provisional entry
    _sig("NOENT"),                                                                    # open, no entry price yet
    _sig("STOPD", date="2026-08-20", close=50.0, stop25_hit=True, stop25_day=4, stop25_r=-1.0,
         stop20_hit=True, stop20_day=3, stop15_hit=True, stop15_day=2, bars_marked=8),
    _sig("WINR", date="2026-07-20", close=20.0, r_20d=0.12, mfe_20d=0.16, mae_20d=-0.01, bars_marked=20,
         stop25_hit=False, stop25_tp2=True, stop25_tp3=False, stop25_r=2.4,
         stop20_hit=False, stop20_tp2=True, stop20_tp3=True, stop20_r=3.0,
         stop15_hit=True, stop15_day=2, stop15_r=-1.0, profitable_ocf=False, profitability="lossmaking"),
]
LIVE = [{"ticker": "PROV", "last": 104.0, "prev_close": 103, "chg_pct": 1,
         "quote_ts": "2026-09-02T19:57:00Z", "updated_at": "2026-09-02T19:58:00Z"}]


def _screener_page(browser, w, h, mobile):
    pg = browser.new_page(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e))); hits = {"n": 0}
    def route(r):
        u = r.request.url
        if u.startswith("file://"): return r.continue_()
        if "/rest/v1/signal_log" in u:
            hits["n"] += 1; return r.fulfill(status=200, content_type="application/json", body=json.dumps(SIGNALS))
        if "/rest/v1/prices_live" in u: return r.fulfill(status=200, content_type="application/json", body=json.dumps(LIVE))
        if "/rest/v1/" in u: return r.fulfill(status=200, content_type="application/json", body="[]")
        return r.abort()
    pg.route("**/*", route)
    pg.goto(APP.as_uri()); pg.wait_for_timeout(1200)
    pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(800)
    pg.evaluate("location.hash='#trades'"); pg.wait_for_timeout(1400)
    pg.locator('#trTabs .tr-tab[data-tab="screener"]').click(); pg.wait_for_timeout(1500)
    return pg, errs, hits


def _cells(card):
    return {c.locator(".ck").text_content().strip(): c.locator(".cv").text_content().strip()
            for c in card.locator(".tr-cell").all()}


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_screener_tab_reads_signal_log_once_and_renders_every_state(browser):
    pg, errs, hits = _screener_page(browser, 1280, 900, False)
    assert pg.locator("#trTabs .tr-tab").count() == 4
    assert hits["n"] == 1, f"signal_log fetched {hits['n']} times — must be once per session"
    assert pg.locator("#trCntScr").inner_text() == "4"
    cards = pg.locator("#trBody .tr-card"); assert cards.count() == 4
    order = [cards.nth(i).locator(".tr-tk").text_content().replace("\u2192", "").strip() for i in range(4)]
    assert order[:2] == ["PROV", "NOENT"], "open positions come first, newest first"

    prov = _cells(cards.nth(0))
    assert prov["Entry"] == "100.00" and prov["Current"] == "104.00"
    assert prov["R now (2.5x)"] == "+0.80R"                         # (104-100) / (2.5 * 2% * 100)
    assert prov["Dist to stop"] == "+8.7%"                          # same sign as the Open tab: + = room
    assert (prov["1.5x ATR stop"], prov["2.0x ATR stop"], prov["2.5x ATR stop"]) == ("97.00", "96.00", "95.00")
    assert "provisional" not in prov["Entry"] and "live-lane" in cards.nth(0).text_content()

    noent = _cells(cards.nth(1))
    assert noent["Entry"] == "none yet" and noent["R now (2.5x)"] == "\u2014", "no entry must never fake an R"

    stopd = _cells(cards.nth(2)); chips = [c.strip() for c in cards.nth(2).locator(".tr-chip").all_inner_texts()]
    assert "STOPPED" in chips and any("Stopped 2.5x" in c for c in chips)
    assert stopd["1.5x ATR"] == "Stopped d2" and stopd["2.5x ATR"] == "Stopped d4"

    winr = _cells(cards.nth(3)); chips = [c.strip() for c in cards.nth(3).locator(".tr-chip").all_inner_texts()]
    assert "2R hit" in chips and "3R hit" not in chips              # tp2 first on 2.5x, tp3 never
    assert "lossmaking" in chips and "OCF negative" not in chips     # the engine's state word, not a paraphrase
    assert winr["1.5x ATR"] == "Stopped d2" and winr["2.0x ATR"] == "+3.00R" and winr["2.5x ATR"] == "+2.40R"

    summary = pg.locator("#trBody .sl-note").first.inner_text()
    assert "INSUFFICIENT" in summary and "not a verdict" in summary
    assert pg.evaluate("document.documentElement.scrollWidth") <= 1280
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_screener_tab_fits_a_phone(browser):
    pg, errs, hits = _screener_page(browser, 390, 844, True)
    assert pg.locator("#trBody .tr-card").count() == 4
    assert pg.evaluate("document.documentElement.scrollWidth") <= 392, "four tabs + Export must wrap, not overflow"
    pg.locator('#trTabs .tr-tab[data-tab="open"]').click(); pg.wait_for_timeout(500)
    assert pg.locator("#trBody .w-card-t").inner_text() == "Open positions"
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_screener_tab_unreachable_log_is_a_failure_not_an_empty_book(browser):
    pg, errs = _page(browser, 1280, 900, False)
    pg.route("**/rest/v1/signal_log*", lambda r: r.fulfill(status=500, body="down"))
    pg.goto(APP.as_uri()); pg.wait_for_timeout(1200)
    pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(800)
    pg.evaluate("location.hash='#trades'"); pg.wait_for_timeout(1200)
    pg.locator('#trTabs .tr-tab[data-tab="screener"]').click(); pg.wait_for_timeout(1200)
    txt = pg.locator("#trBody .tr-empty").inner_text()
    assert "unreachable" in txt and "not zero" in txt


@pytest.mark.skipif(not BUILDER.exists(), reason="portfolio_builder.html not in repo root")
def test_builder_offline_fallback_and_no_fetch_loop(browser):
    pg, errs = _page(browser, 390, 844, True)
    n = {"c": 0}; pg.route("**/rest/v1/prices_history*", lambda r: (n.__setitem__("c", n["c"] + 1), r.fulfill(status=200, content_type="application/json", body="[]")))
    pg.route("**/rest/v1/portfolios*", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
    pg.goto(BUILDER.as_uri() + "?profile=G"); pg.wait_for_timeout(900)
    assert "__PASTE" not in pg.content(), "anon key placeholder must be baked in"
    pg.fill("#q", "GOOG"); pg.press("#q", "Enter"); pg.wait_for_timeout(1500)
    assert n["c"] == 1, f"unmeasured ticker fetched {n['c']} times — loop"
    assert pg.evaluate("document.activeElement.id") == "q"
    assert pg.evaluate("document.documentElement.scrollWidth") <= 390 and not errs


# ---------------------------------------------------------------- Portfolio -> Screener (the book view)
# fetch() of outputs/signal_scorecard.json is refused from a file:// origin, so this view is
# served over localhost. Everything else in the file keeps the file:// harness.
import threading, http.server, functools, socket


@pytest.fixture(scope="module")
def served():
    Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _book_sig(i, marked):
    hit = marked and i % 2 == 0
    r = _sig("B%02d" % i, date=["2026-08-25", "2026-08-28", "2026-09-01"][i % 3], close=100.0 if marked else None,
             atr_pct=[1.0, 3.0, 8.0][i % 3], sector=["Tech", "Energy"][i % 2], profitability=["profitable", "lossmaking"][i % 2],
             mcap_B=[1.0, 30.0, 120.0][i % 3], breakout=i % 4 == 0, days_on_list=i % 3, regime=[None, "ENERGY_SHOCK"][i % 2],
             gex_regime="Negative Gamma", spx_vs_200d_pct=2.0, r_20d=(0.05 if marked else None), mfe_20d=(0.12 if marked else None))
    for k in ("stop15", "stop20", "stop25"):
        r[k + "_hit"] = hit if marked else None; r[k + "_day"] = 3 if hit else None
        r[k + "_r"] = (-1.0 if hit else 1.2) if marked else None
        r[k + "_tp2"] = False if marked else None; r[k + "_tp2_day"] = None; r[k + "_tp3"] = False if marked else None; r[k + "_tp3_day"] = None
    return r


SC_MARKED = {"n": 30, "verdict": "CONTINUE", "fails": [], "asof": "2026-09-26T04:31", "decision_variable": "2.5x ATR, stop only, 20 bars",
             "atr_grid": {"1.5x": {"n": 30, "E_stop_R": -0.05, "E_tp2_R": 0.1, "E_tp3_R": 0.0, "stop_hit": 0.6, "tp2_first": 0.3, "tp3_first": 0.1, "median_mfe_R": 1.9},
                          "2.0x": {"n": 30, "E_stop_R": 0.11, "E_tp2_R": 0.2, "E_tp3_R": 0.1, "stop_hit": 0.5, "tp2_first": 0.35, "tp3_first": 0.15, "median_mfe_R": 1.5},
                          "2.5x": {"n": 30, "E_stop_R": 0.21, "E_tp2_R": 0.3, "E_tp3_R": 0.2, "stop_hit": 0.42, "tp2_first": 0.38, "tp3_first": 0.18, "median_mfe_R": 1.3}},
             "lenses": {"sector": [{"group": "Tech", "n": 22, "thin": False, "E_stop_R": 0.4, "stop_hit": 0.3, "tp2_first": 0.5, "median_mfe_R": 1.8},
                                   {"group": "Energy", "n": 8, "thin": True, "E_stop_R": -0.6, "stop_hit": 0.8, "tp2_first": 0.1, "median_mfe_R": 0.4}]},
             "min_cell": 20}
SC_EMPTY = {"n": 0, "verdict": "INSUFFICIENT", "need": 30, "lenses": {}, "min_cell": 20, "asof": "2026-09-03T04:31"}


def _book_page(browser, base, w, h, mobile, marked):
    pg = browser.new_page(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e))); hits = {"sig": 0, "sc": 0}
    sigs = [_book_sig(i, marked) for i in range(30)]
    def route(r):
        u = r.request.url
        if "signal_scorecard.json" in u:
            hits["sc"] += 1; return r.fulfill(status=200, content_type="application/json", body=json.dumps(SC_MARKED if marked else SC_EMPTY))
        if u.startswith(base): return r.continue_()
        if "/rest/v1/signal_log" in u:
            hits["sig"] += 1; return r.fulfill(status=200, content_type="application/json", body=json.dumps(sigs))
        if "/rest/v1/" in u: return r.fulfill(status=200, content_type="application/json", body="[]")
        return r.abort()
    pg.route("**/*", route)
    pg.goto(base + "/phoenix_app.html"); pg.wait_for_timeout(1200)
    pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(800)
    pg.evaluate("location.hash='#portfolio'"); pg.wait_for_timeout(1400)
    pg.locator('#pfSwitch .pf-seg[data-scr]').click(); pg.wait_for_timeout(1800)
    return pg, errs, hits


def _tds(tbl_row):
    return [x.strip() for x in tbl_row.locator("td").all_inner_texts()]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_portfolio_screener_segment_sits_before_add_and_hides_the_book(browser, served):
    pg, errs, hits = _book_page(browser, served, 1280, 900, False, marked=False)
    segs = [x.split("\n")[0].strip() for x in pg.locator("#pfSwitch .pf-seg").all_inner_texts()]
    assert segs[-2:] == ["SCREENER", "+"], segs
    assert hits["sig"] == 1 and hits["sc"] == 1
    assert pg.evaluate("getComputedStyle(document.getElementById('pfScr')).display") != "none"
    assert pg.evaluate("getComputedStyle(document.getElementById('pfStrip').closest('.w-card')).display") == "none"
    pg.locator('#pfSwitch .pf-seg[data-acc]').first.click(); pg.wait_for_timeout(700)
    assert pg.evaluate("getComputedStyle(document.getElementById('pfScr')).display") == "none"
    assert pg.evaluate("getComputedStyle(document.getElementById('pfStrip').closest('.w-card')).display") != "none"
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_portfolio_screener_day_one_shows_sizing_and_composition_without_a_verdict(browser, served):
    pg, errs, _ = _book_page(browser, served, 1280, 900, False, marked=False)
    strip = [c.strip() for c in pg.locator("#pfScr .sm-stat .v").all_inner_texts()]
    assert strip == ["INSUFFICIENT", "30", "30", "0/30"]
    tbls = pg.locator("#pfScr .pf-scr-tbl table")
    assert _tds(tbls.nth(0).locator("tbody tr").nth(2))[:3] == ["2.5× ATR DECISION", "—", "—"]     # nothing marked: no E invented
    sizing = _tds(tbls.nth(1).locator("tbody tr").nth(2))                                         # 2.5x row: med ATR 3% -> stop 7.5%
    assert sizing[1] == "7.5%" and sizing[2] == "13%" and sizing[4] == "27%"                      # 1%/7.5% and 2%/7.5%
    assert sizing[6] == "10 / 10", "ATR 1% names: 2.5% stop -> 40% position at 1% risk breaches the 35% cap"
    assert _tds(tbls.nth(2).locator("tbody tr").first)[:3] == ["Tech", "15", "0"]                  # composition today, marked 0
    assert pg.locator("#pfScr tr.thin").count() == 2
    pg.locator('#pfScr button[data-lens="regime"]').click(); pg.wait_for_timeout(400)
    txt = pg.locator("#pfScr .w-card").nth(3).inner_text()
    assert "ENERGY_SHOCK" in txt and "15 signals have no value" in txt and "not reconstructed" in txt
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_portfolio_screener_marked_state_reads_the_report_and_sizes_drawdown(browser, served):
    pg, errs, _ = _book_page(browser, served, 1280, 900, False, marked=True)
    strip = [c.strip() for c in pg.locator("#pfScr .sm-stat .v").all_inner_texts()]
    assert strip == ["CONTINUE", "30", "0", "30/30"], strip                # stopped rows at horizon are not double counted
    tbls = pg.locator("#pfScr .pf-scr-tbl table")
    r25 = _tds(tbls.nth(0).locator("tbody tr").nth(2))
    assert r25[:5] == ["2.5× ATR DECISION", "30", "+0.21R", "+0.30R", "+0.20R"] and r25[-1] == "20"
    assert pg.locator("#pfScr tr.pre").count() == 2                        # 2.5x highlighted in both tables
    sizing = _tds(tbls.nth(1).locator("tbody tr").nth(2))
    dd1, dd2 = sizing[7], sizing[8]
    assert dd1 != "—" and dd2 != "—" and dd1 != dd2, "1% and 2% must differ only in the equity path"
    lens = _tds(tbls.nth(2).locator("tbody tr").first)
    assert lens[0] == "Tech" and lens[2] == "22" and lens[3] == "+0.40R"
    pg.fill("#pfScrEq", "50000"); pg.locator("#pfScrEq").evaluate("e=>e.dispatchEvent(new Event('change'))"); pg.wait_for_timeout(400)
    assert "$" in _tds(pg.locator("#pfScr .pf-scr-tbl table").nth(1).locator("tbody tr").nth(2))[3]
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_portfolio_screener_fits_a_phone(browser, served):
    pg, errs, _ = _book_page(browser, served, 390, 844, True, marked=True)
    assert pg.locator("#pfScr .pf-scr-tbl table").count() == 3
    assert pg.evaluate("document.documentElement.scrollWidth") <= 392
    assert not _bad(errs), errs[:2]


# ---------------------------------------------------------------- Auth (Supabase e-mail code / magic link)
import base64, time


def _jwt(email, exp):
    b64 = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return b64({"alg": "HS256"}) + "." + b64({"email": email, "exp": exp, "role": "authenticated"}) + ".sig"


EMAIL = "gabrielschmal@gmail.com"


def _auth_page(browser, base, w, h, mobile):
    pg = browser.new_page(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    now = int(time.time()); calls = {"otp": [], "verify": [], "refresh": 0, "logout": 0}
    def route(r):
        u = r.request.url
        if u.startswith(base): return r.continue_()
        if "/auth/v1/otp" in u:
            calls["otp"].append((u, json.loads(r.request.post_data))); return r.fulfill(status=200, content_type="application/json", body="{}")
        if "/auth/v1/verify" in u:
            body = json.loads(r.request.post_data); calls["verify"].append(body)
            if body.get("token") != "123456":
                return r.fulfill(status=403, content_type="application/json", body=json.dumps({"msg": "Token has expired or is invalid"}))
            return r.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"access_token": _jwt(EMAIL, now + 3600), "refresh_token": "rt1", "expires_in": 3600, "expires_at": now + 3600, "user": {"email": EMAIL}}))
        if "/auth/v1/token" in u:
            calls["refresh"] += 1
            return r.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"access_token": _jwt(EMAIL, now + 7200), "refresh_token": "rt2", "expires_in": 3600, "expires_at": now + 7200, "user": {"email": EMAIL}}))
        if "/auth/v1/logout" in u: calls["logout"] += 1; return r.fulfill(status=204, body="")
        if "/rest/v1/" in u: return r.fulfill(status=200, content_type="application/json", body="[]")
        return r.abort()
    pg.route("**/*", route)
    pg.goto(base + "/phoenix_app.html"); pg.wait_for_timeout(1200)
    pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(600)
    return pg, errs, calls, now


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_auth_code_flow_signs_in_and_writes_carry_the_session(browser, served):
    pg, errs, calls, now = _auth_page(browser, served, 1280, 900, False)
    assert "sb_publishable" in pg.evaluate("PX_AUTH.headers().Authorization"), "signed out: writes use the anon key"
    pg.click("#pxProf"); pg.wait_for_timeout(300)
    assert "Not signed in" in pg.locator("#pxAuthBox").inner_text()
    pg.click('#pxAuthBox [data-auth="in"]'); pg.wait_for_timeout(400)
    pg.fill("#mAuthEmail", EMAIL.upper()); pg.click("#mAuthOk"); pg.wait_for_timeout(600)
    url, body = calls["otp"][0]
    assert body == {"email": EMAIL, "create_user": True} and "redirect_to=" in url, "e-mail lower-cased, link returns to the app"
    assert pg.locator("#mAuthCodeRow").is_visible() and pg.locator("#mAuthOk").inner_text() == "Verify"
    pg.fill("#mAuthCode", "000000"); pg.click("#mAuthOk"); pg.wait_for_timeout(500)
    assert "Not accepted" in pg.locator("#mAuthNote").inner_text() and pg.locator("#trModalBg").count() == 1
    pg.fill("#mAuthCode", "12 34 56"); pg.click("#mAuthOk"); pg.wait_for_timeout(800)      # spaces tolerated
    assert calls["verify"][-1] == {"type": "email", "email": EMAIL, "token": "123456"}
    assert pg.locator("#trModalBg").count() == 0
    assert pg.evaluate("JSON.parse(localStorage.getItem('phoenix.auth')).user.email") == EMAIL
    assert pg.evaluate("window.PHOENIX_PROFILE") == "G", "the allowlisted address selects its own book"
    hdr = pg.evaluate("PX_AUTH.headers().Authorization")
    assert hdr.startswith("Bearer ey") and "sb_publishable" not in hdr, "signed in: writes carry the session token"
    pg.click("#pxProf"); pg.wait_for_timeout(300)
    assert EMAIL in pg.locator("#pxAuthBox").inner_text() and pg.evaluate("document.getElementById('pxProfAv').classList.contains('authed')")
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_auth_expired_token_refreshes_on_boot_and_sign_out_clears(browser, served):
    pg, errs, calls, now = _auth_page(browser, served, 1280, 900, False)
    pg.evaluate("localStorage.setItem('phoenix.auth', JSON.stringify({access_token:'%s',refresh_token:'rt1',expires_at:%d,user:{email:'%s'}}))" % (_jwt(EMAIL, now - 10), now - 10, EMAIL))
    pg.reload(); pg.wait_for_timeout(1500)
    assert calls["refresh"] >= 1 and pg.evaluate("JSON.parse(localStorage.getItem('phoenix.auth')).refresh_token") == "rt2"
    pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(500)
    pg.click("#pxProf"); pg.wait_for_timeout(300); pg.click('#pxAuthBox [data-auth="out"]'); pg.wait_for_timeout(400)
    assert calls["logout"] == 1 and pg.evaluate("localStorage.getItem('phoenix.auth')") is None
    assert "sb_publishable" in pg.evaluate("PX_AUTH.headers().Authorization")
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_auth_magic_link_return_is_parsed_on_full_load_and_on_fragment_navigation(browser, served):
    pg, errs, calls, now = _auth_page(browser, served, 390, 844, True)
    link = served + "/phoenix_app.html#access_token=%s&refresh_token=rt9&expires_in=3600&token_type=bearer&type=magiclink" % _jwt(EMAIL, now + 3600)
    pg.goto(link); pg.wait_for_timeout(1200)                       # same-document: popstate fires before hashchange
    assert pg.evaluate("JSON.parse(localStorage.getItem('phoenix.auth')||'{}').user?.email") == EMAIL
    assert pg.evaluate("location.hash") == "#mission", "the token must not survive in the URL, and the router must land on a real page"
    pg.evaluate("localStorage.removeItem('phoenix.auth')")
    pg2 = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    pg2.route("**/*", lambda r: r.continue_() if r.request.url.startswith(served) else r.fulfill(status=200, content_type="application/json", body="[]"))
    pg2.goto(link); pg2.wait_for_timeout(1500)                     # full load, as from Mail
    assert pg2.evaluate("JSON.parse(localStorage.getItem('phoenix.auth')||'{}').user?.email") == EMAIL
    assert pg2.evaluate("document.documentElement.scrollWidth") <= 392
    pg2.close()
    assert not _bad(errs), errs[:2]


# ---------------------------------------------------------------- Research -> Smart money
SM_Q = "2026-06-30"
def _c(m, a, q=SM_Q, sh=1000): return {"manager": m, "action": a, "shares": sh, "value_usd": 5e7, "shares_delta": 100, "pct_change": 10.0, "quarter": q}
SM_INST = {"asof": "2026-09-04 05:30 UTC", "latest_quarter": SM_Q, "managers": {
    "Berkshire Hathaway (Buffett)": {"quarters": [SM_Q, "2026-03-31"], "latest_in": True, "status": "ok", "edgar_name": "BERKSHIRE HATHAWAY INC"},
    "Pershing Square (Ackman)": {"quarters": ["2026-03-31"], "latest_in": False, "status": "ok"},
    "Appaloosa (Tepper)": {"quarters": [SM_Q, "2026-03-31"], "latest_in": True, "status": "ok"},
    "Greenlight (Einhorn)": {"quarters": ["2023-12-31"], "latest_in": False, "status": "STALE: newest 13F-HR is 2023-12-31", "latest_13f": "2023-12-31"}},
    "tickers": {"AAPL": [_c("Berkshire Hathaway (Buffett)", "HOLD"), _c("Appaloosa (Tepper)", "NEW"), _c("Berkshire Hathaway (Buffett)", "TRIM", "2026-03-31")],
                "NVDA": [_c("Appaloosa (Tepper)", "ADD")], "MSFT": [_c("Berkshire Hathaway (Buffett)", "EXIT", sh=0)], "COF": [_c("Appaloosa (Tepper)", "EXIT", sh=0)]}}
SM_CONG = {"asof": "2026-09-03", "status": {"sources": {"House Clerk PTR": {"rows": 9000, "last_30d": 0, "newest_tx": "2026-08-05", "newest_filed": "2026-08-12"}}, "quiet": ["House Clerk PTR"], "absent_chambers": ["Senate"]},
           "tickers": {"NVDA": [{"date": "2026-08-01", "member": "Nancy Pelosi", "state": "CA11", "chamber": "House", "side": "buy", "amount": "$1,000,001 - $5,000,000", "reported": "2026-08-12"}]}}


def _sm_page(browser, base, w, h, mobile):
    pg = browser.new_page(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    def route(r):
        u = r.request.url
        if u.startswith(base): return r.continue_()
        if "institutional_holdings" in u: return r.fulfill(status=200, content_type="application/json", body=json.dumps(SM_INST))
        if "congress_trades" in u: return r.fulfill(status=200, content_type="application/json", body=json.dumps(SM_CONG))
        if "research_index" in u: return r.fulfill(status=200, content_type="application/json", body=json.dumps({"reports": []}))
        if "/rest/v1/" in u or "snapshots_latest" in u: return r.fulfill(status=200, content_type="application/json", body="[]")
        return r.abort()
    pg.route("**/*", route)
    pg.goto(base + "/phoenix_app.html"); pg.wait_for_timeout(1200)
    pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(600)
    pg.evaluate("location.hash='#smart'"); pg.wait_for_timeout(1800)
    return pg, errs


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_smart_money_grid_shows_filings_as_filed_with_honest_freshness(browser, served):
    pg, errs = _sm_page(browser, served, 1280, 900, False)
    assert pg.evaluate("document.getElementById('page-smart').classList.contains('active')"), "Smart money is its own page"
    fresh = pg.locator("#rsSmart .sm-fresh").inner_text()
    assert "2026-06-30" in fresh and "gone quiet" in fresh and "no Senate rows" in fresh
    pg.click('#rsSmart button[data-smv="famous"]'); pg.wait_for_timeout(600)
    tbl = pg.locator("#rsSmart .sm-tbl").first
    rows = {r.locator("td.sm-first").inner_text().split("\n")[0]: [x.strip() for x in r.locator("td:not(.sm-first)").all_inner_texts()] for r in tbl.locator("tbody tr").all()}
    cols = [h.strip() for h in tbl.locator("thead th").all_inner_texts()][1:]
    assert cols == ["AAPL", "COF", "MSFT", "NVDA"]
    assert rows["Appaloosa (Tepper)"] == ["N", "×", "", "+"] and rows["Berkshire Hathaway (Buffett)"] == ["=", "", "×", ""]
    chips = {r.locator("td.sm-first b").inner_text(): r.locator("td.sm-first .sm-chip").inner_text() for r in tbl.locator("tbody tr").all()}
    assert chips["Pershing Square (Ackman)"] == "Q2 2026 not in yet" and chips["Greenlight (Einhorn)"].startswith("STALE")
    assert pg.evaluate("getComputedStyle(document.querySelector('#rsSmart button.sm-g.sm-n')).color") == "rgb(255, 255, 255)"
    pg.locator('#rsSmart button.sm-g[data-tk="AAPL"][data-mgr="Appaloosa (Tepper)"]').click(); pg.wait_for_timeout(400)
    assert "Appaloosa" in pg.locator("#trModalBg .tr-modal-title").inner_text() and "not a trade price" in pg.locator("#trModalBg").inner_text()
    pg.click("#trModalBg .cancel"); pg.wait_for_timeout(300)
    cong = pg.locator("#rsSmart .sm-tbl").nth(1)
    assert cong.locator("tbody tr").count() == 1 and "Nancy Pelosi" in cong.inner_text()
    pg.evaluate("location.hash='#research'"); pg.wait_for_timeout(600)
    assert pg.evaluate("document.getElementById('page-research').classList.contains('active')")
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_smart_money_fits_a_phone(browser, served):
    pg, errs = _sm_page(browser, served, 390, 844, True)
    pg.click('#rsSmart button[data-smv="famous"]'); pg.wait_for_timeout(600)
    assert pg.locator("#rsSmart .sm-tbl").first.locator("tbody tr").count() == 4
    assert pg.evaluate("document.documentElement.scrollWidth") <= 392
    assert not _bad(errs), errs[:2]


# ---------------------------------------------------------------- profiles, follow list, MC digest
SM_META_FIX = {"asof": "2026-09-04", "committee_sectors": {"Armed Services": ["Electronic technology"], "Financial Services": ["Finance"]},
    "members": {"nancypelosi": {"name": "Nancy Pelosi", "party": "Democrat", "state": "CA", "district": 11, "chamber": "House", "committees": [], "term_end": "2027-01-03"},
                "dannewhouse": {"name": "Dan Newhouse", "party": "Republican", "state": "WA", "district": 4, "chamber": "House", "committees": ["House Committee on Armed Services"]},
                "donaldjtrump": {"name": "Donald J. Trump", "chamber": "Executive", "role": "President of the United States", "committees": [], "no_transaction_feed": True,
                                 "disclosure": "Annual OGE Form 278e only: asset ranges, no transactions, assets held in a trust."}}}
SM_STOCKS = {"stocks": [{"ticker": "NVDA", "sector": "Electronic technology"}, {"ticker": "AAPL", "sector": "Electronic technology"}, {"ticker": "BAC", "sector": "Finance"}]}
SM_CONG2 = {"asof": "2026-09-03", "status": {"sources": {"House Clerk PTR": {"rows": 9, "last_30d": 2, "newest_tx": "2026-09-01", "newest_filed": "2026-09-02"}}, "quiet": [], "absent_chambers": ["Senate"]},
    "tickers": {"NVDA": [{"date": "2026-09-01", "member": "Nancy Pelosi", "state": "CA11", "chamber": "House", "side": "buy", "amount": "$1,000,001 - $5,000,000", "reported": "2026-09-02"}],
                "AAPL": [{"date": "2026-08-28", "member": "Dan Newhouse", "state": "WA04", "chamber": "House", "side": "sell", "amount": "$15,001 - $50,000", "reported": "2026-09-01"}]}}


def _prof_page(browser, base, w, h, mobile):
    pg = browser.new_page(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e))); state = {"watch": [], "posts": 0, "deletes": 0}
    def route(r):
        u, m = r.request.url, r.request.method
        if u.startswith(base): return r.continue_()
        if "institutional_holdings" in u: return r.fulfill(status=200, content_type="application/json", body=json.dumps(SM_INST))
        if "congress_meta" in u: return r.fulfill(status=200, content_type="application/json", body=json.dumps(SM_META_FIX))
        if "congress_trades" in u: return r.fulfill(status=200, content_type="application/json", body=json.dumps(SM_CONG2))
        if "stocks.json" in u: return r.fulfill(status=200, content_type="application/json", body=json.dumps(SM_STOCKS))
        if "watch_entities" in u:
            if m == "POST": state["posts"] += 1; state["watch"].append(json.loads(r.request.post_data)); return r.fulfill(status=201, body="")
            if m == "DELETE": state["deletes"] += 1; state["watch"].clear(); return r.fulfill(status=204, body="")
            return r.fulfill(status=200, content_type="application/json", body=json.dumps(state["watch"]))
        if "research_index" in u: return r.fulfill(status=200, content_type="application/json", body=json.dumps({"reports": []}))
        if "/rest/v1/" in u or "snapshots_latest" in u: return r.fulfill(status=200, content_type="application/json", body="[]")
        return r.abort()
    pg.route("**/*", route)
    pg.goto(base + "/phoenix_app.html"); pg.wait_for_timeout(1200)
    pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(800)
    return pg, errs, state


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_mission_control_digest_reports_new_filings_without_searching(browser, served):
    pg, errs, state = _prof_page(browser, served, 1280, 900, False)
    pg.evaluate("location.hash='#mission'"); pg.wait_for_timeout(2200)
    card = pg.locator("#mcSmart"); assert card.count() == 1
    txt = card.inner_text()
    assert "Nancy Pelosi" in txt and "bought" in txt and "NVDA" in txt, txt[:200]
    assert "Q2 2026" in txt and "not in yet" in txt and "Pershing" in txt
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_member_profile_flags_committee_jurisdiction_and_follow_round_trips(browser, served):
    pg, errs, state = _prof_page(browser, served, 1280, 900, False)
    pg.evaluate("location.hash='#smart'"); pg.wait_for_timeout(1800)
    mem = pg.locator('#rsSmart .sm-who[data-kind="member"]')
    for i in range(mem.count()):
        if "Newhouse" in mem.nth(i).inner_text(): mem.nth(i).click(); break
    pg.wait_for_timeout(700)
    t = pg.locator("#trModalBg").inner_text()
    assert "House Committee on Armed Services" in t and "Electronic technology" in t
    assert "1 sector traded here" in t and "not an allegation" in t
    assert "$33k" in t, "range midpoint, not an invented exact size"
    assert "Net worth and performance" in t, "the missing pieces are named, not faked"
    pg.locator("#trModalBg [data-follow]").click(); pg.wait_for_timeout(800)
    assert state["posts"] == 1 and state["watch"] == [{"profile": "G", "kind": "member", "name": "Dan Newhouse"}]
    assert "Following" in pg.locator("#trModalBg [data-follow]").inner_text()
    pg.locator("#trModalBg [data-follow]").click(); pg.wait_for_timeout(800)
    assert state["deletes"] == 1 and state["watch"] == []
    pg.click("#trModalBg .cancel"); pg.wait_for_timeout(300)
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_fund_profile_and_executive_branch_absence_is_stated(browser, served):
    pg, errs, state = _prof_page(browser, served, 1280, 900, False)
    pg.evaluate("location.hash='#smart'"); pg.wait_for_timeout(1800)
    pg.click('#rsSmart button[data-smv="famous"]'); pg.wait_for_timeout(700)
    pg.locator('#rsSmart .sm-who[data-kind="fund"]').first.click(); pg.wait_for_timeout(700)
    t = pg.locator("#trModalBg").inner_text()
    tl = t.lower()      # the stat labels are upper-cased by CSS, so compare case-insensitively
    assert "warren buffett" in tl and "berkshire hathaway inc" in tl and "disclosed" in tl
    assert "not trade prices" in tl and "slice of the book" in tl
    pg.click("#trModalBg .cancel"); pg.wait_for_timeout(300)
    ex = pg.locator("#rsSmart .w-card").last
    assert "Executive branch" in ex.inner_text() and "no transaction feed exists" in ex.inner_text()
    ex.locator('.sm-who[data-kind="member"]').first.click(); pg.wait_for_timeout(700)
    t = pg.locator("#trModalBg").inner_text()
    assert "OGE Form 278e" in t and "no transactions" in t.lower()
    assert pg.locator("#trModalBg [data-follow]").count() == 0, "nothing to follow: there is no feed"
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_profiles_fit_a_phone(browser, served):
    pg, errs, state = _prof_page(browser, served, 390, 844, True)
    pg.evaluate("location.hash='#smart'"); pg.wait_for_timeout(1800)
    pg.locator('#rsSmart .sm-who[data-kind="fund"]').first.click(); pg.wait_for_timeout(700)
    assert pg.evaluate("document.documentElement.scrollWidth") <= 392
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_smart_money_is_a_first_class_page_not_a_research_tab(browser, served):
    """It was buried behind a switch inside Research. It is now a rail entry and a real address."""
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith(served)
             else r.fulfill(status=200, content_type="application/json", body="[]"))
    pg.goto(served + "/phoenix_app.html"); pg.wait_for_timeout(1200)
    pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(800)
    labels = [a.strip() for a in pg.locator("#pxnav .pxlinks a").all_inner_texts()]
    assert labels == ["Mission Control", "Launch Control", "Markets", "Screeners", "Smart Money",
                      "Trade", "Portfolio", "Research"]
    pg.click("#pxl_smart"); pg.wait_for_timeout(900)
    assert pg.evaluate("location.hash") == "#smart"
    assert pg.evaluate("document.getElementById('page-smart').classList.contains('active')")
    assert pg.evaluate("document.getElementById('pxl_smart').classList.contains('on')")
    pg.reload(); pg.wait_for_timeout(1400); pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(700)
    assert pg.evaluate("document.getElementById('page-smart').classList.contains('active')"), "#smart must be reloadable"
    assert pg.locator("#rsMode").count() == 0, "the Research mode switch is gone"
    pg.close()


# ---------------------------------------------------------------- Launch Control
LC_GEX = {"overview": {"spx_spot": 7747.71, "net_gex_B": -2.1, "flip": 7723, "dist_to_flip_pct": 0.31,
                       "call_wall": 7800, "put_wall": 7700}}
LC_MACRO = {"asof": "2026-09-04 05:33 UTC", "regime": "POLICY_TIGHTENING", "explain": {"held_weeks": 1},
            "inputs": {"spx": 7747.71, "spx_1d": 1.06, "vix": 14.32, "us10y": 4.77, "gold": 4539.9, "wti": 91.3}}
LC_TB = [{"id": "t1", "account": "gabriel", "status": "open", "ticker": "GOOG", "name": "Alphabet",
          "entry": 325.86, "stop": 305.67, "target": 420, "qty": 30, "account_size": 100000},
         {"id": "t2", "account": "gabriel", "status": "open", "ticker": "BAC", "name": "Bank of America",
          "entry": 64.38, "stop": 60.39, "target": 75.03, "qty": 150, "account_size": 100000},
         {"id": "t3", "account": "gabriel", "status": "open", "ticker": "PLTR", "name": "Palantir",
          "entry": 184, "qty": 10, "account_size": 100000}]
LC_LIVE = [{"ticker": t, "last": p, "prev_close": p, "chg_pct": 0,
            "quote_ts": "2026-09-04T13:00:00Z", "updated_at": "2026-09-04T13:00:00Z"}
           for t, p in [("GOOG", 339.08), ("BAC", 63.04), ("PLTR", 182.53)]]


def _lc_page(browser, base, w, h, mobile, scheme="dark", sig=None):
    pg = browser.new_page(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile, color_scheme=scheme)
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    def route(r):
        u = r.request.url
        if u.startswith(base): return r.continue_()
        if "gex" in u and "stocks" not in u: return r.fulfill(status=200, content_type="application/json", body=json.dumps(LC_GEX))
        if "trade_book" in u:
            return r.fulfill(status=200, content_type="application/json",
                             body=json.dumps([{"id": t["id"], "ord": i, "body": t, "deleted": False} for i, t in enumerate(LC_TB)]))
        if "prices_live" in u: return r.fulfill(status=200, content_type="application/json", body=json.dumps(LC_LIVE))
        if "signal_log" in u: return r.fulfill(status=200, content_type="application/json", body=json.dumps(sig if sig is not None else []))
        if "macro" in u: return r.fulfill(status=200, content_type="application/json", body=json.dumps(LC_MACRO))
        if "/rest/v1/" in u or ".json" in u: return r.fulfill(status=200, content_type="application/json", body="[]")
        return r.abort()
    pg.route("**/*", route)
    pg.goto(base + "/phoenix_app.html"); pg.wait_for_timeout(1400)
    pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(900)
    pg.evaluate("document.getElementById('pxl_launch').click()"); pg.wait_for_timeout(2600)
    return pg, errs


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_launch_control_leads_with_decisions_and_names_the_unsized(browser, served):
    pg, errs = _lc_page(browser, served, 1440, 1000, False)
    assert pg.evaluate("location.hash") == "#launch"
    al = pg.locator("#lcRoot .lc-al")
    assert al.count() == 3
    first = al.first
    assert first.locator(".tk").inner_text() == "PLTR", "the position with no stop is the costliest to ignore"
    assert first.locator(".act").inner_text().upper() == "SET STOP"
    assert "cannot be read in R" in first.locator(".why").inner_text()
    acts = [a.inner_text().upper() for a in al.locator(".act").all()]
    assert acts.count("HOLD") == 2, "a position needing nothing must still say so"
    assert pg.locator("#lcRoot .lc-pos").count() == 3
    assert pg.locator("#lcRoot .lc-pos.new .lc-rbar.flat").count() == 1, "unsized positions get a flat rail, not a fake one"
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_launch_control_heat_is_in_R_not_account_percent(browser, served):
    """1R is 1% of the account. Dividing risk$ by the account gave 0.01R for a 1.2R book."""
    pg, errs = _lc_page(browser, served, 1440, 1000, False)
    heat = pg.locator("#lcRoot .lc-heat .num").inner_text()
    assert heat.startswith("1.2"), heat        # GOOG 0.61R + BAC 0.60R, PLTR unsized contributes nothing
    assert "3R cap" in heat
    note = pg.locator("#lcRoot .lc-note").first.inner_text()
    assert "1 of 3 positions carries no stop" in note and "cannot be read in R" in note
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_launch_control_ladder_reads_the_flip_against_spot(browser, served):
    pg, errs = _lc_page(browser, served, 1440, 1000, False)
    lad = pg.locator("#lcRoot .lc-lrow")
    assert lad.count() == 4
    order = [r.locator(".p").inner_text() for r in lad.all()]
    assert order == ["7,800", "7,747.71", "7,723", "7,700"], "descending by price, spot in its true place"
    assert pg.locator("#lcRoot .lc-lrow.spot").count() == 1 and pg.locator("#lcRoot .lc-lrow.flip").count() == 1
    txt = pg.locator("#lcRoot .lc-card").last.inner_text() + pg.content()
    assert "above the flip" in txt and "dampens" in txt
    regime = [x.strip() for x in pg.locator("#lcRoot .lc-reg .v").all_inner_texts()]
    assert regime[0] == "POLICY_TIGHTENING" and "2.1bn" in regime[1]
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_launch_control_survives_a_malformed_signal_log(browser, served):
    """A non-array response used to throw and blank the entire page."""
    pg, errs = _lc_page(browser, served, 1440, 1000, False, sig={"unexpected": "shape"})
    assert pg.locator("#lcRoot .lc-pos").count() == 3, "the book still renders"
    assert pg.locator("#lcRoot .lc-al").count() == 3
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_launch_control_fits_a_phone_and_the_tab_bar_stays_reachable(browser, served):
    pg, errs = _lc_page(browser, served, 390, 844, True)
    assert pg.locator("#lcRoot .lc-pos").count() == 3
    assert pg.evaluate("document.documentElement.scrollWidth") <= 392
    pg.evaluate("window.scrollTo(0,1500)"); pg.wait_for_timeout(400)
    nav = pg.locator("#pxnav").bounding_box()
    assert pg.evaluate("getComputedStyle(document.getElementById('pxnav')).position") == "fixed"
    assert nav["y"] + nav["height"] <= 850, "the bottom bar must stay on screen while scrolling"
    assert not _bad(errs), errs[:2]


@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_theme_follows_the_device_until_the_operator_chooses(browser, served):
    """It hard-defaulted to light, so an iPad in dark mode opened Phoenix in cream every day."""
    for scheme, want_light in (("dark", False), ("light", True)):
        pg = browser.new_page(viewport={"width": 1280, "height": 900}, color_scheme=scheme)
        pg.route("**/*", lambda r: r.continue_() if r.request.url.startswith(served)
                 else r.fulfill(status=200, content_type="application/json", body="[]"))
        pg.goto(served + "/phoenix_app.html"); pg.wait_for_timeout(1300)
        pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(700)
        assert pg.evaluate("document.documentElement.classList.contains('light')") is want_light, scheme
        pg.close()
