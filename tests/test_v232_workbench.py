from __future__ import annotations

from datetime import date

from app.services.review_service import (
    _combo_engines,
    _diagnosis_flags,
    _summary,
)


def _row(engine, ret, mfe, mae, post20=None, rating=""):
    row = {
        "engine": engine,
        "exit_ret": ret,
        "reviewed": bool(rating),
        "rating": rating,
        "signal_date": date(2026, 1, 1),
        "code": "000001",
        "metrics": {
            "mfe": mfe,
            "mae": mae,
            "r5": ret,
            "r10": ret,
            "r20": ret,
            "post_exit_r20": post20,
        },
    }
    row["diagnosis"] = _diagnosis_flags(row)
    return row


def test_combo_engine_mapping():
    assert _combo_engines("A") == ("A",)
    assert _combo_engines("AB") == ("A", "B")
    assert _combo_engines("BC") == ("B", "C")
    assert _combo_engines("ALL") == ("A", "B", "C")
    assert _combo_engines("bad") == ("A", "B", "C")


def test_summary_has_signal_quality_metrics():
    rows = [
        _row("A", 0.20, 0.30, -0.05),
        _row("A", -0.10, 0.02, -0.15),
        _row("B", 0.10, 0.20, -0.04),
        _row("C", -0.05, 0.06, -0.08),
    ]
    s = _summary(rows)
    assert s["total"] == 4
    assert s["wins"] == 2
    assert s["win_rate"] == 0.5
    assert round(s["avg_ret"], 6) == 0.0375
    assert round(s["median_ret"], 6) == 0.025
    assert round(s["profit_factor"], 6) == 2.0
    assert round(s["payoff"], 6) == 2.0
    assert round(s["avg_mfe"], 6) == 0.145
    assert round(s["avg_mae"], 6) == -0.08


def test_diagnosis_flags_are_independent():
    direct = _row("A", -0.2, 0.01, -0.23)
    assert "DIRECT_FAIL" in direct["diagnosis"]

    giveback = _row("B", 0.02, 0.20, -0.05)
    assert "GIVEBACK" in giveback["diagnosis"]

    sold = _row("A", 0.03, 0.08, -0.04, post20=0.20)
    assert "SOLD_RALLY" in sold["diagnosis"]

    excellent = _row("A", 0.15, 0.22, -0.04)
    assert "EXCELLENT" in excellent["diagnosis"]

    survivor = _row("B", 0.08, 0.18, -0.16)
    assert "HIGH_VOL" in survivor["diagnosis"]
