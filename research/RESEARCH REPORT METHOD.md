# RESEARCH REPORT METHOD
*v1 · 22 August 2026 · how to build a Phoenix research report fast and identically every time*

**The one-line trigger.** Say: **"Phoenix report on <TICKER>"** and this document governs everything below. Add `L1`, `L2` or `R` to change the depth (see §2). Add `no-gex` or `no-news` to drop a section.

---

## 1 · THE DATA RUN — always in this order

Never write before this completes. Every number in the report comes from here, not from memory.

| # | Call | Gives |
|---|---|---|
| 1 | `search_contracts(TICKER)` | `underlying_contract_id` — take the row where **symbol matches exactly** and country is US |
| 2 | `get_price_snapshot` with `last, bid_ask, prior_close, change, volume, high, low, open, misc_statistics, historical_vol, implied_vol_underlying, implied_volatility_percentile, underlying_today_option_volume, underlying_avg_option_volume, year_to_date_change, avg_90d_usd_volume` | spot, day range, 13/26/52w hi-lo, IV, HV, IV percentile, option volume skew, YTD |
| 3 | `get_price_history` `ONE_YEAR / ONE_DAY` | 251 daily bars → ATR-14, RSI-14, chart shape |
| 3b | `get_price_history` `ONE_WEEK / step_count 115` | **weekly bars — the gates run on these, not daily** |
| 4 | `get_option_parameters` → `get_option_data` on **the nearest weekly, the next monthly, and the furthest listed monthly**, `min_strike`/`max_strike` = spot ±60% | strike ladder + contract ids for the **full chain** |
| 5 | `get_price_snapshot(option_open_interest)` per strike across all three expiries | call OI / put OI for the full-chain GEX |
| 6 | `search_contracts` + `get_price_snapshot` for the **sector ETF** and **1–2 named peers** | relative strength |
| 7 | `web_search` ×4 — earnings/guidance · news/ratings · **business model, customers, partnerships, history** · **"<TICKER> partnership deal announcement <current month> <year>"** | fundamentals, news, business section, and **deals struck in the last fortnight** |

**The fourth search is not optional.** An earnings-focused search returns the quarter and misses everything after it. Uber announced the Zipline drone partnership — a new business line targeting a million deliveries a day — **four days before the data date**, and a Q2-and-ratings search surfaced none of it. Always run a dated search for the current month.

**Rules for the run**
- IBKR **beats** web search on every number it can supply. Web search once corrected a 200-day of $17.22 that IBKR put at $20.51 — a difference that flipped a gate.
- If per-contract IV returns `isValid:false`, use `implied_vol_underlying.annual_iv` flat across strikes **and say so in the footer**.
- Snapshot `last` and history `close` can differ by a few cents. Use the **history close** as the official close and note the last trade if they diverge.

---

## 2 · DEPTH — declare it in the masthead

| tag | scope | length |
|---|---|---|
| **L1** theme | megatrend, bottleneck, 3–5 value-chain nodes, candidates across all | 6 sections |
| **L2** node screen | 1–2 nodes, ~6 names, same framework applied identically, scored against peers | 8 sections |
| **L3** single name *(default)* | one company, full stack below | 12 sections |
| **R** rotation | leadership changing: concentration → valuation gap → breadth | different shape entirely |

**Most bad reports are an L1 argument with an L3 conclusion.** State the depth and do not conclude below the depth actually researched.

---

## 3 · SECTION ORDER — fixed, lettered A–L

Letters not numbers: numbers imply a sequence the content does not have.

**01 · The business** — what it sells, who pays, how it got here. Three segment cards (product, customer, supplier), a dated milestone list, a five-year growth table, and a **signed-and-shipped** card splitting enterprise deals from product launches. Numbered sections, no table of contents — the rail carries the live stat block only.
**02 · Verdict & tape** — headline as a thesis, lede, verdict box, 8-cell metric strip. *The verdict goes first so the reader can judge whether the evidence supports it or was assembled to fit.*
**B · Gamma structure** — Phoenix-format panel (§5). Skip only with `no-gex`.
**C · Price & trend** — 1-year chart, MA50/200, gamma wall overlay, one structural paragraph.
**D · Gate stack** — the signature element (§6).
**E · Reward-to-risk** — scenario table, 2:1 / 3:1 requirements, the chandelier note (§7).
**F · News flow** — dated rows with sentiment pills (§8).
**G · Reference frame** — *which* comparison is correct depends on the name's size and correlation to SPX (see §6b). For most names: sector ETF, the subject highlighted, 3–4 industry peers. For top-weight index names: SPX and the SPX gamma regime instead.
**H · Fundamentals** — 8-cell strip + bottleneck card + moat card + push-back note.
**I · Quality score** — five blocks, 100 points, band verdict.
**J · Risk register** — severity, mitigation, **and the residual after mitigation**.
**K · Invalidation** — two cards, bullish / bearish. **Never optional.**
**L · Rules applied** — the audit trail (§9).

