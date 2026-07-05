# PROJECT PHOENIX — Architecture Document
### The blueprint for turning MacroFlow into a self-running Trading Operating System
*Draft v1 · prepared by Claude acting as Chief Software Architect*

---

## 0. The one decision that shapes everything: static-first

**As architect, my ruling: Phoenix v0 is static-first, and this is correct on the merits — not a budget compromise.**

Here is the reasoning, because you should be able to challenge it:

A trading system that runs **once a day at 6am** does not need a live, always-on server. It needs something that *wakes up on schedule, does work, and writes the result down*. That is a fundamentally different (and cheaper, and more robust) thing than a live backend.

- Your entire workflow is **end-of-day / start-of-day**. You pull yesterday's close, the engines score it, you read the output over coffee. Nothing about that is real-time.
- A live backend (always-on API + database server) exists to answer requests *the instant they arrive*. You have no such requirement in v0. Paying for a server to sit idle 23 hours 59 minutes a day, waiting for one 6am job, is waste.
- **Static-first is also more reliable.** There is no server to crash, no uptime to monitor, no security surface exposed to the internet. A scheduled job either runs or it doesn't, and if it doesn't, yesterday's data is still sitting there. Nothing goes *down*.

**What "static-first" means concretely:**
```
6am: a scheduled job runs the engines → writes JSON files → commits them
Your dashboard: reads those JSON files → renders
```
No live server. The "backend" is a scheduled script + a folder of JSON. The frontend is static files that read the JSON.

**When Phoenix graduates to a live backend (and it might):** the day you want *on-demand* things — "recompute this stock's score right now with a stop I'm considering," or "show my live IBKR P&L this second," or "alert me intraday when SPX crosses the gamma flip." Those need a live process. That is **Phase 2**, and the architecture below is designed so the jump is clean: the engines don't change, we just put a live API in front of them instead of (or alongside) the scheduler.

So: **build static-first now (free), design so live-backend is a drop-in later (paid, only if needed).** That is the ruling.

---

## 1. The four layers, mapped to reality

Phoenix's four conceptual layers, made concrete:

```
┌─────────────────────────────────────────────────────────────┐
│ LAYER 4 · CLAUDE (you talk to me)                            │
│   Senior PM · Risk Officer · Research Analyst · Coach        │
│   Reads engine JSON. Interprets, challenges, manages risk.   │
│   NEVER pulls data. NEVER calculates scores.                 │
└───────────────────────────▲─────────────────────────────────┘
                            │ reads JSON (paste or upload)
┌───────────────────────────┴─────────────────────────────────┐
│ LAYER 1 · FRONTEND (the dashboard on your iPad)             │
│   Displays engine output. Reads JSON. Renders panels.        │
│   NO calculations live here anymore. Pure presentation.      │
└───────────────────────────▲─────────────────────────────────┘
                            │ reads JSON files
┌───────────────────────────┴─────────────────────────────────┐
│ LAYER 3 · BACKEND (runs on schedule, writes JSON)           │
│   scheduler → data collectors → engines → database → JSON    │
│   In v0: GitHub Actions cron. In Phase 2: + live API.        │
└───────────────────────────▲─────────────────────────────────┘
                            │ calls
┌───────────────────────────┴─────────────────────────────────┐
│ LAYER 2 · DECISION ENGINES (pure functions, no AI)          │
│   macro · industry · stock · trade · position                │
│   Input: data. Output: scores + recommendations. Testable.   │
└───────────────────────────▲─────────────────────────────────┘
                            │ reads
┌───────────────────────────┴─────────────────────────────────┐
│ DATA COLLECTION (dumb pipes, no intelligence)               │
│   ibkr · fred · yfinance · gex · breadth · earnings          │
│   Source abstraction: each field declares its provider.      │
└─────────────────────────────────────────────────────────────┘
```

