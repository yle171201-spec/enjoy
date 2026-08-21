#!/usr/bin/env python3
import argparse
from pathlib import Path
import pandas as pd
from app.db import init_db,SessionLocal
from app.models import Stock
from app.services.repository import upsert_bars
def main():
    ap=argparse.ArgumentParser();ap.add_argument("directory");ap.add_argument("--limit",type=int);a=ap.parse_args();init_db();db=SessionLocal();files=sorted(Path(a.directory).glob("*.parquet"));files=files[:a.limit] if a.limit else files
    try:
        for i,p in enumerate(files,1):
            code=p.stem.zfill(6);x=pd.read_parquet(p)
            if pd.api.types.is_integer_dtype(x.date):x["date"]=pd.to_datetime(x.date,unit="D",origin="unix")
            else:x["date"]=pd.to_datetime(x.date)
            if db.get(Stock,code) is None:db.add(Stock(code=code,name=""));db.commit()
            print(i,code,upsert_bars(db,code,x[["date","open","high","low","close","volume","amount","turnover"]]))
    finally:db.close()
if __name__=="__main__":main()
