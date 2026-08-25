from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import json
from typing import Any, Iterable

import numpy as np
import pandas as pd

from ..engine.strategy_reference_v18 import ensure_frame, prepare_stock, cost50_proxy


@dataclass
class ExecutedTrade:
    signal_id: int | None
    code: str
    engine: str
    signal_date: date
    signal_close: float
    fail_price: float
    h_daily: float
    p_level: float | None
    entry_date: date
    entry_price: float
    entry_idx: int
    exit_date: date
    exit_price: float
    exit_idx: int
    gross_return: float
    net_return: float
    exit_reason: str
    risk_pct: float
    target_weight: float
    gap_pct: float
    mfe: float
    mae: float
    skip_reason: str | None = None


@dataclass
class ExecutionParams:
    mode: str = "close"  # close | next_open
    slippage_bps: float = 0.0
    commission_bps: float = 0.0  # per side
    stamp_tax_bps: float = 0.0   # sell side only
    max_weight: float = 0.20
    ab_risk_budget: float = 0.025
    c_risk_budget: float = 0.015
    skip_open_limit: bool = True



def _metadata(signal: Any) -> dict[str, Any]:
    raw = getattr(signal, "metadata_json", "{}") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}



def _safe_float(v, default=np.nan) -> float:
    try:
        x = float(v)
        return x if np.isfinite(x) else default
    except Exception:
        return default



def _signal_index(df: pd.DataFrame, signal_date: date) -> int | None:
    ds = pd.to_datetime(df["date"]).dt.date.to_numpy()
    hit = np.flatnonzero(ds == signal_date)
    return int(hit[0]) if len(hit) else None



def _limit_threshold(code: str) -> float:
    c = str(code).zfill(6)
    return 0.19 if c.startswith("3") else 0.095



def _open_is_limit_locked(code: str, open_px: float, prev_close: float) -> bool:
    if prev_close <= 0:
        return False
    return open_px / prev_close - 1 >= _limit_threshold(code)



def _risk_weight(entry: float, fail: float, engine: str, p: ExecutionParams) -> tuple[float, float]:
    if not np.isfinite(entry) or not np.isfinite(fail) or entry <= 0:
        return np.nan, 0.0
    risk = (entry - fail) / entry
    if risk <= 0:
        return risk, 0.0
    budget = p.c_risk_budget if engine == "C" else p.ab_risk_budget
    return float(risk), float(min(p.max_weight, budget / risk))



def _net_return(entry: float, exit_px: float, p: ExecutionParams) -> float:
    buy_cost = p.commission_bps / 10000.0
    sell_cost = (p.commission_bps + p.stamp_tax_bps) / 10000.0
    # Cash invested before fees -> shares purchased after buy commission approximation.
    effective_entry = entry * (1 + buy_cost)
    effective_exit = exit_px * (1 - sell_cost)
    return effective_exit / effective_entry - 1



def _path_stats(df: pd.DataFrame, entry_idx: int, exit_idx: int, entry_price: float) -> tuple[float, float]:
    if entry_price <= 0 or exit_idx < entry_idx:
        return np.nan, np.nan
    hi = float(df["high"].iloc[entry_idx:exit_idx + 1].max())
    lo = float(df["low"].iloc[entry_idx:exit_idx + 1].min())
    return hi / entry_price - 1, lo / entry_price - 1



def _ensure_indicators(raw_df: pd.DataFrame, code: str) -> pd.DataFrame:
    # prepare_stock already creates MA10/MA20/ATR/ER10/R5 and weekly fields.
    df = prepare_stock(raw_df, code)
    if "cost50" not in df.columns:
        df = df.copy()
        df["cost50"] = cost50_proxy(df)
    return df



