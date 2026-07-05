"""Smoke tests — the structure imports and the contracts exist."""
def test_config_imports():
    from core import config
    assert config.STOCK["min_market_cap"] == 300e6

def test_database_schema():
    from core import database
    assert "stock_scores" in database.SCHEMA
    assert "trades" in database.SCHEMA

def test_engines_have_run():
    from engines import stock_engine, gex_engine
    assert hasattr(stock_engine, "run")
    assert hasattr(gex_engine, "run")

def test_registry_source_mix():
    from data.sources import registry
    assert registry.preferred("spx_options") == "ibkr"      # accuracy → IBKR
    assert registry.preferred("stock_universe") == "yfinance"  # bulk → free
    assert registry.fallback("spx_options") == "yfinance"    # graceful degradation