**The golden rule enforced by this structure:** intelligence flows *up*, data flows *up*, and each layer only knows about the one below it. Claude (Layer 4) never reaches down into data collection. The frontend never calculates. The engines never call an AI. This separation is the whole point — it's what makes the thing survivable and testable.


---

## 2. Repository structure (the file layout we build toward)

```
phoenix/
├── data/                      # DATA COLLECTION layer
│   ├── sources/
│   │   ├── base.py            # DataSource abstract class (the contract)
│   │   ├── ibkr_source.py     # IBKR: positions, SPX options, true prices
│   │   ├── fred_source.py     # FRED: rates, credit
│   │   ├── yfinance_source.py # yfinance: stock universe, sectors, SPY proxy
│   │   └── registry.py        # maps each data field → its provider
│   └── collectors/
│       ├── macro_data.py      # collects SPX/NDX/VIX/WTI/DXY/gold
│       ├── rates_data.py      # collects the FRED series
│       ├── stock_data.py      # collects the ~3,900 ticker daily bars
│       ├── gex_data.py        # collects/computes the SPX options chain
│       └── universe.py        # ticker → industry → mktcap mapping
│
├── engines/                   # DECISION ENGINES layer (pure, no AI, no I/O)
│   ├── macro_engine.py        # 8-regime detection → regime + confidence
│   ├── industry_engine.py     # cap-weighted industry momentum → rankings
│   ├── stock_engine.py        # 6 gates + Trade/Investment scores
│   ├── gex_engine.py          # gamma/vanna/charm → flip, walls, regime
│   ├── trade_engine.py        # position sizing, R-multiples, risk gates
│   └── position_engine.py     # open-position management (Phase 2)
│
├── core/                      # BACKEND plumbing
│   ├── database.py            # SQLite: schema, read/write, history
│   ├── scheduler.py           # orchestrates the daily run
│   ├── config.py              # keys, thresholds, parameters (one place)
│   └── outputs.py             # writes the JSON the frontend reads
│
├── api/                       # PHASE 2 ONLY (live backend)
│   └── app.py                 # FastAPI: /macro /stocks /gex /positions ...
│
├── outputs/                   # the JSON the frontend consumes (committed)
│   ├── macro.json
│   ├── industries.json
│   ├── stocks.json
│   ├── gex.json
│   └── meta.json              # last-run timestamp, data freshness, warnings
│
├── frontend/
│   └── dashboard.html         # reads outputs/*.json, renders (no calc)
│
├── tests/                     # every engine gets tests (this is non-negotiable)
│   ├── test_macro_engine.py
│   ├── test_stock_engine.py
│   └── ...
│
├── .github/workflows/
│   └── daily.yml              # the 6am cron: run scheduler → commit JSON
│
├── macroflow.db               # the SQLite history database
├── requirements.txt
└── run.py                     # single entrypoint: python run.py --full
```

**Why this shape:**
- **`data/` and `engines/` never import each other's internals.** Collectors produce plain data structures; engines consume them. You can test an engine with fake data, no network.
- **`engines/` are pure functions.** Input → output, no side effects, no file writes, no API calls. This is what makes them testable and trustworthy. An engine that reads a file or calls an API is a bug.
- **`core/` is the only place with I/O and scheduling.** One place to look when "the 6am job didn't run."
- **`config.py` holds every threshold** — the +40bp POLICY_TIGHTENING trigger, the ±15% GEX filter, the gate parameters. Today these are scattered across Colab scripts. One file. Change a threshold, the whole system uses it.
- **`api/` is walled off** — it doesn't exist in v0, and nothing else depends on it, so adding it later touches nothing.

---

## 3. The data-source abstraction (how the IBKR/free mix works)

Your ruling was: **IBKR for accuracy, free sources for the rest.** Here's the design that makes that clean and swappable.

Every collector asks a **registry** which provider to use for each field. The registry is the single place that encodes "this comes from IBKR, that comes from yfinance":

