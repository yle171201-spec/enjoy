from datetime import date, timedelta

import pandas as pd

from app.services.rc4_final import rc4_a_tail_technical_exit, rc4_status


def _frame(closes):
    start = date(2026, 1, 1)
    return pd.DataFrame({
        "date": [start + timedelta(days=i) for i in range(len(closes))],
        "open": [float(x) + 0.5 for x in closes],
        "close": [float(x) for x in closes],
    })


def test_rc4_status_has_no_time_cap():
    s = rc4_status()
    assert s["runner_fraction"] == 0.15
    assert s["ma_window"] == 30
    assert s["break_days"] == 3
    assert s["time_cap"] is None
    assert s["eligibility_activation"] is False


def test_three_consecutive_below_ma30_then_next_open():
    closes = [100.0] * 35 + [99.0, 98.0, 97.0, 96.0]
    df = _frame(closes)
    out = rc4_a_tail_technical_exit(df, 35)
    assert out.reason == "MA30_3close_next_open"
    assert out.decision_idx == 37
    assert out.exit_idx == 38
    assert out.exit_open == 96.5


def test_reclaim_ma30_resets_counter():
    closes = [100.0] * 35 + [99.0, 98.0, 101.0, 99.0, 98.0, 97.0, 96.0]
    df = _frame(closes)
    out = rc4_a_tail_technical_exit(df, 35)
    assert out.reason == "MA30_3close_next_open"
    assert out.decision_idx == 40
    assert out.exit_idx == 41


def test_data_end_does_not_invent_time_stop():
    closes = [100.0] * 40
    df = _frame(closes)
    out = rc4_a_tail_technical_exit(df, 35)
    assert out.reason == "data_end_no_technical_exit"
    assert out.exit_idx is None