---

## 4 · VISUAL SYSTEM — sampled from `phoenix_app.html`

```
--bg:#020F1F   --card:#08192B  --card2:#0C2137  --line:#14304A  --line2:#1E4462
--ink:#E7EEF6  --inkMed:#C4D2DF --inkLight:#94A8BA --inkFade:#6B8299 --inkGhost:#47596C
--gold:#D37736 --green:#3FBF77 --red:#D9534F   --amber:#D8A24A  --rail:#010A15
--serif:Georgia,'Times New Roman',serif        --mono:'Courier New',ui-monospace,monospace
```

**Type discipline.** Georgia for prose and headlines — the report reads as a document, not a dashboard. Courier New for every number, label, ticker and rule id, without exception. Headlines are regular weight with italic gold for the turn; never bold-shouty.

**Layout.** 232px sticky left rail carrying the TOC and a live stat block; content max ~1400px. Sections separated by 1px `--line`, never by cards-within-cards.

**Chart palette.** Price `--ink` 1.6px · 50-day `--gold` · 200-day `#5E7C99` dashed · gold gradient fill under price · grid `#0F2438`. Call gamma `#5B7FD4`, put gamma `--gold`, spot `#4FA8E0`, flip `--amber` dashed.


**Both themes, always.** Phoenix ships a light theme and the report must too — a fixed `.pxtheme` Dark/Light switch top-right, `html.light` class on the root, choice persisted to `localStorage`, and `prefers-color-scheme` respected on first load. Light tokens sampled from the app:

```
--bg:#F6EFE4   --card:rgba(255,253,249,.82)   --card2:rgba(248,241,231,.92)
--line:rgba(122,96,68,.20)                    --line2:rgba(122,96,68,.34)
--ink:#2C2118  --inkMed:#43352A  --inkLight:#6A5949  --inkFade:#8C7A67  --inkGhost:#B3A493
--gold:#C2632A --green:#1E7A4E   --red:#B4362F  --amber:#A87A1C  --rail:#F3EADC
GEX light: call #1E7A4E  put #B4362F  spot #2C5A82  flip #A87A1C
```


**Two CSS failures that shipped and must not recur.**

*The stretched switch.* The responsive override set `top:auto; bottom:16px`, but the `.pxtheme` base rule declaring `top:14px` appeared **later in the stylesheet**. Equal specificity means source order wins, so both anchors applied and the switch stretched the full viewport height. **Put positioning overrides last in the sheet, declare `bottom:auto` explicitly alongside `top`, and give the element `height:auto; max-height` plus fixed-height buttons.** Never rely on a media query overriding a rule that comes after it.

*The collapsed rail stats.* Hiding `<br>` inside a flex container and relying on `gap` to separate the lines produced `META · $549.90L3 SINGLE NAME23 AUG 2026` — **flex gaps do not apply between text nodes**, only between element children. Either wrap each line in its own element or keep the `<br>` and use `line-height`. The second is simpler and does not require touching the markup.

**Check both at phone width before shipping**, not just tablet: the tablet breakpoint hid the first bug because the switch had room to look normal.

**Paint the background explicitly at every level in both themes.** Relying on the `--bg` variable cascade alone fails on iOS in-app browsers: the class toggles and text colours change while the page stays dark. Set it on `html`, `body`, `.shell` and `main`, and set `color-scheme` on the root element in CSS *and* in JS on toggle, plus a `<meta name="color-scheme" content="dark light">`.