def _exit_a(signal: Any, df: pd.DataFrame, entry_idx: int, entry_price: float, evaluate_entry_day: bool) -> tuple[int, float, str, float]:
    meta = _metadata(signal)
    H = _safe_float(getattr(signal, "h_daily", None), _safe_float(meta.get("Hdaily")))
    P = _safe_float(getattr(signal, "p_level", None), _safe_float(meta.get("P")))
    if not np.isfinite(H) or not np.isfinite(P):
        raise ValueError("A signal missing Hdaily or P")

    h = df["high"].to_numpy(float)
    c = df["close"].to_numpy(float)
    ma10 = df["ma10"].to_numpy(float)
    ma20 = df["ma20"].to_numpy(float)
    atr = df["atr20"].to_numpy(float)
    r5 = df["r5"].to_numpy(float)
    er10 = df["er10"].to_numpy(float)
    cost = df["cost50"].to_numpy(float)

    maxh = h[entry_idx]
    mature = False
    highm = False
    deep_count = 0
    start = entry_idx if evaluate_entry_day else entry_idx + 1
    last_mfe = maxh / entry_price - 1

    for i in range(start, len(df)):
        maxh = max(maxh, h[i])
        mfe = maxh / entry_price - 1
        last_mfe = mfe
        if mfe >= 1.25 * H:
            mature = True
        if mfe >= 1.50 * H:
            highm = True

        if not mature:
            atrpct = atr[i] / c[i] if c[i] > 0 and np.isfinite(atr[i]) else 0.0
            deepgap = float(np.clip(max(H, 3 * atrpct), 0.06, 0.20))
            deep_count = deep_count + 1 if c[i] < P * (1 - deepgap) else 0
            if deep_count >= 3:
                return i, float(c[i]), "结构失", mfe
            if (
                i - entry_idx >= 20
                and mfe < H
                and c[i] < ma20[i]
                and ma10[i] < ma20[i]
                and er10[i] < 0
            ):
                return i, float(c[i]), "滞败", mfe

        if (
            highm and i >= 5 and np.isfinite(cost[i]) and np.isfinite(cost[i - 5])
            and cost[i - 5] > 0 and cost[i] / cost[i - 5] - 1 <= 0.005
            and r5[i] < 0 and c[i] < ma10[i]
        ):
            return i, float(c[i]), "高成熟成本停滞", mfe

        if mature and c[i] < ma20[i] and ma10[i] < ma20[i] and er10[i] < 0:
            return i, float(c[i]), "趋势破坏", mfe

    return len(df) - 1, float(c[-1]), "数据末端", last_mfe



def _exit_b(signal: Any, df: pd.DataFrame, entry_idx: int, entry_price: float, evaluate_entry_day: bool) -> tuple[int, float, str, float]:
    meta = _metadata(signal)
    H = _safe_float(getattr(signal, "h_daily", None), _safe_float(meta.get("Hdaily")))
    fail = _safe_float(getattr(signal, "fail_price", None))
    if not np.isfinite(H) or not np.isfinite(fail):
        raise ValueError("B signal missing Hdaily or fail price")

    h = df["high"].to_numpy(float)
    c = df["close"].to_numpy(float)
    ma10 = df["ma10"].to_numpy(float)
    ma20 = df["ma20"].to_numpy(float)
    er10 = df["er10"].to_numpy(float)

    maxh = h[entry_idx]
    proven = False
    fail_count = 0
    start = entry_idx if evaluate_entry_day else entry_idx + 1
    last_mfe = maxh / entry_price - 1

    for i in range(start, len(df)):
        maxh = max(maxh, h[i])
        mfe = maxh / entry_price - 1
        last_mfe = mfe
        if mfe >= 1.5 * H:
            proven = True

        if not proven:
            fail_count = fail_count + 1 if c[i] < fail else 0
            if fail_count >= 2:
                return i, float(c[i]), "首回踩失效", mfe
            if (
                i - entry_idx >= 20
                and mfe < H
                and c[i] < ma20[i]
                and ma10[i] < ma20[i]
                and er10[i] < 0
            ):
                return i, float(c[i]), "滞败", mfe

        if proven:
            giveback = (maxh - c[i]) / entry_price
            if giveback >= 1.0 * H:
                return i, float(c[i]), "证明后结构回吐", mfe

    return len(df) - 1, float(c[-1]), "数据末端", last_mfe



