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
