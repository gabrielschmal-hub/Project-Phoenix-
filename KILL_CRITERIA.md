# Kill criteria — pre-registered 2 Sep 2026, before any signal was marked

Applies to **new** screener signals only (first appearance in 5 sessions). A name sitting on the
list for ten days is one event, not ten. Evaluated on 20-day forward returns, in units of initial
risk R = 2.5 x ATR(14) as % of close. Computed by `signal_log.py report`.

**Sample:** verdict is INSUFFICIENT until 30 new signals are marked. Do not read the numbers before that.

**STOP trading the screener if, at n >= 30, any of:**
1. E_price (mean 20-day return / R) is below **+0.054R**. Zero is not the bar.
2. The 2.5 x ATR stop is hit inside 20 days on more than **60%** of signals.
3. Median MFE is below **1R** — signals that never reach breakeven cannot be managed by any exit rule.

**CONTINUE** only if all three pass. Re-evaluate every 30 new signals. These thresholds do not move
after the fact; changing them requires a dated entry here explaining why, written before the next report.

Not yet in the numbers, by design: commissions, slippage, FX, tax. They only make E worse, so a STOP
here is a STOP; a CONTINUE here is provisional until they are added.

---

## Entry 2 Sep 2026 — three-ATR study added; decision variable unchanged

Written the same day the criteria were registered, before any signal was marked (n marked = 0).

**What changed in the report.** Each signal is now walked bar by bar under 1.5x, 2.0x and 2.5x
ATR stops, recording which of the stop, the 2:1 target and the 3:1 target was touched *first*.
Within a bar the stop is assumed to resolve first. The report prints all three multiples and three
exit policies (stop only, 2:1, 3:1) side by side.

**What did not change.** The verdict is computed on **2.5x ATR, stop only, held to 20 bars** — the
column this document registered above. Thresholds 1–3 are unchanged. `signal_log.py` names this in
`scorecard()["decision_variable"]` and will not pick a different column.

**Why this entry exists.** Three multiples and three policies are nine cells. With n = 30, one of
nine cells clearing +0.054R by chance is likely even if the screener has no edge. So:

- 1.5x and 2.0x, and the 2:1 / 3:1 policies, are **descriptive**. They cannot rescue a STOP.
- If a different multiple is ever to become the decision variable, that is a *new* pre-registration:
  a dated entry here naming the multiple and the policy, written **before** the next 30 signals are
  marked, and evaluated only on signals marked after that date. Signals already seen do not count.

**Entry price.** The day-one audit found `close` was null on every row because `screener_triggers`
carries no close. The entry is now the close of the signal bar, taken from the same daily series as
the forward returns. The provisional `entry_ref` shown in the app is never used here.

**Signal date.** `screener_triggers` accumulates across days. Rows are now dated by their own asof,
not by the day the log ran. The 163 rows appended on 2 Sep with the wrong date are to be deleted and
re-appended; nothing had been marked.

**Lenses (added the same day, still n marked = 0).** The nightly report now cuts the marked
signals eleven ways: sector, industry, profitability, market cap, ATR width, engine regime, GEX
regime, SPX vs its 200-day mean, breakout, days on list, time in trade. Cells under n = 20 are
flagged thin. None of this is a decision variable. A cell that looks good is a hypothesis; the path
to a rule is the one written above: a dated entry naming the cut, then a fresh sample logged after
that date. The signals that suggested the cut can never be the ones that confirm it.

**Regime provenance.** The engine's snapshot carried a null regime on every day before 2 Sep 2026
because the stocks step passed a key that never existed. Fixed at the source. The six earlier days
keep a null engine regime — not reconstructed. Their SPX state (close, distance to the 50- and
200-day means) is latched by the mark step from the ^GSPC series, which is objective.