Chart strokes and grid lines need explicit light overrides — an SVG drawn for dark ink stays dark-inked on cream otherwise. Add a `@media print` rule hiding the rail and the switch. **Default to dark and let the button always win; do not auto-switch on `prefers-color-scheme`**, which reads as a bug when the system and the saved preference disagree.

**Restraint.** One accent doing the work. No gradients beyond the single chart fill, no shadows, no rounded cards. The signature is the gate panel; everything else stays quiet.

---


---

## 4b · THE NEWS SEARCH IS NOT OPTIONAL — AND ONE SEARCH IS NOT ENOUGH

**This has failed once and it must not fail again.** Uber announced the **Zipline drone partnership** — a new business line targeting one million deliveries a day by 2029, with an equity investment and launches in Dallas and Houston this year — on **17 August**. The report was written for data through **21 August**. An earnings-and-ratings search returned neither the deal nor any mention of it, and the report shipped without the single most important development of the month.

**Why it failed:** an earnings-focused query returns the quarter. Anything announced *after* the print is invisible to it. Analyst-rating queries return ratings. Neither surfaces a partnership, an acquisition, a product launch or a regulatory decision.

**The required minimum is four searches, and the last two are the ones that catch what the first two miss:**

```
1. "<TICKER> Q<n> <year> earnings results guidance"
2. "<TICKER> news <month> <year> analyst rating price target"
3. "<TICKER> business model customers partnerships history growth"
4. "<TICKER> partnership deal acquisition announcement <month> <year>"   ← MANDATORY
```

**Search 4 is dated to the current month and must be run every time**, even when the first three look complete. If the report covers data through a date, the search must cover the four weeks before that date.

**A checkable test before shipping:** for every claim in the news section, ask *what would a deal announced ten days ago look like in this list?* If the answer is "it wouldn't appear", the search was insufficient. Where a company's strategy is explicitly partnership-driven — Uber's autonomy stack, any platform business, any acquirer — **assume there is a deal you have not found and search until you can name the most recent one.**

---

## 5 · THE GEX PANEL — Elliot's format, exactly

Header row: **TICKER** · regime badge (green `POSITIVE GAMMA` / red `NEGATIVE GAMMA`) · distance-to-flip pill · right-aligned status line in caps (`TESTING THE 18 CALL WALL`, `HELD THE 18 CALL WALL`).

Sub-line: expiry, strike range, units, vol assumption.

Chart: cumulative gamma curve in white with filled area beneath; per-strike bars above zero for call gamma and below for put gamma; **yellow dashed vertical for FLIP with a filled label chip**; **cyan vertical for SPOT with a filled label chip**; strikes on the x-axis.

Levels ladder, **descending**, one row per level, coloured left border:

```
CALL GAMMA   blue   call-dominant two-sided strike
CALL WALL    gold   call gamma concentrates, dealers sell into strength
SPOT · LAST  cyan   highlighted row, price right-aligned
PUT WALL     grey   first shelf of dealer buying below
THE FLIP     amber  positive gamma above, negative below
PUT GAMMA    grey   put-dominant two-sided strike
```

Then a narrative paragraph in Elliot's register: what the level is doing, what dealers do on either side of it, and whether this is a **wall story or a flip story**.

**Window the chart to spot &minus;14% / +15%.** Far out-of-the-money strikes carry almost no gamma and compress everything near the money into invisibility. A 200 strike on a $310 stock adds nothing and costs the whole chart its resolution. If a level outside the window matters, state it in the ladder rather than plotting it.

**Plot NET gamma per strike as a single bar, coloured by sign** — green above zero, red below. Not separate call and put bars. This is what Phoenix does and it is more legible: one bar, one number, sign carries the meaning. Drop the cumulative overlay from the chart; the flip line already encodes it.

**Header strip, in Phoenix's own terms:** `Regime · Net GEX · Flip · OI Res · OI Sup · Deep magnet`. Use those labels exactly.

**Markers:** spot (blue solid) · flip (yellow dashed) · OI resistance (green dashed) · OI support (red dashed) · deep magnet (purple dotted).

**Use Phoenix's GEX where Phoenix publishes it.** A sampled chain is not a substitute for a complete one and the error is not small: on AAPL a seven-strike sample gave flip 271.45 and resistance at 320, while Phoenix's full chain gives flip **290.56** and resistance at **310** — which is *at spot*. The sample said resistance was 3.4% overhead; it was already here. **Compute independently only for tickers Phoenix does not cover, and say so in the footer when you do.**

