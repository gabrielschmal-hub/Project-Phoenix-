# Project Phoenix — Trading Operating System

MacroFlow's evolution from an AI-refreshed dashboard into a self-running trading OS.
Claude is the final intelligence layer (PM / Risk / Research / Coach), never the plumbing.

## Architecture (four layers)
- **Data Collection** (`data/`) — dumb pipes. IBKR / FRED / yfinance. Source-abstracted, degrades gracefully.
- **Decision Engines** (`engines/`) — pure functions, no AI, no I/O. Input data → output scores.
- **Backend** (`core/`) — scheduler, database, JSON output writer.
- **Frontend** (`frontend/`) — reads JSON, renders. No calculation.

## How v0 runs (static-first, free)
```
6am → GitHub Actions → run.py --full → engines → outputs/*.json → commit → GitHub Pages serves it
```
No server, no monthly cost. See `docs/ARCHITECTURE.md` for the full blueprint.

## Quick start (local)
```bash
pip install -r requirements.txt
python run.py --full        # run the whole pipeline
python run.py --engine gex  # run one engine
python -m pytest tests/     # run tests
```

## Status
Scaffold stage. Building: Stock Engine (Track 1) + GEX vertical slice (Track 2).
