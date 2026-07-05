"""yfinance source — stock universe, sectors, SPY proxy. Free. STATUS: stub — Stage D."""
from data.sources.base import DataSource, Result
class YFinanceSource(DataSource):
    name = "yfinance"
    def fetch(self, field, **params):
        raise NotImplementedError("yfinance source: Stage D")
