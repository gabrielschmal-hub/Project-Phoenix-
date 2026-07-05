# outputs/

The JSON files the frontend reads. Written by the engines each run (via core/outputs.py).
In static-first v0 these are committed to the repo and served by GitHub Pages.
Same shapes become live API responses in Phase 2.

- `macro.json` — regime, confidence, stat boxes
- `industries.json` — ranked industries
- `stocks.json` — scored universe
- `gex.json` — net GEX, flip, walls, greeks
- `meta.json` — freshness, source flags, warnings (always present)
