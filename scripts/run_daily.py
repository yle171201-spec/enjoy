#!/usr/bin/env python3
from app.db import init_db, SessionLocal
from app.services.data_update import sync_market
from app.services.strategy_service import run_full_scan


def main():
    init_db()
    db = SessionLocal()
    try:
        updated = sync_market(db)
        print(f"data update completed: {len(updated)} stocks")
        sig, cmp, diag = run_full_scan(db)
        print("scan:", diag)
        print("golden:", cmp)
        print("latest signals:", len(sig))
    finally:
        db.close()


if __name__ == "__main__":
    main()
