from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
import pandas as pd


class DataProvider(ABC):
    name = "base"
    max_workers = 4

    @abstractmethod
    def stock_list(self) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def history(self, code: str, start: date, end: date) -> pd.DataFrame:
        raise NotImplementedError

    def latest_completed_trade_date(self, now: datetime | None = None) -> date:
        """Latest exchange trading day whose close should already be complete."""
        return date.today()

    def daily_snapshot(self, trade_date: date | None = None) -> pd.DataFrame:
        """Optional one-shot all-market EOD snapshot.

        Return columns: code,name,market,board,is_st,date,open,high,low,close,
        volume,amount,turnover. Volume is standardized to shares and turnover to
        decimal ratio.
        """
        raise NotImplementedError
