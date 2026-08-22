from __future__ import annotations

import copy
import numpy as np
import pandas as pd

import app.engine.strategy_reference_v18 as eng
from app.engine.strategy_reference_v18 import prepare_stock
from app.services.live_scan import _latest_raw_metrics, _pad_prepared_for_live


def _raw(seed=1, n=380):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2024-01-02", periods=n)
    close = 15 * np.exp(np.cumsum(rng.normal(0.0003, 0.018, n)))
    open_ = close * (1 + rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + rng.uniform(0.001, 0.02, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.001, 0.02, n))
    volume = rng.uniform(5e5, 5e6, n)
    amount = volume * close
    turnover = rng.uniform(0.006, 0.06, n)
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "amount": amount, "turnover": turnover,
    })


def test_latest_raw_metrics_matches_canonical_prepare():
    for seed in range(1, 8):
        raw = _raw(seed)
        target = pd.Timestamp(raw.date.iloc[-1]).date()
        m = _latest_raw_metrics(raw, target)
        z = prepare_stock(raw, "000001")
        t = len(z) - 1
        assert np.isclose(m["abase"], z["abase"].iloc[t], equal_nan=True)
        assert np.isclose(m["tbase"], z["tbase"].iloc[t], equal_nan=True)
        assert np.isclose(m["above20"], float(z["above20_flag"].iloc[t]), equal_nan=True)
        assert np.isclose(m["mom20"], float(z["mom20_flag"].iloc[t]), equal_nan=True)


def test_live_padding_preserves_original_rows():
    z = prepare_stock(_raw(9), "000001")
    p = _pad_prepared_for_live(z, 35)
    assert len(p) == len(z) + 35
    for col in ["open", "high", "low", "close", "ma10", "ma20", "atr20", "abase", "tbase"]:
        np.testing.assert_allclose(
            z[col].to_numpy(float),
            p[col].iloc[:len(z)].to_numpy(float),
            rtol=0, atol=0, equal_nan=True,
        )
    assert not p["board"].iloc[len(z):].any()
    assert not p["weekly_up"].iloc[len(z):].any()


def test_state_callback_does_not_change_engine_output(monkeypatch):
    # The canonical V18 has a pre-existing all-empty edge case in combine_abc():
    # if synthetic data produces no A/B/C rows, it raises KeyError("code").
    # Patch ONLY that unrelated test edge, so this test isolates state_cb.
    original_combine = eng.combine_abc

    def combine_allow_all_empty(a, b, c):
        if a.empty and b.empty and c.empty:
            return pd.DataFrame(columns=["code", "date", "engine", "buy", "fail_price"])
        return original_combine(a, b, c)

    monkeypatch.setattr(eng, "combine_abc", combine_allow_all_empty)

    frames = {
        "000001": _raw(11, 330),
        "000002": _raw(12, 330),
        "300001": _raw(13, 330),
        "600001": _raw(14, 330),
    }
    s0, d0 = eng.run_close_reference(
        copy.deepcopy(frames), run_exits=False, memory_safe=False
    )

    captured = {"market": 0, "a60": 0}

    def cb(kind, payload):
        if kind == "MARKET":
            captured["market"] += 1
        elif kind == "A60":
            captured["a60"] += len(payload)

    s1, d1 = eng.run_close_reference(
        copy.deepcopy(frames),
        run_exits=False,
        memory_safe=False,
        state_cb=cb,
    )

    assert d0 == d1
    assert captured["market"] == 1
    cols = ["code", "date", "engine", "buy", "fail_price", "target_weight"]
    if s0.empty:
        assert s1.empty
    else:
        pd.testing.assert_frame_equal(
            s0[cols].reset_index(drop=True),
            s1[cols].reset_index(drop=True),
            check_dtype=False,
        )
