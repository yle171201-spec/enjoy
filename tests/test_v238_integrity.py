from datetime import date
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import pandas as pd

from app.db import Base
from app.models import Signal
from app.services.backtest import _signals_query
from app.services.execution_engine import (
    ExecutedTrade, ExecutionParams, execute_signals, materialize_next_open_exit,
)


def test_next_open_exit_materializes_following_open():
    d = pd.bdate_range("2026-01-05", periods=5)
    frame = pd.DataFrame({
        "date": d, "open": [10.0,10.1,9.9,9.4,9.2],
        "high": [10.2,10.2,10.0,9.6,9.4], "low": [9.8,9.9,9.7,9.2,9.0],
        "close": [10.0,10.0,9.8,9.3,9.1], "volume": 1_000_000.0,
        "amount": 50_000_000.0, "turnover": .02,
    })
    t = ExecutedTrade(
        None,"000001","B",d[0].date(),10.0,9.0,.1,None,
        d[1].date(),10.1,1,d[2].date(),9.8,2,
        9.8/10.1-1,9.8/10.1-1,"首回踩失效",.1,.2,.01,.03,-.04,None,
    )
    out = materialize_next_open_exit(t, frame, ExecutionParams(mode="next_open"))
    assert out.exit_date == d[3].date()
    assert abs(out.exit_price - 9.4) < 1e-12
    assert out.exit_idx == 3


def test_execute_signals_reports_missing_frame():
    class S:
        id=1; code="000001"; engine="A"; signal_date=date(2026,1,5)
    errors=[]
    out=execute_signals([S()], {}, ExecutionParams(), errors=errors)
    assert out == []
    assert len(errors) == 1
    assert errors[0]["stage"] == "missing_frame"


def test_signal_query_merges_v18_live_and_prefers_full_v18():
    engine=create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        common=dict(
            code="000001", signal_date=date(2026,1,5), engine="A",
            signal_close=10.0, fail_price=9.0, risk_pct=.1, target_weight=.2,
            h_daily=.1, p_level=9.5, metadata_json="{}",
        )
        db.add(Signal(strategy_version="V18-LIVE", **common))
        db.add(Signal(strategy_version="V18", exit_reason="趋势破坏", **common))
        db.add(Signal(
            strategy_version="V18-LIVE", code="000002", signal_date=date(2026,1,6),
            engine="B", signal_close=20.0, fail_price=18.0, risk_pct=.1,
            target_weight=.2, h_daily=.1, metadata_json="{}",
        ))
        db.commit()
        rows=_signals_query(db, ("A","B"))
        assert len(rows) == 2
        a=next(x for x in rows if x.code=="000001")
        assert a.strategy_version == "V18"
        assert a.exit_reason == "趋势破坏"
        b=next(x for x in rows if x.code=="000002")
        assert b.strategy_version == "V18-LIVE"
