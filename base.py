"""
The DataSource contract. Every source (IBKR, FRED, yfinance) implements this.
Every result carries WHERE it came from and WHETHER it's degraded — that metadata
flows all the way up to Claude, so the analyst always knows the data's provenance.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

@dataclass
class Result:
    value: Any
    source: str                       # "ibkr" | "fred" | "yfinance"
    degraded: bool = False            # True if we fell back from the preferred source
    note: str = ""                    # human-readable explanation if degraded
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class DataSource(ABC):
    name: str = "base"

    @abstractmethod
    def fetch(self, field: str, **params) -> Result:
        """Fetch one field. Must return a Result (never raise on 'no data' — return degraded)."""
        ...
