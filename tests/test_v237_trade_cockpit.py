from types import SimpleNamespace

def _calc(signal, entry_price, account_equity=0):
    risk = (entry_price - signal.fail_price) / entry_price
    budget = .015 if signal.engine == "C" else .025
    weight = min(.20, budget / risk)
    buy_amount = account_equity * weight if account_equity > 0 else None
    lot_shares = int(buy_amount // (entry_price * 100)) * 100 if buy_amount is not None else None
    return risk, weight, buy_amount, lot_shares


def test_position_size_matches_risk_budget():
    s = SimpleNamespace(engine="A", fail_price=9.0)
    risk, weight, _, _ = _calc(s, 10.0)
    assert abs(risk - .10) < 1e-12
    assert abs(weight - .20) < 1e-12  # capped at 20%


def test_optional_equity_converts_to_board_lot():
    s = SimpleNamespace(engine="B", fail_price=9.0)
    _, weight, amount, shares = _calc(s, 10.0, 100000)
    assert weight == .20
    assert amount == 20000
    assert shares == 2000
