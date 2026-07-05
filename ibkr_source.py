"""IBKR source — wraps the IBKR API ONCE, correctly, in one place.
When the integer-param bug is solved, it's solved here for the whole system.
STATUS: stub — Stage E."""
from data.sources.base import DataSource, Result
class IBKRSource(DataSource):
    name = "ibkr"
    def fetch(self, field, **params):
        raise NotImplementedError("IBKR source: Stage E")