def _exit_c(signal: Any, df: pd.DataFrame, entry_idx: int, entry_price: float, evaluate_entry_day: bool) -> tuple[int, float, str, float]:
    meta = _metadata(signal)
    H = _safe_float(getattr(signal, "h_daily", None), _safe_float(meta.get("Hdaily")))
    fail = _safe_float(getattr(signal, "fail_price", None))
    if not np.isfinite(H) or not np.isfinite(fail):
        raise ValueError("C signal missing Hdaily or fail price")

    h = df["high"].to_numpy(float)
    c = df["close"].to_numpy(float)
    ma10 = df["ma10"].to_numpy(float)
    ma20 = df["ma20"].to_numpy(float)
    r5 = df["r5"].to_numpy(float)
    er10 = df["er10"].to_numpy(float)
    cost = df["cost50"].to_numpy(float)

    maxh = h[entry_idx]
    proven = False
    highm = False
    fail_count = 0
    start = entry_idx if evaluate_entry_day else entry_idx + 1
    last_mfe = maxh / entry_price - 1

    for i in range(start, len(df)):
        maxh = max(maxh, h[i])
        mfe = maxh / entry_price - 1
        last_mfe = mfe
        if mfe >= 1.0 * H:
            proven = True
        if mfe >= 1.5 * H:
            highm = True

        if not proven:
            fail_count = fail_count + 1 if c[i] < fail else 0
            if fail_count >= 2:
                return i, float(c[i]), "横盘结构失效", mfe
            if (
                i - entry_idx >= 20
                and mfe < H
                and c[i] < ma20[i]
                and ma10[i] < ma20[i]
                and er10[i] < 0
            ):
                return i, float(c[i]), "滞败", mfe

        if (
            highm and i >= 5 and np.isfinite(cost[i]) and np.isfinite(cost[i - 5])
            and cost[i - 5] > 0 and cost[i] / cost[i - 5] - 1 <= 0.005
            and r5[i] < 0 and c[i] < ma10[i]
        ):
            return i, float(c[i]), "高成熟成本停滞", mfe

        if proven and c[i] < ma20[i]:
            return i, float(c[i]), "MA20破坏", mfe

    return len(df) - 1, float(c[-1]), "数据末端", last_mfe



def execute_signal(signal: Any, raw_df: pd.DataFrame, params: ExecutionParams | None = None, prepared_df: pd.DataFrame | None = None) -> ExecutedTrade:
    p = params or ExecutionParams()
    code = str(getattr(signal, "code")).zfill(6)
    engine = str(getattr(signal, "engine"))
    sdate = getattr(signal, "signal_date")
    signal_close = float(getattr(signal, "signal_close"))
    fail = float(getattr(signal, "fail_price"))
    h_daily = _safe_float(getattr(signal, "h_daily", None), _safe_float(_metadata(signal).get("Hdaily")))
    p_level = _safe_float(getattr(signal, "p_level", None), _safe_float(_metadata(signal).get("P")))
    if not np.isfinite(p_level):
        p_level = None

    df = prepared_df if prepared_df is not None else _ensure_indicators(raw_df, code)
    sig_idx = _signal_index(df, sdate)
    if sig_idx is None:
        raise ValueError(f"signal date {sdate} not found for {code}")

    if p.mode not in {"close", "next_open"}:
        raise ValueError("mode must be close or next_open")

    if p.mode == "close":
        entry_idx = sig_idx
        entry_date = sdate
        entry_price = signal_close * (1 + p.slippage_bps / 10000.0)
        gap_pct = 0.0
        evaluate_entry_day = False
    else:
        if sig_idx + 1 >= len(df):
            risk, weight = _risk_weight(signal_close, fail, engine, p)
            return ExecutedTrade(
                getattr(signal, "id", None), code, engine, sdate, signal_close, fail, h_daily,
                p_level, sdate, signal_close, sig_idx, sdate, signal_close, sig_idx,
                0.0, 0.0, "无次日数据", risk, weight, np.nan, np.nan, np.nan, "NO_NEXT_BAR"
            )
        entry_idx = sig_idx + 1
        entry_date = df["date"].iloc[entry_idx].date()
        raw_open = float(df["open"].iloc[entry_idx])
        prev_close = float(df["close"].iloc[entry_idx - 1])
        gap_pct = raw_open / prev_close - 1 if prev_close > 0 else np.nan
        if p.skip_open_limit and _open_is_limit_locked(code, raw_open, prev_close):
            risk, weight = _risk_weight(raw_open, fail, engine, p)
            return ExecutedTrade(
                getattr(signal, "id", None), code, engine, sdate, signal_close, fail, h_daily,
                p_level, entry_date, raw_open, entry_idx, entry_date, raw_open, entry_idx,
                0.0, 0.0, "开盘涨停跳过", risk, weight, gap_pct, np.nan, np.nan, "SKIP_LIMIT_LOCK"
            )
        entry_price = raw_open * (1 + p.slippage_bps / 10000.0)
        evaluate_entry_day = True

    risk, weight = _risk_weight(entry_price, fail, engine, p)
    if not np.isfinite(risk) or risk <= 0:
        return ExecutedTrade(
            getattr(signal, "id", None), code, engine, sdate, signal_close, fail, h_daily,
            p_level, entry_date, entry_price, entry_idx, entry_date, entry_price, entry_idx,
            0.0, 0.0, "结构在入场时已失效", risk, 0.0, gap_pct, np.nan, np.nan, "SKIP_INVALID_STRUCTURE"
        )

    if engine == "A":
        exit_idx, exit_px, reason, mfe = _exit_a(signal, df, entry_idx, entry_price, evaluate_entry_day)
    elif engine == "B":
        exit_idx, exit_px, reason, mfe = _exit_b(signal, df, entry_idx, entry_price, evaluate_entry_day)
    elif engine == "C":
        exit_idx, exit_px, reason, mfe = _exit_c(signal, df, entry_idx, entry_price, evaluate_entry_day)
    else:
        raise ValueError(f"unknown engine {engine}")

    gross = exit_px / entry_price - 1
    net = _net_return(entry_price, exit_px, p)
    mfe_path, mae = _path_stats(df, entry_idx, exit_idx, entry_price)
    # Use path-computed MFE for consistency if available.
    mfe = mfe_path if np.isfinite(mfe_path) else mfe

    return ExecutedTrade(
        signal_id=getattr(signal, "id", None), code=code, engine=engine,
        signal_date=sdate, signal_close=signal_close, fail_price=fail,
        h_daily=h_daily, p_level=p_level,
        entry_date=entry_date, entry_price=float(entry_price), entry_idx=entry_idx,
        exit_date=df["date"].iloc[exit_idx].date(), exit_price=float(exit_px), exit_idx=exit_idx,
        gross_return=float(gross), net_return=float(net), exit_reason=reason,
        risk_pct=float(risk), target_weight=float(weight), gap_pct=float(gap_pct),
        mfe=float(mfe), mae=float(mae), skip_reason=None
    )



