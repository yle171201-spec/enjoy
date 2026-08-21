from datetime import date
import pandas as pd
import tushare as ts
from .base import DataProvider
class TushareProvider(DataProvider):
    def __init__(self,token):
        if not token:raise ValueError("TUSHARE_TOKEN is required")
        self.pro=ts.pro_api(token)
    def stock_list(self):
        x=self.pro.stock_basic(exchange="",list_status="L",fields="ts_code,symbol,name,market").rename(columns={"symbol":"code"})
        x["code"]=x.code.astype(str).str.zfill(6);x=x[x.code.str.startswith(("0","3","6"))&~x.code.str.startswith(("688","689"))]
        x["market2"]=x.ts_code.str[-2:];x["board"]=x.code.map(lambda c:"创业板" if c.startswith("3") else "主板");x["is_st"]=x.name.fillna("").str.upper().str.contains("ST")
        return x[["code","name","market2","board","is_st"]].rename(columns={"market2":"market"})
    def history(self,code,start,end):
        ts_code=str(code).zfill(6)+( ".SH" if str(code).startswith("6") else ".SZ")
        d=self.pro.daily(ts_code=ts_code,start_date=start.strftime("%Y%m%d"),end_date=end.strftime("%Y%m%d"));b=self.pro.daily_basic(ts_code=ts_code,start_date=start.strftime("%Y%m%d"),end_date=end.strftime("%Y%m%d"),fields="ts_code,trade_date,turnover_rate")
        cols=["date","open","high","low","close","volume","amount","turnover"]
        if d is None or d.empty:return pd.DataFrame(columns=cols)
        x=d.merge(b[["trade_date","turnover_rate"]],on="trade_date",how="left");x["date"]=pd.to_datetime(x.trade_date);x["amount"]=pd.to_numeric(x.amount,errors="coerce")*1000.0;x["turnover"]=pd.to_numeric(x.turnover_rate,errors="coerce")/100.0
        for c in ["open","high","low","close","vol"]:x[c]=pd.to_numeric(x[c],errors="coerce")
        x=x.rename(columns={"vol":"volume"});return x[cols].sort_values("date")
