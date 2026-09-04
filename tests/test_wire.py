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
