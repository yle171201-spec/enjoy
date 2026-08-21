#!/usr/bin/env python3
from app.db import init_db,SessionLocal
from app.services.strategy_service import run_full_scan
if __name__=="__main__":
    init_db();db=SessionLocal()
    try:
        sig,cmp,diag=run_full_scan(db);print(diag);print(cmp)
    finally:db.close()
