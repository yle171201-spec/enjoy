from __future__ import annotations

"""V18 Adaptive Money-Wave RC4 FINAL — verified tail-management hotfix.

IMPORTANT
---------
This module deliberately does NOT modify the frozen V18 signal generator.

Verified/frozen behavior implemented here:
1. Existing A Runner has NO fixed maximum holding period.
2. MA30 is the simple rolling mean of the latest 30 closes.
3. Technical exit is confirmed only after 3 CONSECUTIVE closes below MA30.
4. Any close >= MA30 resets the counter.
5. Exit executes at the NEXT trading day's open.
6. Account capacity / a fresh high-priority A/B may make a Runner yield first.

The exact historical numeric A15P3 tail-eligibility trigger source was not
preserved, so this hotfix does not invent or auto-activate that trigger.
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

RC4_VERSION = "V18 Adaptive Money-Wave RC4 FINAL"
RC4_RUNNER_FRACTION = 0.15
RC4_MA_WINDOW = 30
RC4_MA_BREAK_DAYS = 3


@dataclass(frozen=True)
class TailExit:
    decision_idx: Optional[int]
    exit_idx: Optional[int]
    decision_date: Optional[pd.Timestamp]
    exit_date: Optional[pd.Timestamp]
    exit_open: Optional[float]
    reason: str


def add_ma30(df: pd.DataFrame) -> pd.DataFrame:
    if "close" not in df.columns:
        raise ValueError("missing close column")
    z = df.copy()
    z["ma30"] = pd.to_numeric(z["close"], errors="coerce").rolling(
        RC4_MA_WINDOW, min_periods=RC4_MA_WINDOW
    ).mean()
    return z


def rc4_a_tail_technical_exit(
    df: pd.DataFrame,
    base_exit_idx: int,
    consecutive_days: int = RC4_MA_BREAK_DAYS,
) -> TailExit:
    if consecutive_days < 1:
        raise ValueError("consecutive_days must be >= 1")
    for col in ("date", "open", "close"):
        if col not in df.columns:
            raise ValueError(f"missing {col} column")

    z = add_ma30(df).reset_index(drop=True)
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
                decision_idx=i,
                exit_idx=None,
                decision_date=pd.Timestamp(z.at[i, "date"]),
                exit_date=None,
                exit_open=None,
                reason="MA30_3close_confirmed_but_no_next_bar",
            )
        return TailExit(
            decision_idx=i,
            exit_idx=j,
            decision_date=pd.Timestamp(z.at[i, "date"]),
            exit_date=pd.Timestamp(z.at[j, "date"]),
            exit_open=float(z.at[j, "open"]),
            reason="MA30_3close_next_open",
        )

    return TailExit(
        decision_idx=None,
        exit_idx=None,
        decision_date=None,
        exit_date=None,
        exit_open=None,
        reason="data_end_no_technical_exit",
    )


def rc4_status() -> dict:
    return {
        "version": RC4_VERSION,
        "runner_fraction": RC4_RUNNER_FRACTION,
        "ma_window": RC4_MA_WINDOW,
        "break_days": RC4_MA_BREAK_DAYS,
        "time_cap": None,
        "technical_exit": "3 consecutive closes below MA30 -> next open",
        "capacity_priority": "fresh A/B may force Runner to yield first",
        "eligibility_activation": False,
        "eligibility_note": (
            "Exact historical A15P3 tail-eligibility trigger source was not preserved; "
            "this build does not invent or auto-activate it."
        ),
    }
