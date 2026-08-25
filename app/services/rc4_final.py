from __future__ import annotations

"""V18 Adaptive Money-Wave RC4 — A Runner account layer.

Separate from strategy_reference_v18.py.

Recovered/frozen A Runner:
- proof >= 3.0H at original A exit decision;
- decision-day 5-trading-day close return <= -8%;
- original exit executes next open: sell 85%, retain 15%;
- fresh A/B may force Runner to yield first;
- otherwise 3 consecutive closes below simple MA30 -> next open exit;
- no fixed time stop.
"""

from dataclasses import dataclass
from typing import Optional
import numpy as np
import pandas as pd

RC4_VERSION = "V18 Adaptive Money-Wave RC4 FINAL"
RC4_RUNNER_FRACTION = 0.15
RC4_PROOF_H = 3.0
RC4_WASH5 = -0.08
RC4_MA_WINDOW = 30
RC4_MA_BREAK_DAYS = 3


@dataclass(frozen=True)
class RunnerEligibility:
    eligible: bool
    proof_h: float
    ret5: float
    reason: str


@dataclass(frozen=True)
class TailExit:
    decision_idx: Optional[int]
    exit_idx: Optional[int]
    decision_date: Optional[pd.Timestamp]
    exit_date: Optional[pd.Timestamp]
    exit_open: Optional[float]
    reason: str


def _clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    for col in ("date", "open", "close"):
        if col not in df.columns:
            raise ValueError(f"missing {col} column")
    z = df.copy()
    z["date"] = pd.to_datetime(z["date"])
    z["open"] = pd.to_numeric(z["open"], errors="coerce")
    z["close"] = pd.to_numeric(z["close"], errors="coerce")
    return z.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def add_ma30(df: pd.DataFrame) -> pd.DataFrame:
    z = _clean_frame(df)
    z["ma30"] = z["close"].rolling(RC4_MA_WINDOW, min_periods=RC4_MA_WINDOW).mean()
    return z


def decision_index_for_date(df: pd.DataFrame, decision_date) -> int | None:
    z = _clean_frame(df)
    target = pd.Timestamp(decision_date).normalize()
    hit = np.flatnonzero(z["date"].dt.normalize().to_numpy() == target)
    return int(hit[0]) if len(hit) else None


def rc4_a_runner_eligibility(
    h_daily: float,
    mfe: float,
    df: pd.DataFrame,
    decision_idx: int,
    proof_h_min: float = RC4_PROOF_H,
    wash5_max: float = RC4_WASH5,
) -> RunnerEligibility:
    z = _clean_frame(df)
    if decision_idx < 0 or decision_idx >= len(z):
        raise IndexError("decision_idx out of range")

    H = float(h_daily)
    M = float(mfe)
    proof_h = M / H if np.isfinite(H) and H > 0 and np.isfinite(M) else np.nan

    ret5 = np.nan
    if decision_idx >= 5:
        c0 = float(z.at[decision_idx - 5, "close"])
        c1 = float(z.at[decision_idx, "close"])
        if np.isfinite(c0) and c0 > 0 and np.isfinite(c1):
            ret5 = c1 / c0 - 1

    if not np.isfinite(proof_h):
        return RunnerEligibility(False, proof_h, ret5, "invalid_proof")
    if proof_h < proof_h_min:
        return RunnerEligibility(False, proof_h, ret5, "proof_below_3H")
    if not np.isfinite(ret5):
        return RunnerEligibility(False, proof_h, ret5, "ret5_unavailable")
    if ret5 > wash5_max:
        return RunnerEligibility(False, proof_h, ret5, "wash_not_deep_enough")
    return RunnerEligibility(True, proof_h, ret5, "proof3H_and_wash8")


def rc4_a_tail_technical_exit(
    df: pd.DataFrame,
    base_exit_idx: int,
    consecutive_days: int = RC4_MA_BREAK_DAYS,
) -> TailExit:
    if consecutive_days < 1:
        raise ValueError("consecutive_days must be >= 1")
    z = add_ma30(df)
    if base_exit_idx < 0 or base_exit_idx >= len(z):
        raise IndexError("base_exit_idx out of range")

    count = 0
    for i in range(base_exit_idx, len(z)):
        close = float(z.at[i, "close"])
        ma30 = float(z.at[i, "ma30"]) if pd.notna(z.at[i, "ma30"]) else np.nan
        count = count + 1 if np.isfinite(ma30) and close < ma30 else 0
        if count < consecutive_days:
            continue
        j = i + 1
        if j >= len(z):
            return TailExit(
                i, None, pd.Timestamp(z.at[i, "date"]), None, None,
                "MA30_3close_confirmed_but_no_next_bar",
            )
        return TailExit(
            i, j, pd.Timestamp(z.at[i, "date"]), pd.Timestamp(z.at[j, "date"]),
            float(z.at[j, "open"]), "MA30_3close_next_open",
        )

    return TailExit(None, None, None, None, None, "data_end_no_technical_exit")


def rc4_status() -> dict:
    return {
        "version": RC4_VERSION,
        "runner_fraction": RC4_RUNNER_FRACTION,
        "eligibility_activation": True,
        "eligibility": "A proof >= 3H AND decision-day 5T return <= -8%",
        "proof_h_min": RC4_PROOF_H,
        "wash5_max": RC4_WASH5,
        "ma_window": RC4_MA_WINDOW,
        "break_days": RC4_MA_BREAK_DAYS,
        "time_cap": None,
        "core_exit": "85% at original A exit next open",
        "technical_exit": "3 consecutive closes below MA30 -> next open",
        "capacity_priority": "fresh A/B forces Runner to yield before C",
        "scope": "ABC account A-Runner layer; frozen V18 signal generator untouched",
        "xd_overlay": False,
    }
