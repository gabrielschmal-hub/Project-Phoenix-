"""Browser tests for phoenix_app.html + portfolio_builder.html. Network is blocked: layout only."""
import pathlib, pytest
from playwright.sync_api import sync_playwright
ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "phoenix_app.html"; BUILDER = ROOT / "portfolio_builder.html"

@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        b = pw.chromium.launch(); yield b; b.close()

def _page(browser, w, h, mobile):
    pg = browser.new_page(viewport={"width": w, "height": h}, is_mobile=mobile, has_touch=mobile)
    pg.route("**/*", lambda r: r.abort() if not r.request.url.startswith("file://") else r.continue_())
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e))); return pg, errs

@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_app_iphone_layout(browser):
    pg, errs = _page(browser, 390, 844, True); pg.goto(APP.as_uri()); pg.wait_for_timeout(1500)
    assert 'name="viewport"' in pg.content()
    pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(1000)
    for hsh in ("home", "markets", "screeners", "trade", "portfolio", "research"):
        pg.evaluate(f"location.hash='#{hsh}'"); pg.wait_for_timeout(900)
        assert pg.evaluate("document.documentElement.scrollWidth") <= 392, f"#{hsh} overflows"
    bar = pg.locator("#pxnav").bounding_box(); assert bar["y"] > 700 and bar["width"] >= 388, "rail must be a bottom bar on phones"
    assert pg.locator("#pxnav .pxlinks a").count() == 6
    assert not [e for e in errs if "SyntaxError" in e or "ReferenceError" in e], errs[:2]

@pytest.mark.skipif(not APP.exists(), reason="phoenix_app.html not in repo root")
def test_app_desktop_unchanged(browser):
    pg, errs = _page(browser, 1280, 900, False); pg.goto(APP.as_uri()); pg.wait_for_timeout(1400)
    pg.evaluate("PX_ENTER()"); pg.wait_for_timeout(900)
    r = pg.locator("#pxnav").bounding_box(); assert r["x"] == 0 and r["height"] > 700, "desktop rail stays on the left"
    assert pg.evaluate("document.documentElement.scrollWidth") <= 1280

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
