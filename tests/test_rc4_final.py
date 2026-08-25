from datetime import date, timedelta
import pandas as pd

from app.services.rc4_final import (
    rc4_a_runner_eligibility,
    rc4_a_tail_technical_exit,
    rc4_status,
)


def _frame(closes):
    start = date(2026, 1, 1)
    return pd.DataFrame({
        "date": [start + timedelta(days=i) for i in range(len(closes))],
        "open": [float(x) + 0.5 for x in closes],
        "close": [float(x) for x in closes],
    })


def test_status_has_recovered_eligibility():
    s = rc4_status()
    assert s["eligibility_activation"] is True
    assert s["runner_fraction"] == 0.15
    assert s["proof_h_min"] == 3.0
    assert s["wash5_max"] == -0.08
    assert s["time_cap"] is None


def test_runner_requires_both_3h_and_wash8():
    df = _frame([100.0] * 6 + [99.0, 96.0, 94.0, 92.0, 90.0])
    yes = rc4_a_runner_eligibility(0.10, 0.31, df, 10)
    assert yes.eligible is True
    assert yes.proof_h >= 3.0
    assert yes.ret5 <= -0.08

    no_proof = rc4_a_runner_eligibility(0.10, 0.29, df, 10)
    assert no_proof.eligible is False

    shallow = _frame([100.0] * 6 + [100.0, 99.0, 98.0, 97.0, 96.0])
    no_wash = rc4_a_runner_eligibility(0.10, 0.31, shallow, 10)
    assert no_wash.eligible is False


def test_three_consecutive_below_ma30_then_next_open():
    closes = [100.0] * 35 + [99.0, 98.0, 97.0, 96.0]
    out = rc4_a_tail_technical_exit(_frame(closes), 35)
    assert out.reason == "MA30_3close_next_open"
    assert out.decision_idx == 37
    assert out.exit_idx == 38
    assert out.exit_open == 96.5


def test_reclaim_ma30_resets_counter():
    closes = [100.0] * 35 + [99.0, 98.0, 101.0, 99.0, 98.0, 97.0, 96.0]
    out = rc4_a_tail_technical_exit(_frame(closes), 35)
    assert out.decision_idx == 40
    assert out.exit_idx == 41


def test_data_end_does_not_invent_time_stop():
    out = rc4_a_tail_technical_exit(_frame([100.0] * 40), 35)
    assert out.reason == "data_end_no_technical_exit"
    assert out.exit_idx is None
