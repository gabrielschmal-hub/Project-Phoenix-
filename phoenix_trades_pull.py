#!/usr/bin/env python3
"""
phoenix_trades_pull.py — trade book: Supabase -> outputs/trades.json (27 Aug 2026)

The trade book lives in the Supabase table `trade_book` now: the app writes
every add / edit / close there the moment it happens (PIN-guarded RPCs
save_trade / delete_trade). The engine keeps reading outputs/trades.json, so
this script runs FIRST in the daily workflow and rewrites that file from the
table. phoenix.py is untouched. The run then commits outputs/ as always, so
Pages and the Supabase snapshot stay consistent.

Rules
  - the table is the book of record; the file is a build artefact
  - if the table is unreachable or empty, the existing file is left alone
    (a run with yesterday's book beats a run with no book) and the exit code
    is non-zero so the log says so
  - `_meta` row = the file-level keys (schema, accounts, execution, lessons...)
  - rows are written in `ord` order, deleted rows are dropped

ENV  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY   OUT  outputs/trades.json
"""
import json, os, sys
from datetime import datetime, timezone

import requests

SUPA_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPA_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
OUT = os.environ.get("PHOENIX_TRADES_OUT", os.path.join("outputs", "trades.json"))


def main():
    if not SUPA_URL or not SUPA_KEY:
        print("[trades] SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set — outputs/trades.json left as is")
        return 2
    h = {"apikey": SUPA_KEY, "Authorization": f"Bearer {SUPA_KEY}"}
    try:
        r = requests.get(SUPA_URL + "/rest/v1/trade_book?select=id,ord,body,deleted,updated_at&order=ord.asc&limit=5000",
                         headers=h, timeout=60)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        print(f"[trades] table unreachable ({e}) — outputs/trades.json left as is")
        return 1
    meta = {}
    trades = []
    for row in rows:
        if row.get("deleted"):
            continue
        if row["id"] == "_meta":
            meta = row.get("body") or {}
        else:
            trades.append(row.get("body") or {})
    if not trades and not meta:
        print("[trades] table is empty — outputs/trades.json left as is")
        return 1
    if not trades:
        print("[trades] WARNING: no trades in the table (meta only) — writing an EMPTY book on purpose? refusing; file left as is")
        return 1
    payload = dict(meta)
    payload["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["source"] = "Supabase trade_book (phoenix_trades_pull.py) — the app writes the table, this file is built from it"
    payload["trades"] = trades
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)
    # the watchlist rides along: outputs/watchlist.json feeds the ETF profile set
    # (a starred ETF gets a profile next run) and anything else the engine wants
    try:
        r2 = requests.get(SUPA_URL + "/rest/v1/watch_tickers?select=ticker,bucket&active=eq.true&bucket=in.(watch,watch_eu)",
                          headers=h, timeout=30)
        r2.raise_for_status()
        wl = sorted({w["ticker"] for w in r2.json()})
        wp = os.path.join(os.path.dirname(OUT) or ".", "watchlist.json")
        with open(wp, "w", encoding="utf-8") as f:
            json.dump({"asof": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "count": len(wl), "tickers": wl}, f, indent=1)
        print(f"[trades] outputs/watchlist.json <- {len(wl)} watched names")
    except Exception as e:
        print(f"[trades] watchlist skipped ({e})")
    latest = max((row.get("updated_at") or "" for row in rows), default="")
    from collections import Counter
    st = Counter(t.get("status") for t in trades)
    print(f"[trades] outputs/trades.json <- {len(trades)} trades from trade_book "
          f"({dict(st)}) · latest edit {latest[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
