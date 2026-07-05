"""
Phoenix database — SQLite (one file, no server).
The system's memory: accumulates history so we can backtest and validate.
"""
import sqlite3
from contextlib import contextmanager
from core.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS macro_daily (
    date TEXT PRIMARY KEY, spx REAL, ndx REAL, vix REAL, wti REAL, dxy REAL, gold REAL,
    rate_2y REAL, rate_10y REAL, real_10y REAL, hy_spread REAL,
    regime TEXT, regime_confidence REAL, source_flags TEXT
);
CREATE TABLE IF NOT EXISTS stock_daily (
    date TEXT, ticker TEXT, close REAL, volume REAL,
    PRIMARY KEY (date, ticker)
);
CREATE TABLE IF NOT EXISTS fundamentals (
    ticker TEXT, quarter_end TEXT, revenue REAL, net_income REAL, fcf REAL,
    debt REAL, equity REAL, gross_profit REAL,
    PRIMARY KEY (ticker, quarter_end)
);
CREATE TABLE IF NOT EXISTS stock_scores (
    date TEXT, ticker TEXT, trade_score REAL, invest_score REAL,
    vol_state TEXT, breakout INTEGER, industry TEXT,
    PRIMARY KEY (date, ticker)
);
CREATE TABLE IF NOT EXISTS industry_scores (
    date TEXT, industry TEXT, cap_wtd_momentum REAL, rank INTEGER,
    above_ma INTEGER, rising INTEGER,
    PRIMARY KEY (date, industry)
);
CREATE TABLE IF NOT EXISTS gex_daily (
    date TEXT PRIMARY KEY, net_gex REAL, regime TEXT, gamma_flip REAL,
    call_wall REAL, put_wall REAL, vanna REAL, charm REAL, source TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, ticker TEXT, setup TEXT,
    qty REAL, entry REAL, stop REAL, entry_date TEXT, exit_date TEXT,
    exit_price REAL, status TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS universe (
    ticker TEXT PRIMARY KEY, sector TEXT, industry TEXT, market_cap REAL, updated TEXT
);
"""

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    """Create all tables if they don't exist."""
    with get_db() as conn:
        conn.executescript(SCHEMA)

def upsert(table, rows, keys):
    """Insert-or-replace a list of dict rows into a table."""
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ",".join("?" * len(cols))
    collist = ",".join(cols)
    sql = f"INSERT OR REPLACE INTO {table} ({collist}) VALUES ({placeholders})"
    with get_db() as conn:
        conn.executemany(sql, [[r[c] for c in cols] for r in rows])

if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
