from __future__ import annotations

from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo
import threading
import pandas as pd
import numpy as np
import akshare as ak
import baostock as bs

from .base import DataProvider


_FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
_BS_LOCK = threading.RLock()
_SH = ZoneInfo("Asia/Shanghai")
_EOD_SNAPSHOT_CUTOFF = time(16, 10)


class DailySnapshotNotReady(RuntimeError):
    """Raised when an all-market spot snapshot is not safe to persist as a daily bar."""



def _eligible_code(code: str) -> bool:
    c = str(code).zfill(6)
    return c.startswith(("0", "3", "6")) and not c.startswith(("688", "689"))


def _board(code: str) -> str:
    return "创业板" if str(code).startswith("3") else "主板"


def _to_num(s):
    return pd.to_numeric(s, errors="coerce")


class _BaoSession:
    def __enter__(self):
        _BS_LOCK.acquire()
        lg = bs.login()
        if getattr(lg, "error_code", "1") != "0":
            _BS_LOCK.release()
            raise RuntimeError(f"BaoStock login failed: {getattr(lg, 'error_msg', '')}")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            bs.logout()
        finally:
            _BS_LOCK.release()


class PublicDataProvider(DataProvider):
    """No-token public A-share provider.

    * Historical daily bars: BaoStock (unadjusted, turnover + amount included)
    * Post-close one-shot daily update: AKShare Eastmoney all-A snapshot
    * If BaoStock history is unavailable, falls back to AKShare history.

    Data contract is normalized to:
      volume = shares, amount = CNY yuan, turnover = decimal ratio.
    """

    name = "public"
    # BaoStock's Python client is stateful; history calls are serialized.
    max_workers = 1

    def stock_list(self) -> pd.DataFrame:
        try:
            with _BaoSession():
                rs = bs.query_stock_basic()
                rows = []
                while (rs.error_code == "0") & rs.next():
                    rows.append(rs.get_row_data())
                if rs.error_code != "0":
                    raise RuntimeError(rs.error_msg)
                x = pd.DataFrame(rows, columns=rs.fields)
            if x.empty:
                raise RuntimeError("BaoStock stock_basic returned empty")
            x = x[(x["type"] == "1") & (x["status"] == "1")].copy()
            x["market"] = x["code"].str[:2].str.upper()
            x["code"] = x["code"].str.split(".").str[-1].str.zfill(6)
            x = x[x["code"].map(_eligible_code)]
            x["name"] = x["code_name"].fillna("").astype(str)
            x["board"] = x["code"].map(_board)
            x["is_st"] = x["name"].str.upper().str.contains("ST", na=False)
            return x[["code", "name", "market", "board", "is_st"]].drop_duplicates("code")
        except Exception:
            return self._ak_stock_list()

    def _ak_stock_list(self) -> pd.DataFrame:
        x = ak.stock_zh_a_spot_em().copy().rename(columns={"代码": "code", "名称": "name"})
        x["code"] = x["code"].astype(str).str.zfill(6)
        x = x[x["code"].map(_eligible_code)].copy()
        x["market"] = x["code"].map(lambda c: "SH" if c.startswith("6") else "SZ")
        x["board"] = x["code"].map(_board)
        x["is_st"] = x["name"].fillna("").astype(str).str.upper().str.contains("ST")
        return x[["code", "name", "market", "board", "is_st"]].drop_duplicates("code")

    def history(self, code: str, start: date, end: date) -> pd.DataFrame:
        code = str(code).zfill(6)
        try:
            return self._history_baostock(code, start, end)
        except Exception:
            return self._history_akshare(code, start, end)

    def _history_baostock(self, code: str, start: date, end: date) -> pd.DataFrame:
        bs_code = ("sh." if code.startswith("6") else "sz.") + code
        with _BaoSession():
            rs = bs.query_history_k_data_plus(
                bs_code,
                _FIELDS,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag="3",  # raw prices: structural levels match executable prices
            )
            rows = []
            while (rs.error_code == "0") & rs.next():
                rows.append(rs.get_row_data())
            if rs.error_code != "0":
                raise RuntimeError(rs.error_msg)
            x = pd.DataFrame(rows, columns=rs.fields)
        cols = ["date", "open", "high", "low", "close", "volume", "amount", "turnover"]
        if x.empty:
            return pd.DataFrame(columns=cols)
        x = x[x["tradestatus"].astype(str) == "1"].copy()
        x["date"] = pd.to_datetime(x["date"], errors="coerce")
        x["turnover"] = _to_num(x["turn"]) / 100.0
        for c in ["open", "high", "low", "close", "volume", "amount"]:
            x[c] = _to_num(x[c])
        x = x[(x["close"] > 0) & (x["open"] > 0)]
        return x[cols].dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)

    def _history_akshare(self, code: str, start: date, end: date) -> pd.DataFrame:
        x = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="",
            timeout=20,
        )
        cols = ["date", "open", "high", "low", "close", "volume", "amount", "turnover"]
        if x is None or x.empty:
            return pd.DataFrame(columns=cols)
        x = x.rename(columns={
            "日期": "date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
            "成交量": "volume", "成交额": "amount", "换手率": "turnover",
        })
        x["date"] = pd.to_datetime(x["date"], errors="coerce")
        # Eastmoney history volume is in lots; normalize to shares.
        x["volume"] = _to_num(x["volume"]) * 100.0
        x["turnover"] = _to_num(x["turnover"]) / 100.0
        for c in ["open", "high", "low", "close", "amount"]:
            x[c] = _to_num(x[c])
        return x[cols].dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)

    def trade_dates(self, start: date, end: date) -> list[date]:
        try:
            with _BaoSession():
                rs = bs.query_trade_dates(start_date=start.isoformat(), end_date=end.isoformat())
                rows = []
                while (rs.error_code == "0") & rs.next():
                    rows.append(rs.get_row_data())
                if rs.error_code != "0":
                    raise RuntimeError(rs.error_msg)
                x = pd.DataFrame(rows, columns=rs.fields)
            return [pd.Timestamp(d).date() for d in x.loc[x["is_trading_day"] == "1", "calendar_date"]]
        except Exception:
            # HTTPS fallback via AKShare/Sina exchange calendar.
            cal = ak.tool_trade_date_hist_sina()
            col = "trade_date" if "trade_date" in cal.columns else cal.columns[0]
            ds = pd.to_datetime(cal[col], errors="coerce").dt.date
            return sorted(d for d in ds.dropna() if start <= d <= end)

    def latest_completed_trade_date(self, now: datetime | None = None) -> date:
        now = now or datetime.now(_SH)
        if now.tzinfo is None:
            now = now.replace(tzinfo=_SH)
        else:
            now = now.astimezone(_SH)
        # Before 16:00 CST, today's bar is not treated as completed.
        cutoff = now.date() if now.time() >= time(16, 0) else now.date() - timedelta(days=1)
        days = self.trade_dates(cutoff - timedelta(days=15), cutoff)
        if not days:
            raise RuntimeError("cannot resolve latest completed A-share trading day")
        return max(days)

    def snapshot_trade_date(self, now: datetime | None = None) -> date:
        """Return the *current* trading date only when a spot snapshot is safe to persist.

        AKShare ``stock_zh_a_spot_em`` has no authoritative trade-date column. Therefore
        it must never be relabeled as yesterday's bar. We only accept it on an actual
        A-share trading day and after a conservative Shanghai-time close cutoff.
        """
        now = now or datetime.now(_SH)
        if now.tzinfo is None:
            now = now.replace(tzinfo=_SH)
        else:
            now = now.astimezone(_SH)

        today = now.date()
        trading_today = today in set(self.trade_dates(today, today))
        if not trading_today:
            raise DailySnapshotNotReady(f"{today} 不是A股交易日；实时快照不会写入历史日线")
        if now.time() < _EOD_SNAPSHOT_CUTOFF:
            raise DailySnapshotNotReady(
                f"北京时间 {_EOD_SNAPSHOT_CUTOFF.strftime('%H:%M')} 前禁止写入当日日线；当前 {now.strftime('%H:%M:%S')}"
            )
        return today

    def daily_snapshot(self, trade_date: date | None = None, now: datetime | None = None) -> pd.DataFrame:
        target = self.snapshot_trade_date(now)
        if trade_date is not None and trade_date != target:
            raise DailySnapshotNotReady(
                "AKShare 全市场实时快照只能写入当前交易日收盘数据，不能用于补写历史日期"
            )

        x = ak.stock_zh_a_spot_em().copy()
        x = x.rename(columns={
            "代码": "code", "名称": "name", "最新价": "close", "最高": "high", "最低": "low",
            "今开": "open", "成交量": "volume", "成交额": "amount", "换手率": "turnover",
        })
        x["code"] = x["code"].astype(str).str.zfill(6)
        x = x[x["code"].map(_eligible_code)].copy()
        for c in ["open", "high", "low", "close", "volume", "amount", "turnover"]:
            x[c] = _to_num(x[c])
        # Eastmoney spot volume is in lots; normalize to shares.
        x["volume"] = x["volume"] * 100.0
        x["turnover"] = x["turnover"] / 100.0
        x["market"] = x["code"].map(lambda c: "SH" if c.startswith("6") else "SZ")
        x["board"] = x["code"].map(_board)
        x["is_st"] = x["name"].fillna("").astype(str).str.upper().str.contains("ST")
        x["date"] = pd.Timestamp(target)
        x = x[(x["close"] > 0) & (x["open"] > 0) & (x["high"] >= x["low"])]
        cols = [
            "code", "name", "market", "board", "is_st", "date", "open", "high", "low", "close",
            "volume", "amount", "turnover",
        ]
        x = x[cols].dropna(subset=["code", "close", "amount", "turnover"])
        if len(x) < 3000:
            raise RuntimeError(f"AKShare bulk snapshot looks incomplete: only {len(x)} eligible rows")
        return x.reset_index(drop=True)