```python
# data/sources/registry.py  (illustrative)
SOURCE_MAP = {
    "spx_price":       "ibkr",      # accuracy matters → IBKR
    "spx_options":     "ibkr",      # GEX needs true SPX → IBKR
    "vix_term":        "ibkr",      # futures curve → IBKR
    "positions":       "ibkr",      # your live book → IBKR only
    "rates":           "fred",      # FRED is authoritative + free
    "credit_spreads":  "fred",
    "stock_universe":  "yfinance",  # 3,900 tickers → free is fine
    "sector_etfs":     "yfinance",
    "gex_fallback":    "yfinance",  # if IBKR down → SPY proxy
}
```

**The key design principle: graceful degradation.** Every IBKR-sourced field declares a fallback. If the IBKR connector is glitching (as it has been all week), the collector logs it, falls back to the free source where one exists, and **flags the degradation in `meta.json`** so the frontend can show "SPX from proxy — IBKR unavailable" and Claude knows the data is second-best. The system never *stops* because one source is down. This directly fixes the pain we hit repeatedly.

```python
# data/sources/base.py  (the contract every source implements)
class DataSource(ABC):
    @abstractmethod
    def fetch(self, field: str, **params) -> Result:
        """Returns {value, source, timestamp, degraded: bool, note: str}"""
```

Every piece of data carries *where it came from* and *whether it's degraded*. That metadata flows all the way up to Claude, so I always know if I'm reading gold-standard IBKR data or a proxy — and I'll tell you.

**On the IBKR connector specifically:** the bug we've been hitting (integer params coerced to strings) is an *interface* problem, not a data problem. In Phoenix, `ibkr_source.py` wraps the IBKR API once, correctly, in one place — so when we solve the param handling, we solve it everywhere, forever. Today it's scattered across tool calls; there it's one wrapper with one fix.


---

## 4. The database schema (what history we keep)

Today, MacroFlow has almost no memory — each Colab pull overwrites the last. A real trading OS **accumulates history**, because history is what lets you backtest, track regime transitions, and measure whether your edge is real. SQLite (one file, `macroflow.db`, no server) is the right choice for v0.

Core tables:

```sql
-- daily market snapshot (one row per trading day)
macro_daily(date PK, spx, ndx, vix, wti, dxy, gold,
            rate_2y, rate_10y, real_10y, hy_spread,
            regime, regime_confidence, source_flags)

-- per-stock daily (the universe, ~3,900 × days)
stock_daily(date, ticker, close, volume,
            PRIMARY KEY(date, ticker))

-- fundamentals (quarterly, slow-changing)
fundamentals(ticker, quarter_end, revenue, net_income, fcf,
             debt, equity, gross_profit, PRIMARY KEY(ticker, quarter_end))

-- engine output history (so we can see how scores evolved)
stock_scores(date, ticker, trade_score, invest_score,
             vol_state, breakout, industry, PRIMARY KEY(date, ticker))

industry_scores(date, industry, cap_wtd_momentum, rank,
                above_ma, rising, PRIMARY KEY(date, industry))

-- GEX history
gex_daily(date PK, net_gex, regime, gamma_flip, call_wall,
          put_wall, vanna, charm, source)

-- YOUR trades (the planner data, finally in a real store)
trades(id PK, account, ticker, setup, qty, entry, stop,
       entry_date, exit_date, exit_price, status, reason)

-- ticker → industry → mktcap (the universe map)
universe(ticker PK, sector, industry, market_cap, updated)
```

**Why this matters for you specifically:** once `stock_scores` and `industry_scores` accumulate, we can answer questions we *can't* answer today — "show me every time an industry flipped from falling to rising and what happened next," "how often does a Trade score above 80 actually work," "was the regime engine right last quarter." That's the raw material for the hedge-fund-grade validation Phoenix is really about. **The database is what turns MacroFlow from a snapshot into a system that learns.**

And your **trades finally live in a real table** — not baked into HTML, not in a fragile localStorage. The planner reads/writes the `trades` table. This permanently solves the saving problem that's bitten us twice.

