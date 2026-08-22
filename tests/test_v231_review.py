from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from app.services.review_service import _signal_metrics, _filter_rows, _sort_rows


def _bar(d, o, h, l, c):
    return SimpleNamespace(trade_date=d, open=o, high=h, low=l, close=c)


def test_review_metrics_start_after_signal_close():
    d0 = date(2026, 1, 2)
    bars = [
        _bar(d0, 10, 99, 1, 10),
        _bar(d0 + timedelta(days=1), 10.2, 11, 9.5, 10.5),
        _bar(d0 + timedelta(days=2), 10.4, 12, 9.8, 11.0),
        _bar(d0 + timedelta(days=3), 10.9, 11.5, 9.0, 9.5),
        _bar(d0 + timedelta(days=4), 9.6, 10.2, 9.2, 10.0),
        _bar(d0 + timedelta(days=5), 10.1, 10.8, 10.0, 10.5),
    ]
    sig = SimpleNamespace(
        signal_date=d0,
        signal_close=10.0,
        exit_date=d0 + timedelta(days=4),
    )
    m = _signal_metrics(sig, bars)
    assert round(m["next_open"], 6) == 10.2
    assert round(m["next_open_gap"], 6) == 0.02
    assert round(m["mfe"], 6) == 0.20
    assert round(m["mae"], 6) == -0.10
    assert m["mfe_day"] == 2
    assert m["mae_day"] == 3
    assert m["hold_bars"] == 4
    assert round(m["r5"], 6) == 0.05


def test_review_filter_and_sort():
    rows = [
        {"engine":"A","exit_ret":0.2,"reviewed":False,"rating":"","signal_date":date(2026,1,1),"code":"000001","metrics":{"mfe":0.3,"mae":-0.05}},
        {"engine":"B","exit_ret":-0.1,"reviewed":True,"rating":"合格","signal_date":date(2026,1,2),"code":"000002","metrics":{"mfe":0.05,"mae":-0.15}},
    ]
    assert len(_filter_rows(rows, engine="A")) == 1
    assert len(_filter_rows(rows, outcome="LOSS")) == 1
    assert len(_filter_rows(rows, rating="UNREVIEWED")) == 1
    assert _sort_rows(rows, sort="RET_DESC")[0]["code"] == "000001"
