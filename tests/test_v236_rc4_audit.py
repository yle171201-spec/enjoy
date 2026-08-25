from datetime import date

from app.services.backtest import compare_portfolio_results


def _result(terminal, cagr, mdd, end, accepted=10, tails=0):
    return {
        "metrics": {
            "terminal": terminal,
            "cagr": cagr,
            "mdd": mdd,
            "accepted": accepted,
            "rc4_tail_created": tails,
        },
        "equity": [
            {"date": date(2025, 1, 1), "equity": 1.0},
            {"date": end, "equity": terminal},
        ],
    }


def test_comparison_aligns_baseline_to_later_rc4_end():
    baseline = _result(1.20, 0.20, -0.10, date(2025, 12, 31), 10, 0)
    rc4 = _result(1.30, 0.25, -0.11, date(2026, 6, 30), 10, 2)
    c = compare_portfolio_results(rc4, baseline)

    assert c["same_start"] is True
    assert c["common_end"] == date(2026, 6, 30)
    assert c["rc4_terminal"] == 1.30
    assert c["baseline_terminal"] == 1.20
    assert abs(c["terminal_delta"] - 0.10) < 1e-12
    assert c["tail_created"] == 2
    assert c["rc4_cagr"] > c["baseline_cagr"]


def test_mdd_delta_positive_means_less_drawdown():
    baseline = _result(1.10, 0.10, -0.12, date(2025, 12, 31))
    rc4 = _result(1.12, 0.11, -0.10, date(2025, 12, 31))
    c = compare_portfolio_results(rc4, baseline)
    assert abs(c["mdd_delta"] - 0.02) < 1e-12
