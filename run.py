#!/usr/bin/env python3
"""
Phoenix single entrypoint.
  python run.py --full            run the whole daily pipeline
  python run.py --engine gex      run one engine only
  python run.py --collect stocks  run one collector only
"""
import argparse, sys
from core.scheduler import run_full, run_engine, run_collector

def main():
    p = argparse.ArgumentParser(description="Project Phoenix pipeline")
    p.add_argument("--full", action="store_true", help="run the full daily pipeline")
    p.add_argument("--engine", type=str, help="run a single engine (macro|industry|stock|gex|trade)")
    p.add_argument("--collect", type=str, help="run a single collector (macro|rates|stocks|gex|universe)")
    args = p.parse_args()

    if args.full:
        run_full()
    elif args.engine:
        run_engine(args.engine)
    elif args.collect:
        run_collector(args.collect)
    else:
        p.print_help(); sys.exit(1)

if __name__ == "__main__":
    main()