---

## 5. The engine contracts — and where I challenge your methodology

This is the heart of it. Each engine is a pure function with a defined input and output. But designing the *contract* forces us to confront the *scoring logic* — and as your PM, I have real objections to raise. I'll give each engine its contract, then challenge it.

### 5.1 Macro Engine

**Contract:** `macro_engine(market_data, rates_data) → {regime, confidence, signals[]}`
Detects which of the 8 regimes is active, with a confidence and the contributing signals.

**Challenge — your NO_CLEAR default is doing suspicious work.** You've told me NO_CLEAR fires 62-66% of the time *by design*. I understand the intent (survival first — don't force a signal that isn't there). But as your risk officer I have to ask the uncomfortable question: **is NO_CLEAR a genuine "no edge" state, or is it a dumping ground for "our thresholds didn't quite trigger"?** Those are very different. The first is honest humility; the second is a system that rarely commits and therefore rarely helps. Before we hard-code this, I want to see the *distribution* — when NO_CLEAR fires, how close were we to a real regime? If we're consistently 5bp from POLICY_TIGHTENING (as we literally are right now, +33bp vs a +40bp trigger), then the threshold, not the market, is manufacturing the "no clear" reading. **The database will let us measure this.** I'm not saying you're wrong — I'm saying we should prove the 62-66% is signal, not threshold artifact, before we enshrine it.

### 5.2 Industry Engine

**Contract:** `industry_engine(stock_data, universe) → {industries[]: {momentum, rank, above_ma, rising}}`
Cap-weighted industry momentum, the load-bearing signal in your funnel.

**Challenge — this is your strongest-validated edge, so I'll defend it *and* stress it.** Your backtest showed rising-industry membership is the core signal (rising → +12-14%, flat/falling → ~0%). Good — that's real and it survived out-of-sample. But two questions: (1) **Cap-weighting means a few megacaps dominate each industry's score.** "Semiconductors is rising" might really mean "NVDA is rising." Is that the signal you want, or do you want breadth *within* the industry (are *most* names participating)? These diverge exactly at tops. (2) **Your industry definitions come from a static TradingView export.** Industries drift; a name reclassified changes the signal. We should decide how often the universe map refreshes.

### 5.3 Stock Engine

**Contract:** `stock_engine(stock_data, industry_scores, fundamentals, universe) → {stocks[]: {trade_score, invest_score, gates_passed, vol_state, breakout}}`

**Challenge — the two-score system is sound, but the weights are asserted, not derived.** Your Trade score is RS-mkt 30% / vol-surge 25% / tightness 20% / RS12 15% / breakout bonus. Where did those weights come from? If the honest answer is "they feel right," that's fine *for now* — but Phoenix's whole premise is measurement over intuition. **The database + backtest engine should eventually *fit* these weights, not assume them.** Also, a specific concern: **volume-surge is 25% of your Trade score, but your own backtest found volume accumulation was noise out-of-sample** (+26.5% in-sample → −1.6% → +12.4%, unstable). Why is an unstable signal carrying a quarter of the weight? That's the kind of thing I'm here to catch.

### 5.4 GEX Engine

**Contract:** `gex_engine(options_chain) → {net_gex, regime, gamma_flip, call_wall, put_wall, vanna, charm}`

**Challenge — we already know the honest limitation:** on the SPY proxy, the *levels* are accurate (flip, walls matched your source) but the *net-GEX sign* flips (SPY is put-heavier than SPX). The contract must therefore **carry a confidence/source flag** — "levels: high confidence, regime-sign: low confidence on proxy." When IBKR options data is available, sign confidence goes high. I won't let the engine report a regime it can't stand behind.

### 5.5 Trade Engine

**Contract:** `trade_engine(entry, stop, account_equity, atr, rules) → {size, risk_R, allocation, gates: {passed, breaches[]}}`
Position sizing and risk gates — the 1R/2R sizing, max-heat cap, cool-off logic already in your planner.

