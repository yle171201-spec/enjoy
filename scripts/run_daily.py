#!/usr/bin/env python3
from app.db import init_db, SessionLocal
from app.services.data_update import sync_daily_public
from app.services.strategy_service import run_full_scan


def main():
    init_db()
    db = SessionLocal()
    try:
        update = sync_daily_public(db)
        print("public EOD update:", update)
        sig, cmp, diag = run_full_scan(db)
        print("scan:", diag)
        print("golden:", cmp if cmp else "live-window only / golden not evaluated")
        print("signals in live scan window:", len(sig))
    finally:
        db.close()


if __name__ == "__main__":
    main()
