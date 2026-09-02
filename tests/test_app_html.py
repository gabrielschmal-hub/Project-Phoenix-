"""Browser tests for phoenix_app.html + portfolio_builder.html. Network is blocked: layout only.

2 Sep 2026: the iPhone sweep used to visit '#trade', which is not a route — the page is '#trades' —
so the Trade page was never inside the overflow check. Fixed, and the Screener tab is now covered
at 390 and 1280 with mocked signal_log rows in every state the card can render.
"""
import json, pathlib, pytest
from playwright.sync_api import sync_playwright
ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "phoenix_app.html"; BUILDER = ROOT / "portfolio_builder.html"
PAGES = ("home", "markets", "screeners", "trades", "portfolio", "research")


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
    assert pg.locator("#pxnav .pxlinks a").count() == 6
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