**GEX arithmetic — Phoenix's own method, taken from `drawMktGex`.** `gamma × OI × 100 × S² × 0.01`, calls positive and puts negative; the running cumulative crossing zero gives the flip. **Walls are chosen on open interest, not on the tallest gamma bar** — Phoenix states this in its own legend. **Always compute the full chain**, not one expiry. Deep out-of-the-money puts in far-dated months are what drag the flip and the put wall down; omitting them produces a flip that is wrong by dollars. On SOFI the near two chains gave a flip of 18.32 and a put wall of 18; adding January's 66,759 puts at $15 moved the put wall to **15** and the flip to **18.63**.

**Then show the per-expiry breakdown as a second view** — a small table of expiry, days, call OI, put OI and dominant strike. It answers the question the aggregate hides: which expiry actually governs hedging now. Far-dated contracts carry the most open interest and the least gamma, so they set the structure without moving today's hedging.

**Chart classes that must exist or the SVG renders black.** `.gcurve` needs `fill:none`; `.garea` needs an explicit `fill`. An SVG `<path>` with no fill declared defaults to **solid black**, which silently destroys the chart in both themes and is not obvious from the code.

**One axis, net gamma per strike.** The dual-axis experiment was a wrong turn: two scales on one plot is harder to read than the problem it solved. Net-per-strike bars on a single axis, windowed tightly, is both legible and sufficient.

**Never put a paragraph inside a flex legend.** `.gkey span{display:inline-flex}` turns any `<b>` inside a note span into a flex child and scrambles the sentence into columns. Give the note `display:block !important` and `width:100%`.

**Phoenix GEX palette, exact:**
```
call #2D8A55   put #C43A30   spot #3B82C4   flip #E8C34A   pin #9B7BD4   magnet #8A6A3A
```

**Compute it, never copy it.** Where another analyst's panel exists, compute independently first, then show both and explain the discrepancy — different expiry windows produce genuinely different flips, and **the near-chain flip is the one that binds a stop**.

**A call wall is resistance, not a ceiling.** Dealer selling into it is finite. Cleared on volume the mechanic inverts: dealers short gamma above the strike must buy to re-hedge, the wall becomes support, and the move accelerates. Never write 'capped at X' — write 'X is where it gets hard, and clearing it makes the move easier'.

---


---

## 5c · THE VOLATILITY PANEL — MANDATORY IN EVERY REPORT

Placed immediately **before** reward-to-risk, because it determines the stop and the stop determines everything downstream. Eight cells:

```
Implied vol (annual)  ·  Realised vol (30d)  ·  Spread (IV − HV)  ·  IV percentile 52w
Daily ATR-14 ($ and %)  ·  IV-implied daily move  ·  1-month 1σ  ·  Stop basis
```

**The spread is the operative number.** Implied volatility is a *forecast*; realised is a *measurement*. When they diverge the options market and the tape disagree about how much room a position needs, and a stop placed on the wrong one is either harvested by noise or too wide for the account to carry.

**Rule: default to the measured range unless the two agree within ~8%.** State the basis explicitly in the eighth cell — `REALISED` or `EITHER` — so the reader does not have to infer it.

**This is not a marginal adjustment.** Across the eleven names reviewed 23 August 2026, **nine had implied below realised**, several by more than fifteen points:

| | IV | Realised | Spread | Stop error if sized on IV |
|---|---|---|---|---|
| PLTR | 48.0% | 68.8% | **−20.8** | ~30% too tight |
| MSFT | 25.6% | 45.4% | **−19.8** | ~44% too tight |
| AMZN | 28.4% | 47.0% | **−18.6** | ~40% too tight |
| META | 34.1% | 47.3% | −13.2 | ~28% too tight |
| AAPL | 23.9% | 36.0% | −12.1 | ~34% too tight |
| NVDA | 39.2% | 37.8% | **+1.4** | aligned — earnings bid |
| MP | 69.0% | 69.1% | **0.0** | aligned |

**Two readings worth calling out when they occur.** A **low IV percentile with a wide negative spread** (TSLA 6th, GOOGL 10th, SOFI 2nd) means the options market has forgotten a move the price history still contains — the most dangerous configuration for stop placement. **IV above realised** almost always means a scheduled event is priced in; check the calendar before concluding the options are expensive.