def materialize_next_open_exit(
    trade: ExecutedTrade,
    raw_df: pd.DataFrame,
    params: ExecutionParams | None = None,
) -> ExecutedTrade:
    """Convert a close-confirmed exit decision to the following trading-day open.

    The core engine keeps the decision date because RC4/account logic needs that
    point-in-time state. Execution research calls this helper for actual NEXT_OPEN
    sale prices without double-shifting the portfolio layer.
    """
    p = params or ExecutionParams(mode="next_open")
    if trade.skip_reason or p.mode != "next_open":
        return trade
    if trade.exit_reason in {"数据末端", "无次日数据"}:
        return trade
    if raw_df is None or raw_df.empty:
        raise ValueError(f"missing frame for next-open exit {trade.code}")

    df = ensure_frame(raw_df)
    decision_idx = _signal_index(df, trade.exit_date)
    if decision_idx is None:
        raise ValueError(f"exit decision date {trade.exit_date} not found for {trade.code}")
    next_idx = decision_idx + 1
    if next_idx >= len(df):
        px = float(df["close"].iloc[decision_idx])
        gross = px / trade.entry_price - 1
        net = _net_return(trade.entry_price, px, p)
        return replace(
            trade, exit_price=px, exit_idx=decision_idx,
            gross_return=float(gross), net_return=float(net), exit_reason="数据末端",
        )

    px = float(df["open"].iloc[next_idx])
    gross = px / trade.entry_price - 1
    net = _net_return(trade.entry_price, px, p)
    return replace(
        trade,
        exit_date=df["date"].iloc[next_idx].date(),
        exit_price=px,
        exit_idx=next_idx,
        gross_return=float(gross),
        net_return=float(net),
    )


def execute_signals(
    signals: Iterable[Any],
    frames: dict[str, pd.DataFrame],
    params: ExecutionParams | None = None,
    errors: list[dict] | None = None,
) -> list[ExecutedTrade]:
    p = params or ExecutionParams()
    out: list[ExecutedTrade] = []
    prepared: dict[str, pd.DataFrame] = {}
    for s in signals:
        code = str(getattr(s, "code")).zfill(6)
        engine = str(getattr(s, "engine", ""))
        sdate = getattr(s, "signal_date", None)
        if code not in frames:
            if errors is not None:
                errors.append({"stage":"missing_frame","code":code,"engine":engine,"signal_date":str(sdate),"error":"no daily frame"})
            continue
        try:
            if code not in prepared:
                prepared[code] = _ensure_indicators(frames[code], code)
            out.append(execute_signal(s, frames[code], p, prepared[code]))
        except Exception as e:
            if errors is not None:
                errors.append({"stage":"execute_signal","code":code,"engine":engine,"signal_date":str(sdate),"error":f"{type(e).__name__}: {e}"})
            continue
    return out
