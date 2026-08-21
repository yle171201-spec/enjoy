from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.engine.strategy_reference_v18 import target_weight, weekly_up_series
from app.services.execution_engine import ExecutionParams, ExecutedTrade, execute_signal
from app.services.portfolio import PortfolioParams, simulate_portfolio


def make_frame(n=320, start="2024-01-01", base=10.0):
    dates = pd.bdate_range(start, periods=n)
    close = np.full(n, base, dtype=float)
    return pd.DataFrame({
        "date": dates,
        "open": close.copy(), "high": close * 1.01, "low": close * .99, "close": close.copy(),
        "volume": np.full(n, 1_000_000.0), "amount": np.full(n, 50_000_000.0),
        "turnover": np.full(n, .02),
    })


def test_position_sizing():
    risk, w = target_weight(10, 9, "A")
    assert round(risk, 6) == .1 and round(w, 6) == .2
    risk, w = target_weight(10, 9, "C")
    assert round(w, 6) == .15


def test_weekly_uses_previous_completed_week():
    x = make_frame(260)
    # Find a Monday near the end; changing Monday's own close must not change Monday weekly filter.
    idx = next(i for i in range(220, len(x)) if x.date.iloc[i].weekday() == 0)
    a = weekly_up_series(x)[idx]
    y = x.copy()
    y.loc[idx, "close"] = 1000.0
    b = weekly_up_series(y)[idx]
    assert a == b


def test_next_open_recalculates_risk_and_weight():
    x = make_frame()
    sig_idx = 260
    x.loc[sig_idx, "close"] = 10.0
    x.loc[sig_idx + 1, ["open", "high", "low", "close"]] = [10.5, 10.7, 10.2, 10.4]
    s = SimpleNamespace(
        id=1, code="000001", engine="B", signal_date=x.date.iloc[sig_idx].date(),
        signal_close=10.0, fail_price=9.0, h_daily=.10, p_level=None,
        metadata_json="{}"
    )
    t = execute_signal(s, x, ExecutionParams(mode="next_open", skip_open_limit=True))
    assert t.skip_reason is None
    assert abs(t.entry_price - 10.5) < 1e-9
    assert abs(t.risk_pct - ((10.5 - 9.0) / 10.5)) < 1e-9
    assert abs(t.target_weight - .175) < 1e-9


def test_next_open_limit_lock_is_skipped():
    x = make_frame()
    sig_idx = 260
    x.loc[sig_idx, "close"] = 10.0
    x.loc[sig_idx + 1, ["open", "high", "low", "close"]] = [11.0, 11.0, 11.0, 11.0]
    s = SimpleNamespace(
        id=2, code="000001", engine="B", signal_date=x.date.iloc[sig_idx].date(),
        signal_close=10.0, fail_price=9.0, h_daily=.10, p_level=None,
        metadata_json="{}"
    )
    t = execute_signal(s, x, ExecutionParams(mode="next_open", skip_open_limit=True))
    assert t.skip_reason == "SKIP_LIMIT_LOCK"


def test_c_yields_to_new_ab_when_full():
    dates = pd.bdate_range("2025-01-02", periods=5)
    def f(px):
        return pd.DataFrame({
            "date": dates, "open": px, "high": np.array(px) * 1.01, "low": np.array(px) * .99,
            "close": px, "volume": 1_000_000.0, "amount": 50_000_000.0, "turnover": .02,
        })
    frames = {"000001": f([10,10,10,10,10]), "000002": f([20,20,20,20,20])}
    c = ExecutedTrade(None,"000001","C",dates[0].date(),10,9,.1,None,dates[0].date(),10,0,dates[3].date(),10,3,0,0,"数据末端",.1,.15,0,.1,-.1)
    a = ExecutedTrade(None,"000002","A",dates[1].date(),20,17,.1,18,dates[1].date(),20,1,dates[3].date(),20,3,0,0,"数据末端",.15,.20,0,.1,-.1)
    r = simulate_portfolio(
        [c,a], frames, ExecutionParams(mode="next_open"),
        PortfolioParams(max_positions=1,max_c_positions=1,c_yields_to_ab=True,random_seed=1)
    )
    assert r["metrics"]["accepted"] == 2
    assert r["metrics"]["c_replacements"] == 1


def test_portfolio_is_mark_to_market():
    dates = pd.bdate_range("2025-01-02", periods=5)
    frame = pd.DataFrame({
        "date": dates, "open": [10,10,8,9,10], "high": [10,10,8,9,10],
        "low": [10,10,8,9,10], "close": [10,10,8,9,10],
        "volume": 1_000_000.0, "amount": 50_000_000.0, "turnover": .02,
    })
    t = ExecutedTrade(None,"000001","A",dates[0].date(),10,8,.1,9,dates[0].date(),10,0,dates[4].date(),10,4,0,0,"数据末端",.2,.2,0,.1,-.2)
    r = simulate_portfolio([t], {"000001": frame}, ExecutionParams(mode="next_open"), PortfolioParams(max_positions=5))
    assert r["metrics"]["mdd"] < 0