## 5b · THE GATES ARE WEEKLY — get this right

`phoenix.py` computes every gate on **weekly** closes. Daily moving averages are the wrong number, and TradingView's daily EMA is a third wrong number.

```
ma40 = 40-week SMA   ~200-day     trend200:  last > ma40
ma10 = 10-week SMA   ~50-day      trend50:   last > ma10
ma30 = 30-week SMA   Weinstein    stage2:    last > ma30 AND ma30 rising vs 4 weeks ago
high = max of last 104 WEEKLY highs          near_high: -8% <= pos <= -1%, or pos > -1%
industry: ticker's industry above a rising average — needs rotation_nav
```

**Five gates: `trend200` `trend50` `industry` `near_high` `stage2`.** A full passer clears all five; a near-miss clears exactly four. There is no `rs_mkt` or `vol_surge` in the stack — do not invent gates.

**On SOFI this mattered.** Daily SMA200 = $20.51, weekly 40-week SMA = **$20.06**, TradingView daily EMA200 is around $19.0 and sits almost on price. Quote the 40-week. And `stage2` failed not because price was below the 30-week — it was above — but because **the 30-week was still falling**. Always check the slope, not only the level.

**If `rotation_nav.json` and `universe.csv` are absent, mark `industry` UNVERIFIED** and show a hand-built peer group. Never guess a gate.

---

## 6a · CHOOSE THE RIGHT REFERENCE FRAME

**The correct benchmark is a function of correlation to SPX, not of GICS classification.**

| the name is | governed by | show |
|---|---|---|
| **Top 20–30 SPX weight** | **SPX and the SPX gamma regime.** The stock is a large part of what the index *is* — causation runs both ways, and index-level hedging, ETF creation/redemption and the passive bid reach it without passing through the sector | SPX level and trend · SPX net GEX and flip · index correlation |
| Top 30–50 | Mixed. Sector begins to carry information but the index still dominates | Both, and say which is binding |
| Outside the top 50, or any mid-cap | **Sector and industry.** Idiosyncratic and sector flow dominate; the index is background | Sector ETF · 3–4 industry peers |

**The test is correlation, not market cap.** A large but low-correlation name is governed by its industry; a smaller but highly index-correlated one moves with SPX. Where correlation is unknown, say so rather than assuming.

**Worked example.** Comparing AAPL to XLK measures *the rest of technology outrunning Apple*, because Apple is a top-three weight in the thing it is being compared against — near-decorative. Comparing SOFI to XLF is genuinely informative: a 33-point deficit that is entirely its own. **Same table, opposite value.**

**Consequence for the GEX section:** for a top-weight index name, single-name gamma is only half the picture. **SPX gamma regime is the other half** and should be retrieved alongside it.

---

## 6 · THE GATE STACK — the signature element

One row per gate: index, name in mono, one sentence of evidence with the actual number, and a `PASS` / `FAIL` / `AMBIGUOUS` tag. Failing rows get a red wash. **Order the failures first** — the funnel terminates at the first failure and the reader should see that immediately.

Close with a stamp bar: counts on the left, the outcome in mono on the right (`NO CANDIDATE` / `CANDIDATE`).

Follow with **two rule chips** explaining *why* the failing gates matter — not repeating what they say.

---

## 7 · REWARD-TO-RISK — always answer three questions

1. **What does the current structure give?** Scenario table: entry, stop, risk %, ×ATR, R:R, shares, position value. Include the rejected wide-stop version so the improvement is visible.
2. **What does 2:1 and 3:1 require?** Solve for the stop at spot, *and* solve for the entry at a fixed stop. Three cards side by side.

**Never label a column "Risk" in a risk document.** In the reward-to-risk table, the percentage next to the stop is **stop distance as a share of the price**, not the share of the account at risk. Those are different quantities and confusing them is the single most dangerous ambiguity a trading report can contain — a reader with a 1% risk policy sees "8.3%" and reasonably concludes the policy has been breached.

Label it **`Stop dist`**, and state the invariant explicitly beneath every such table:

> Account risk is **1.00% in every row**, fixed at $20 on a $2,000 book. Share count is derived from it: `shares = $20 ÷ (entry − stop)`. A wider stop buys **fewer shares, not more risk**. The only figure that varies with stop distance is **position value** — which is why the concentration cap and the 1R budget are separate constraints on separate axes.