**Challenge — this one I mostly endorse.** Your risk framework (1% conservative / 2% aggressive, 2×ATR stops, 35% max position, 3R heat cap, 6-losses cool-off) is disciplined and it's the most *hedge-fund* part of your whole system. My only push: **the cool-off (6 losses → 5-day pause) is a psychological rule, and it's a good one — but it should be enforced by the engine, not your willpower.** Phoenix should *refuse* to size a new trade during a cool-off, not just suggest. Make the discipline structural.


---

## 6. The API surface (Phase 2 — designed now, built later)

Even though v0 is static-first, we design the API *contract* now so the engines produce the right shapes. In Phase 2, these become live endpoints; in v0, they're the JSON files in `outputs/`.

| Endpoint / File | Returns | v0 (static) | Phase 2 (live) |
|---|---|---|---|
| `/macro` → `macro.json` | regime, confidence, signals, stat boxes | written at 6am | live + on-demand |
| `/industries` → `industries.json` | ranked industries, momentum, posture | written at 6am | live |
| `/stocks` → `stocks.json` | scored universe, gates, breakouts | written at 6am | live + filterable |
| `/gex` → `gex.json` | net GEX, flip, walls, vanna, charm | written at 6am | live + intraday |
| `/positions` → `positions.json` | live IBKR book, P&L, risk | — | live only |
| `/trades` → `trades.json` | trade log, stats, R-multiples | written at 6am | live CRUD |
| `/meta` → `meta.json` | freshness, source flags, warnings | always | always |

**The contract is identical in both worlds** — same JSON shape whether it came from a 6am file or a live endpoint. That's what makes the upgrade seamless: the frontend and Claude don't know or care which produced it.

---

## 7. The daily run (how v0 actually works, start to finish)

```
06:00  GitHub Actions cron fires (.github/workflows/daily.yml)
   │
   ├─ run.py --full
   │    ├─ collectors pull data (IBKR where available, free elsewhere)
   │    │    └─ each result flagged with source + degraded status
   │    ├─ write raw data → macroflow.db (history accumulates)
   │    ├─ engines run on the fresh data:
   │    │    ├─ macro_engine    → regime
   │    │    ├─ industry_engine → rankings
   │    │    ├─ stock_engine    → scores (all ~3,900)
   │    │    └─ gex_engine      → flip/walls/greeks
   │    ├─ write engine output → macroflow.db (score history)
   │    └─ outputs.py writes outputs/*.json
   │
   ├─ git commit outputs/*.json + macroflow.db
   └─ git push
        │
        └─ frontend (GitHub Pages / Netlify) now serves fresh JSON
             │
             └─ you open the dashboard → sees today's data
                  │
                  └─ you paste the JSON to me → I interpret as your PM
```

**Cost: $0.** GitHub Actions free tier covers a daily job comfortably. GitHub Pages hosts the frontend free. No server, no database host, no monthly fee. The only thing you'd ever pay for is the *optional* Phase 2 live backend.

**IBKR wrinkle (honest):** IBKR's API needs an authenticated session (their Client Portal Gateway or TWS running). A headless GitHub Action *can't* easily hold an IBKR session. So in v0, **IBKR-sourced data (positions, true SPX options) may still need a manual assist** — you run a small local/Colab step for those, free sources run automatically. This is the one place the "fully automatic" dream meets reality: the free sources automate cleanly; IBKR's session model resists headless automation. We design around it (IBKR fields flagged optional, free fallbacks automatic). Full IBKR automation is a Phase 2 problem that likely needs the paid always-on host anyway.

---

## 8. Migration plan — from today to Phoenix without breaking what works

We do **not** rip everything out. We migrate incrementally, keeping a working system the whole way.

**Stage A — Extract the engines (no behavior change).**
Move the scoring logic that currently lives in the Colab/selection scripts into `engines/*.py` as pure functions. Same math, same output — just relocated and testable. Verify each engine reproduces today's numbers exactly before moving on. *This is pure refactoring; the dashboard doesn't change.*

