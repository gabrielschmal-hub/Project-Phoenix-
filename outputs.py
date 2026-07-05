"""
Writes the JSON files the frontend reads. The bridge between engines and dashboard.
In v0 these are committed to the repo; in Phase 2 an API serves the same shapes.
"""
import json, os
from datetime import datetime, timezone
from core.config import OUTPUTS_DIR

def write_json(name, data):
    """Write one output file, e.g. write_json('gex', {...}) -> outputs/gex.json"""
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    path = os.path.join(OUTPUTS_DIR, f"{name}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=1)
    return path

def write_meta(source_flags=None, warnings=None):
    """The freshness/health file — always written, tells frontend + Claude the data state."""
    meta = {
        "last_run": datetime.now(timezone.utc).isoformat(),
        "source_flags": source_flags or {},   # e.g. {"spx": "ibkr", "gex": "spy_proxy"}
        "warnings": warnings or [],            # e.g. ["IBKR unavailable — SPX from proxy"]
    }
    return write_json("meta", meta)