**Sanity check before shipping:** for every row, `shares × (entry − stop)` must land within a few cents of the 1R budget. If it doesn't, the sizing is wrong rather than the label.

3. **What if it runs past the target?** Always the chandelier. A fixed target caps you exactly when the trade becomes worth holding.

**Anchor the stop to the nearest defensible structure, not the most obvious one.** Anchoring to a chart support zone three dollars away gave 0.94:1; anchoring to the reclaimed gamma wall one dollar away gave 2.31:1. **Same trade. The stop location was the whole difference.**

Always close with the commission floor: 0.054R at current account size, 1.70R payoff needed to break even.

---

## 8 · NEWS — dated rows, never a paragraph

`DATE | pill | headline | 2–3 lines of body`. Pills: `DRIVER` `FLOW` `INITIATION` `BEAR CASE` `EARNINGS` `PRODUCT` `OVERHANG` `CATALYST`, coloured bull/bear/neutral.

**Always include the bear case.** A news section with only bullish items is marketing.
**Split the section in two, with headers:** `TICKER-SPECIFIC` and `MARKET-WIDE — reaches <TICKER> through <channels>`. A move shared with the peer group is beta, and the split is what makes that visible rather than merely asserted.
**Note where analyst targets sit relative to gamma levels.** On SOFI the $22 Piper target sat exactly on the $22 wall; that coincidence is information.

---

## 9 · RULES APPLIED — the audit trail

Final table before the verdict: `rule id | applied to | effect on the verdict`. Ten to twelve rows. Rule ids in gold mono.

**This is what makes the report Phoenix's rather than generic.** A judgement without a rule id behind it is an opinion, and the table is where that gets caught.

Rule chips inline through the report use the same ids — gold uppercase mono label, one paragraph, gold left border. Use them where a rule *changes* the reading, not to decorate agreement.

---

## 9b · WRITE IT AS PUBLISHED, NOT AS REVISED

The report is a document, not a conversation. It must read as though written once, by someone who reached these conclusions directly.

**Never appears in the output:** "my earlier number", "a correction to", "you were right", "I previously anchored", "(earlier version)", or any row in a table labelled as a superseded attempt. If an approach was rejected, present it as **an alternative that scores worse**, with its own row and no history: *"Chart support zone − 0.75 ATR — 1.18:1"* rather than *"my earlier stop"*.

**Attribute methods, not people, unless the source is the subject.** Write "the full-chain flip sits at 16.35" rather than "his flip is 16.35". Where two calculations differ, present both as **two windows with different properties** and say which one binds — that is analysis. Saying whose number is whose is provenance, and it belongs in the footer.

**Corrections happen before publication, silently.** The reader gets the conclusion and the evidence. The revision history is yours.

---

## 10 · THE STANDING RULES

- **Verdict first.** State the conclusion in section A so the analysis can be judged as support rather than discovery.
- **Business quality and tradeability are separate questions** and the report says both plainly. A 70/100 business on a Stage-1 chart is a watchlist entry.
- **Fundamental scoring is context, never a gate** — otherwise it argues you into the trade the technical stack just refused.
- **Every claim carries its number.** Not "weak relative strength" but "−27.5% YTD against XLF at +5.5%."
- **Reconcile conflicting sources rather than picking one.** Elliot's near chain and the September chain measure different windows; both were shown and the disagreement was explained.
- **State limitations in the footer**, specifically: vol assumption, expiry window covered, whether dealer positioning is inferred, and any source that is a session old.
- **Invalidation is mandatory.** No report ships without section K.
- **Never fabricate a data source.** If GEX needs data you do not have, say so and explain what it would tell you.

---

## 11 · WHAT TO SAY TO GET ONE

> **"Phoenix report on NVDA."**

That is enough. Defaults: L3, all twelve sections, live IBKR run, Phoenix palette, single-file HTML.

Modifiers: `L1` / `L2` / `R` for depth · `no-gex` · `no-news` · `quick` for markdown-only, no charts · `compare X vs Y` for a two-name L2.

**Time cost:** roughly 15 IBKR calls, 2 web searches, and one build. The data run is the long part; the writing is templated.
