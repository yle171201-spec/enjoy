#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date
import json

from app.db import init_db, SessionLocal
from app.services.backtest import close_vs_next_open, portfolio_backtest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", default="ABC")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--slippage-bps", type=float, default=0)
    ap.add_argument("--portfolio", action="store_true")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--execution", choices=["close", "next_open"], default="next_open")
    ap.add_argument("--mc", type=int, default=0)
    args = ap.parse_args()

    init_db(); db = SessionLocal()
    try:
        start = date.fromisoformat(args.start) if args.start else None
        end = date.fromisoformat(args.end) if args.end else None
        if args.portfolio:
            r = portfolio_backtest(db, tuple(args.engines), args.execution, start, end, args.k, monte_carlo_seeds=args.mc, slippage_bps=args.slippage_bps)
            print(json.dumps({"metrics": r["metrics"], "monte_carlo": r.get("monte_carlo")}, ensure_ascii=False, default=str, indent=2))
        else:
            r = close_vs_next_open(db, tuple(args.engines), start, end, args.slippage_bps)
            print(json.dumps({"close": r["close"]["summary"], "next_open": r["next_open"]["summary"], "gap": r["gap_buckets"]}, ensure_ascii=False, default=str, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
