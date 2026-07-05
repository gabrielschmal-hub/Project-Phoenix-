"""
The scheduler — orchestrates the daily run. Called by run.py (and by GitHub Actions at 6am).
This is the ONLY place that knows the full sequence: collect → store → score → write JSON.
Engines and collectors stay ignorant of each other; the scheduler wires them together.
"""
from core.database import init_db
from core.outputs import write_json, write_meta

def run_full():
    """The complete daily pipeline."""
    print("=== Phoenix full run ===")
    init_db()
    warnings, flags = [], {}

    # --- 1. COLLECT (each collector returns data + source/degraded flags) ---
    # from data.collectors import macro_data, rates_data, stock_data, gex_data, universe
    # macro = macro_data.collect(); flags.update(macro.flags); ...
    print("[collect] (collectors not yet wired)")

    # --- 2. STORE raw data → database (history accumulates) ---
    print("[store]   (db writes not yet wired)")

    # --- 3. SCORE (run engines on fresh data) ---
    # from engines import macro_engine, industry_engine, stock_engine, gex_engine
    # regime = macro_engine.run(macro, rates)
    # industries = industry_engine.run(stock_data, universe)
    # stocks = stock_engine.run(stock_data, industries, fundamentals, universe)
    # gex = gex_engine.run(options_chain)
    print("[score]   (engines not yet wired)")

    # --- 4. WRITE JSON outputs the frontend reads ---
    # write_json("macro", regime); write_json("stocks", stocks); write_json("gex", gex)
    write_meta(source_flags=flags, warnings=warnings)
    print("[write]   meta.json written")
    print("=== done ===")

def run_engine(name):
    """Run a single engine (for dev/testing one piece)."""
    print(f"=== Phoenix single engine: {name} ===")
    if name == "gex":
        # from engines import gex_engine
        # from data.collectors import gex_data
        # chain = gex_data.collect(); result = gex_engine.run(chain)
        # write_json("gex", result)
        print("[gex] (not yet wired — Track 2)")
    elif name == "stock":
        print("[stock] (not yet wired — Track 1)")
    else:
        print(f"[{name}] not yet implemented")

def run_collector(name):
    """Run a single collector (for dev/testing data pulls)."""
    print(f"=== Phoenix single collector: {name} ===")
    print(f"[{name}] (not yet wired)")