**Stage B — Add the database.**
Stand up `macroflow.db` and start writing every daily pull into it. History begins accumulating. Frontend still works as today.

**Stage C — Wire the JSON outputs.**
Engines write `outputs/*.json`. Modify the dashboard to *read* those JSON files instead of having data baked in. Now a refresh = regenerate JSON, no HTML surgery. *This alone eliminates most of the pain we've hit.*

**Stage D — Automate the free sources.**
GitHub Actions runs the FRED + yfinance collectors at 6am, commits JSON. The macro/rates/stock/sector data now refreshes with zero action from you.

**Stage E — Add IBKR (assisted).**
The IBKR collector for positions + true SPX options, run via your manual assist, flowing into the same JSON/DB. GEX sign problem gets its real fix here (true SPX data).

**Stage F — Backtest & tune (the payoff).**
With history accumulating, build the backtest engine that *validates and fits* the scoring weights — turning the asserted weights from Section 5 into derived ones. This is where the methodology challenges get answered with data.

**Stage G — (optional) Phase 2 live backend.**
Only if/when you want on-demand recalc, live positions, or intraday alerts. Paid host, FastAPI in front of the same engines.

Each stage leaves you with a working system. If we stop at any stage, what exists still runs.

---

## 9. Claude's role at each layer (the boundary, explicit)

Phoenix says I'm the final intelligence layer, never the plumbing. Concretely, here's what that means day to day:

**What I STOP doing:**
- Pulling data on demand (the collectors do it)
- Calculating scores in-conversation (the engines do it)
- Regenerating the whole dashboard for every refresh (JSON does it)
- Being the single point of failure for your daily workflow

**What I START doing (my actual job):**
- **Senior PM:** "The regime flipped to POLICY_TIGHTENING overnight and your book is 60% high-beta semis. Here's how I'd de-risk."
- **Risk Officer:** "You're 2 losses from the cool-off. This next setup is marginal. I'd pass."
- **Research Analyst:** "The industry engine ranks Financials #1, but breadth is thin — it's three names. Here's what I'd verify before trusting it."
- **Performance Coach:** "Your last 8 stopped-out trades share a pattern: you entered before the volume confirmed. The engine flagged all 8 as 'accumulation not confirmed.' You're overriding your own gates."
- **Decision Partner:** challenging scoring, pressure-testing theses, catching the emotional trades.

**The test of whether Phoenix succeeded:** you can go a week without me, the system still runs, and when we *do* talk, it's about *decisions and risk* — not "the Colab broke" or "refresh the dashboard." I move from operator to advisor. That's the whole mission.

---

## 10. Open questions before we build (Stage A)

Things I need from you to start extracting engines:

1. **The regime engine's exact rules.** I have the 8 regimes and several thresholds from our work, but I want to confirm the *complete* trigger logic for each before I codify it. (You have `MacroFlow_Regime_Engine_Reference` — that's our source of truth.)
2. **Which engine to extract first in Stage A.** My vote: **Stock Engine**, because it's the most-used, best-specified, and its output you look at daily. Proving the extraction on the hardest engine de-risks the rest.
3. **GitHub account.** Static-first v0 runs on GitHub Actions + Pages. Do you have a GitHub account, or want me to walk you through creating one? (Free.) This is the "always-on home" question, answered the free way.
4. **The universe refresh cadence.** How often should ticker→industry→mktcap update? (Affects the industry engine's stability.)

---

## Appendix: what this does and doesn't change about our workflow *today*

**Doesn't change (keep doing):** the dashboard you have works. Keep using it. The combined Colab works. Keep using it. Nothing we've built gets thrown away — the engines *are* the Colab logic, relocated.

**Does change (the trajectory):** every stage moves calculation out of chat and into code that runs without me. The end state is a system where I'm your PM, not your operator.

*End of draft v1. This is a living document — we revise as we build.*
