#!/usr/bin/env python3
"""
phoenix_sync.py  —  Session 1 of the Supabase migration.

Reads outputs/*.json off disk and pushes them into Supabase as snapshot rows.

Deliberately knows NOTHING about phoenix.py. It does not import it, does not
patch it, does not run it. The 33-step pipeline is untouched and cannot break
because of anything in this file. If this script dies, the outputs are still
committed to the repo and still served by Pages exactly as they are today.

Safe to re-run. Safe to delete.

  python phoenix_sync.py                 # sync outputs/ -> Supabase
  python phoenix_sync.py --dry-run       # show what would happen, write nothing
  python phoenix_sync.py --keep 5        # retention: snapshots kept per file
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

TIMEOUT = 60
RETRIES = 3


# ---------------------------------------------------------------- transport

class Supa:
    def __init__(self, url: str, key: str):
        self.rest = url.rstrip("/") + "/rest/v1"
        self.h = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _go(self, method: str, path: str, **kw):
        last = None
        for attempt in range(RETRIES):
            try:
                r = requests.request(
                    method, f"{self.rest}{path}",
                    headers={**self.h, **kw.pop("extra_headers", {})},
                    timeout=TIMEOUT, **kw,
                )
                if r.status_code >= 500:
                    last = RuntimeError(f"{r.status_code} {r.text[:200]}")
                    time.sleep(2 ** attempt)
                    continue
                if r.status_code >= 400:
                    raise RuntimeError(f"{r.status_code} {r.text[:400]}")
                return r
            except requests.RequestException as e:
                last = e
                time.sleep(2 ** attempt)
        raise last

    def get(self, path):
        return self._go("GET", path).json()

    def insert(self, table, row, returning=False):
        hdr = {"Prefer": "return=representation" if returning else "return=minimal"}
        r = self._go("POST", f"/{table}", json=row, extra_headers=hdr)
        return r.json() if returning else None

    def patch(self, table, where, row):
        self._go("PATCH", f"/{table}?{where}", json=row)

    def rpc(self, fn, args=None):
        r = requests.post(
            f"{self.rest}/rpc/{fn}", headers=self.h,
            json=args or {}, timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json() if r.text.strip() else None


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="outputs")
    ap.add_argument("--kind", default="full", choices=["full", "manual"])
    ap.add_argument("--keep", type=int, default=5,
                    help="snapshots retained per filename")
    ap.add_argument("--max-mb", type=float, default=4.0,
                    help="skip files larger than this; Pages still serves them")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        print("FATAL: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
        return 2

    out = Path(a.dir)
    if not out.is_dir():
        print(f"FATAL: {out}/ not found — run this from the repo root")
        return 2

    files = sorted(p for p in out.glob("*.json") if p.is_file())
    if not files:
        print(f"FATAL: no *.json in {out}/ — did the pipeline run?")
        return 2

    supa = Supa(url, key)

    # ---- freeze gate. Read it BEFORE doing any work. -------------------
    try:
        st = supa.get("/system_state?id=eq.1&select=mode,reason,frozen_at")
        mode = (st[0]["mode"] if st else "live")
    except Exception as e:
        print(f"FATAL: cannot reach Supabase: {e}")
        return 2

    if mode == "frozen":
        print("FROZEN — system_state.mode='frozen'. Nothing written.")
        print(f"        reason: {st[0].get('reason') or '(none given)'}")
        print(f"        since:  {st[0].get('frozen_at')}")
        print("        unfreeze: update system_state set mode='live';")
        return 0

    print(f"live · {len(files)} json files in {out}/")

    if a.dry_run:
        total = 0
        for p in files:
            n = p.stat().st_size
            total += n
            flag = "  SKIP (too big)" if n > a.max_mb * 1e6 else ""
            print(f"  {p.name:<34} {n/1024:>9,.1f} KB{flag}")
        print(f"  {'TOTAL':<34} {total/1e6:>9,.2f} MB")
        print("dry run — nothing written")
        return 0

    # ---- open a run row ------------------------------------------------
    run = supa.insert("runs", {
        "kind": a.kind,
        "git_sha": os.environ.get("GITHUB_SHA"),
        "notes": os.environ.get("GITHUB_WORKFLOW"),
    }, returning=True)
    run_id = run[0]["id"]
    print(f"run {run_id}")

    ok, skipped, failed, sent_bytes = [], [], [], 0

    for p in files:
        size = p.stat().st_size
        if size > a.max_mb * 1e6:
            skipped.append(f"{p.name} ({size/1e6:.2f} MB > {a.max_mb} MB cap)")
            print(f"  skip  {p.name}  {size/1e6:.2f} MB — Pages still serves it")
            continue
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            failed.append(f"{p.name}: unparseable ({e})")
            print(f"  BAD   {p.name}  {e}")
            continue
        try:
            supa.insert("snapshots", {
                "name": p.name,
                "payload": payload,
                "bytes": size,
                "run_id": run_id,
            })
            ok.append(p.name)
            sent_bytes += size
            print(f"  ok    {p.name}  {size/1024:,.1f} KB")
        except Exception as e:
            failed.append(f"{p.name}: {e}")
            print(f"  FAIL  {p.name}  {e}")

    # ---- retention. 500MB free tier is not infinite. -------------------
    pruned = None
    try:
        pruned = supa.rpc("prune_snapshots", {"keep": a.keep})
        print(f"pruned {pruned} old snapshot rows (keep={a.keep})")
    except Exception as e:
        failed.append(f"prune: {e}")

    # ---- close the run row ---------------------------------------------
    warnings = {"skipped": skipped, "failed": failed} if (skipped or failed) else None
    supa.patch("runs", f"id=eq.{run_id}", {
        "finished_at": "now()",
        "ok": not failed,
        "steps": {"synced": ok, "pruned": pruned},
        "warnings": warnings,
    })

    print(f"\n{len(ok)} synced ({sent_bytes/1e6:.2f} MB) · "
          f"{len(skipped)} skipped · {len(failed)} failed")

    # Loud failure is free here: this is a separate workflow, so going red
    # cannot break the daily pipeline. Silent failure is the real danger.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
