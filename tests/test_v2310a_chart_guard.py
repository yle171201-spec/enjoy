from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import DailyBar, LiveMarketState, ScanRun
from app.services.backtest import _freshness_diag


def test_freshness_detects_strategy_state_behind_data():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(DailyBar(
            code="000001", trade_date=date(2026, 8, 24),
            open=10, high=10, low=10, close=10,
            volume=1, amount=1, turnover=.01,
        ))
        db.add(ScanRun(
            data_date=date(2026, 3, 2), status="ok",
            a_count=0, b_count=0, c_count=0, combined_count=0,
        ))
        db.add(LiveMarketState(
            strategy_version="V18",
            trade_date=date(2026, 3, 2),
        ))
        db.commit()

        d = _freshness_diag(db, date(2026, 3, 2))
        assert d["data_end"] == date(2026, 8, 24)
        assert d["covered_through"] == date(2026, 3, 2)
        assert d["signal_history_stale"] is True
