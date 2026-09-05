"""The wire step's file discovery.

4 Sep 2026: the morning chat began writing phoenix_wire_gabriel_2026-09-04.html with
underscores. The glob looked for "phoenix-*-*.html", so those files did not match and were
never even listed as skipped. The step published the 1 Sep brief for three days, green every
time. These tests pin the discovery rules so a brief can never go missing silently again.
"""
import os, re, sys, glob, textwrap, pathlib, pytest
ROOT = pathlib.Path(__file__).resolve().parent.parent


def _discover(dirname):
    """The exact rules run_wire uses, extracted from phoenix.py so the test cannot drift."""
    src = (ROOT / "phoenix.py").read_text()
    i = src.index("    files = [f for f in _g.glob(_os.path.join(WIRE_DIR")
    j = src.index("    print(f\"[wire] {len(files)} file(s)")
    body = src[i:j]
    body = textwrap.dedent(body).replace("    return", "    raise SystemExit")
    ns = {"_g": glob, "_os": os, "_re": re, "WIRE_DIR": dirname, "print": lambda *a, **k: None}
    exec("import glob as _g, re as _re, os as _os\n" + body, ns)
    return ns["best"], ns["best_wk"], ns["skipped"]


def _mk(tmp_path, names):
    d = tmp_path / "wire"; d.mkdir()
    for n in names: (d / n).write_text("x")
    return str(d)


def test_underscore_dailies_are_found(tmp_path):
    """The exact filenames that were in the repo on 4 Sep."""
    d = _mk(tmp_path, ["phoenix_wire_gabriel_2026-09-04.html", "phoenix_wire_aldemar_2026-09-04.html",
                       "phoenix-wire-gabriel-2026-09-01.html"])
    best, wk, skipped = _discover(d)
    assert best["gabriel"][0] == "2026-09-04", "the newest daily must win regardless of separator"
    assert best["aldemar"][0] == "2026-09-04"
    assert skipped == []


def test_hyphen_weeklies_still_work(tmp_path):
    d = _mk(tmp_path, ["phoenix-weekly-gabriel-2026-08-31.html", "phoenix-weekly-gabriel-2026-08-22.html"])
    _, wk, _ = _discover(d)
    assert wk["gabriel"][0] == "2026-08-31"


def test_a_name_that_cannot_be_parsed_is_reported_not_ignored(tmp_path):
    d = _mk(tmp_path, ["Phoenix Wire Gabriel 2026-09-05.html", "phoenix_wire_gabriel_2026-09-04.html"])
    best, _, skipped = _discover(d)
    assert len(skipped) == 1 and "Phoenix Wire Gabriel" in skipped[0], "spaces must be surfaced, not swallowed"
    assert best["gabriel"][0] == "2026-09-04"


def test_non_phoenix_files_are_left_alone(tmp_path):
    d = _mk(tmp_path, ["wire_sample.json", "README.md", "phoenix_wire_gabriel_2026-09-04.html"])
    best, _, skipped = _discover(d)
    assert skipped == [] and set(best) == {"gabriel"}


def test_mixed_case_and_both_separators(tmp_path):
    d = _mk(tmp_path, ["Phoenix_Wire_Gabriel_2026-09-04.html", "phoenix-wire-aldemar-2026-09-03.html"])
    best, _, skipped = _discover(d)
    assert set(best) == {"gabriel", "aldemar"} and skipped == []


# ---------------------------------------------------------------- Mission Control brief sections
def _parse(raw):
    src = (ROOT / "phoenix.py").read_text()
    i = src.index("def _wire_strip"); j = src.index("\ndef _wire_sanitize")
    ns = {}; exec(src[i:j], ns)
    return ns["_wire_parse_html"](raw)


def _sec(kicker, title, body, items="", right="r"):
    return ('<div class="ed-sec"><div class="ed-k"><span>' + kicker + '</span>'
            '<span class="ed-n">' + right + '</span></div>'
            '<h2 class="ed-hl">' + title + '</h2>' + items +
            '<p class="ed-body">' + body + '</p></div>')


def test_every_mission_control_section_routes_to_its_own_slot():
    raw = ('<div class="ed-call">Payrolls decide the week</div>'
           '<div class="ed-vwhy">A hot print flips the index.</div>'
           + _sec("IN FIVE MINUTES", "Tightening holds", "Three things matter.")
           + _sec("THE STANCE", "Constructive but unconvinced", "Held 6 days.")
           + _sec("THE MARKET", "SPX above the flip", "VIX 14.3.")
           + _sec("SMART MONEY", "Energy in, software out", "Appaloosa opened CRNX.")
           + _sec("THE SCREENER", "Nine new, two clean", "CRNX and DINO.")
           + _sec("YOUR POSITIONS", "Two live, one unsized", "PLTR has no stop.",
                  '<div class="ed-w"><span class="w-tag">HOLD</span><span><b>GOOG</b> +0.65R</span>'
                  '<span class="w-when">now</span></div>')
           + _sec("THE WORLD", "Payrolls, then CPI", "Rates dominate.")
           + _sec("WHAT CHANGED", "Gamma flipped negative", "It was positive yesterday.")
           + _sec("WHAT WOULD CHANGE THIS", "A close above 7,800", "Or VIX through 20.")
           + '<p class="ed-body"><b>On deck.</b> CPI 11 Sep</p>')
    o = _parse(raw)
    for slot, title in (("recap", "Tightening holds"), ("stance", "Constructive but unconvinced"),
                        ("markets", "SPX above the flip"), ("smart_money", "Energy in, software out"),
                        ("screener", "Nine new, two clean"), ("positions", "Two live, one unsized"),
                        ("world", "Payrolls, then CPI"), ("changed", "Gamma flipped negative"),
                        ("invalidation", "A close above 7,800")):
        assert o[slot], "slot not filled: " + slot
        assert o[slot]["title"] == title, slot
    assert o["headline"] == "Payrolls decide the week" and o["ondeck"] == "CPI 11 Sep"
    assert o["positions"]["items"][0]["ticker"] == "GOOG", "a leading <b>TICKER</b> becomes tappable"


def test_an_unplanned_section_is_kept_as_a_theme_not_dropped():
    o = _parse(_sec("SHIPPING RATES", "Firm into Q4", "Not one of the nine kickers."))
    assert o["themes"] and o["themes"][0]["kicker"] == "SHIPPING RATES"
    assert all(o[s] is None for s in ("recap", "stance", "markets", "changed"))


def test_kicker_matching_tolerates_the_variants_a_writer_will_use():
    for kicker, slot in (("Five minutes", "recap"), ("The recap", "recap"),
                         ("MARKETS", "markets"), ("Phoenix's stance", "stance"),
                         ("Opportunities", "screener"), ("Positioning", "smart_money"),
                         ("Invalidation", "invalidation")):
        o = _parse(_sec(kicker, "T", "B"))
        assert o[slot], f"{kicker!r} should route to {slot}"


def test_missing_sections_stay_none_so_the_page_can_omit_them():
    o = _parse(_sec("THE MARKET", "Only one section", "Everything else absent."))
    assert o["markets"]["title"] == "Only one section"
    assert o["recap"] is None and o["invalidation"] is None and o["positions"] is None
