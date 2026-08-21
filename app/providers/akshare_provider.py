from datetime import date
import pandas as pd
import akshare as ak
from .base import DataProvider
class AkShareProvider(DataProvider):
    def stock_list(self):
        x=ak.stock_zh_a_spot_em().copy().rename(columns={"代码":"code","名称":"name"})
        x["code"]=x["code"].astype(str).str.zfill(6);x=x[x.code.str.startswith(("0","3","6"))&~x.code.str.startswith(("688","689"))]
        x["market"]=x.code.map(lambda c:"SH" if c.startswith("6") else "SZ");x["board"]=x.code.map(lambda c:"创业板" if c.startswith("3") else "主板");x["is_st"]=x.name.fillna("").str.upper().str.contains("ST")
        return x[["code","name","market","board","is_st"]].drop_duplicates("code")
    def history(self,code,start,end):
        x=ak.stock_zh_a_hist(symbol=str(code).zfill(6),period="daily",start_date=start.strftime("%Y%m%d"),end_date=end.strftime("%Y%m%d"),adjust="")
        cols=["date","open","high","low","close","volume","amount","turnover"]
        if x is None or x.empty:return pd.DataFrame(columns=cols)
        x=x.rename(columns={"日期":"date","开盘":"open","最高":"high","最低":"low","收盘":"close","成交量":"volume","成交额":"amount","换手率":"turnover"})
        x["date"]=pd.to_datetime(x.date);x["turnover"]=pd.to_numeric(x.turnover,errors="coerce")/100.0
        for c in ["open","high","low","close","volume","amount"]:x[c]=pd.to_numeric(x[c],errors="coerce")
        return x[cols].dropna(subset=["close"])
