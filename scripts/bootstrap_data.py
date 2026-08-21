#!/usr/bin/env python3
import argparse
from app.db import init_db, SessionLocal
from app.services.data_update import bootstrap_batch, data_stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()
    init_db()
    db = SessionLocal()
    try:
        r = bootstrap_batch(db, limit=args.limit)
        print("bootstrap batch:", len(r), "stocks")
        print("stats:", data_stats(db))
    finally:
        db.close()


if __name__ == "__main__":
    main()
