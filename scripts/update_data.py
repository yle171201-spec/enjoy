#!/usr/bin/env python3
import argparse
from datetime import date
from app.db import init_db,SessionLocal
from app.services.data_update import sync_market
def main():
    ap=argparse.ArgumentParser();ap.add_argument("--start");ap.add_argument("--end");ap.add_argument("--workers",type=int,default=6);ap.add_argument("--limit",type=int);a=ap.parse_args();init_db();db=SessionLocal()
    try:print("updated",len(sync_market(db,date.fromisoformat(a.start) if a.start else None,date.fromisoformat(a.end) if a.end else None,a.workers,a.limit)),"stocks")
    finally:db.close()
if __name__=="__main__":main()
