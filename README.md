# Project Phoenix (flat v0)

Single-file trading OS scaffold. Everything is in `phoenix.py` so it runs on
GitHub with no folder structure to fight. We split it into the proper package
layout later (via Working Copy or a computer).

## Run
    python phoenix.py --full        # full daily pipeline
    python phoenix.py --engine gex  # one engine
    python phoenix.py --init-db     # create the database

## Files
- `phoenix.py`       — everything (config, db, engines, scheduler)
- `requirements.txt` — dependencies
- `outputs/`         — the JSON the dashboard reads (created on first run)
- `.github/workflows/daily.yml` — the 6am scheduled run

Status: scaffold runs clean. Next: port GEX (Track 2) + Stock engine (Track 1). 
 
