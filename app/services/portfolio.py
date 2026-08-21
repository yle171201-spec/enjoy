from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd

from .execution_engine import ExecutedTrade, ExecutionParams, execute_signals


@dataclass
class PortfolioParams:
    max_positions: int = 5
    max_c_positions: int = 1
    c_yields_to_ab: bool = True
    random_seed: int = 20260819
    commission_bps: float = 0.0
    stamp_tax_bps: float = 0.0


@dataclass
class Position:
    tid: int
    code: str
    engine: str
    shares: float
    entry_date: date
    scheduled_exit_date: date



def _frame_maps(frames: dict[str, pd.DataFrame], codes: set[str]):
    out = {}
    calendars = set()
    for code in codes:
        if code not in frames:
            continue
        z = frames[code].copy()
        z["date"] = pd.to_datetime(z["date"]).dt.date
        z = z.sort_values("date").drop_duplicates("date")
        out[code] = z.set_index("date")[["open", "close"]]
        calendars.update(z["date"].tolist())
    return out, sorted(calendars)



def _asof_close(px: pd.DataFrame, d: date) -> float:
    if d in px.index:
        return float(px.loc[d, "close"])
    # selected stock count is small; index search is fast enough.
    idx = px.index.searchsorted(d, side="right") - 1
    if idx < 0:
        return np.nan
    return float(px.iloc[idx]["close"])



def _mark(pxmap: dict[str, pd.DataFrame], code: str, d: date, when: str) -> float:
    px = pxmap.get(code)
    if px is None or px.empty:
        return np.nan
    if d in px.index:
        if when == "open":
            return float(px.loc[d, "open"])
        return float(px.loc[d, "close"])
    return _asof_close(px, d)



def _sell_value(shares: float, px: float, p: PortfolioParams) -> float:
    fee = (p.commission_bps + p.stamp_tax_bps) / 10000.0
    return shares * px * (1 - fee)



def _buy_shares(cash: float, target_value: float, px: float, p: PortfolioParams) -> tuple[float, float]:
    if px <= 0 or cash <= 0 or target_value <= 0:
        return 0.0, 0.0
    fee = p.commission_bps / 10000.0
    actual_value = min(target_value, cash / (1 + fee))
    shares = actual_value / px
    cash_used = actual_value * (1 + fee)
    return shares, cash_used



def _equity(cash: float, positions: dict[int, Position], pxmap: dict[str, pd.DataFrame], d: date, when: str) -> float:
    v = cash
    for pos in positions.values():
        px = _mark(pxmap, pos.code, d, when)
        if np.isfinite(px):
            v += pos.shares * px
    return float(v)



def _drawdown_metrics(equity: pd.DataFrame) -> dict:
    if equity.empty:
        return {"mdd": np.nan, "peak_date": None, "trough_date": None, "recovery_date": None, "max_underwater_days": 0}
    s = equity.set_index("date")["equity"].astype(float)
    peak = s.cummax()
    dd = s / peak - 1
    trough = dd.idxmin()
    peak_date = s.loc[:trough].idxmax()
    target = float(peak.loc[trough])
    after = s.loc[trough:]
    rec = after[after >= target]
    recovery = rec.index[0] if len(rec) else None
    cur = mx = 0
    for x in dd.to_numpy():
        if x < -1e-12:
            cur += 1
            mx = max(mx, cur)
        else:
            cur = 0
    return {
        "mdd": float(dd.min()),
        "peak_date": peak_date,
        "trough_date": trough,
        "recovery_date": recovery,
        "max_underwater_days": int(mx),
    }



def _yearly(equity: pd.DataFrame) -> list[dict]:
    if equity.empty:
        return []
    x = equity.copy()
    x["year"] = pd.to_datetime(x["date"]).dt.year
    rows = []
    prev = 1.0
    for y, g in x.groupby("year"):
        vals = g["equity"].to_numpy(float)
        if not len(vals):
            continue
        end = float(vals[-1])
        ext = np.r_[prev, vals]
        dd = ext / np.maximum.accumulate(ext) - 1
        rows.append({"year": int(y), "return": end / prev - 1, "mdd": float(dd.min()), "end_equity": end})
        prev = end
    return rows



def simulate_portfolio(
    trades: Iterable[ExecutedTrade],
    frames: dict[str, pd.DataFrame],
    execution: ExecutionParams,
    portfolio: PortfolioParams | None = None,
) -> dict:
    p = portfolio or PortfolioParams(
        commission_bps=execution.commission_bps,
        stamp_tax_bps=execution.stamp_tax_bps,
    )
    all_trades = list(trades)
    valid = [t for t in all_trades if not t.skip_reason]
    skipped = [t for t in all_trades if t.skip_reason]
    if not valid:
        return {
            "metrics": {
                "terminal": 1.0, "cagr": 0.0, "mdd": 0.0, "peak_date": None, "trough_date": None,
                "recovery_date": None, "max_underwater_days": 0, "accepted": 0, "rejected": 0,
                "skipped": len(skipped), "full_rejections": 0, "duplicate_rejections": 0,
                "c_cap_rejections": 0, "c_replacements": 0, "accepted_A": 0, "accepted_B": 0,
                "accepted_C": 0, "calmar_like": 0.0
            },
            "equity": [], "yearly": [], "trades": [], "rejections": [], "skipped_detail": []
        }

    codes = {t.code for t in valid}
    pxmap, full_calendar = _frame_maps(frames, codes)
    start = min(t.entry_date for t in valid)
    end = max(t.exit_date for t in valid)
    calendar = [d for d in full_calendar if start <= d <= end]

    by_entry: dict[date, list[int]] = {}
    by_exit: dict[date, list[int]] = {}
    for tid, t in enumerate(valid):
        by_entry.setdefault(t.entry_date, []).append(tid)
        by_exit.setdefault(t.exit_date, []).append(tid)

    rng = np.random.default_rng(p.random_seed)
    cash = 1.0
    positions: dict[int, Position] = {}
    accepted: list[int] = []
    rejection_rows: list[dict] = []
    replacement_count = 0
    duplicate_count = 0
    full_count = 0
    c_cap_count = 0
    equity_rows = []
    realized_rows = []

    def held_codes():
        return {x.code for x in positions.values()}

    def close_position(tid: int, d: date, when: str, reason_override: str | None = None):
        nonlocal cash
        pos = positions.get(tid)
        if pos is None:
            return
        px = _mark(pxmap, pos.code, d, when)
        if not np.isfinite(px):
            return
        cash += _sell_value(pos.shares, px, p)
        t = valid[tid]
        realized_rows.append({
            "tid": tid, "code": pos.code, "engine": pos.engine,
            "entry_date": pos.entry_date, "exit_date": d, "exit_price": px,
            "reason": reason_override or t.exit_reason,
        })
        del positions[tid]

    for d in calendar:
        if execution.mode == "next_open":
            # Entries happen at the open; positions that exit at today's close still occupy capacity.
            candidates = list(by_entry.get(d, []))
            ab = [tid for tid in candidates if valid[tid].engine in {"A", "B"}]
            cc = [tid for tid in candidates if valid[tid].engine == "C"]
            rng.shuffle(ab); rng.shuffle(cc)

            for tid in ab:
                t = valid[tid]
                if t.code in held_codes():
                    duplicate_count += 1
                    rejection_rows.append({"date": d, "code": t.code, "engine": t.engine, "reason": "duplicate_code"})
                    continue
                if len(positions) >= p.max_positions and p.c_yields_to_ab:
                    cpos = [x for x, pos in positions.items() if pos.engine == "C"]
                    if cpos:
                        # With max C=1 this is deterministic. If >1, yield weakest marked C.
                        old = min(cpos, key=lambda x: _mark(pxmap, positions[x].code, d, "open") / valid[x].entry_price)
                        close_position(old, d, "open", "C让位A/B")
                        replacement_count += 1
                if len(positions) >= p.max_positions:
                    full_count += 1
                    rejection_rows.append({"date": d, "code": t.code, "engine": t.engine, "reason": "full"})
                    continue
                eq = _equity(cash, positions, pxmap, d, "open")
                target = eq * t.target_weight
                sh, used = _buy_shares(cash, target, t.entry_price, p)
                if sh <= 0:
                    continue
                cash -= used
                positions[tid] = Position(tid, t.code, t.engine, sh, d, t.exit_date)
                accepted.append(tid)

            c_now = sum(1 for x in positions.values() if x.engine == "C")
            for tid in cc:
                t = valid[tid]
                if len(positions) >= p.max_positions:
                    full_count += 1
                    rejection_rows.append({"date": d, "code": t.code, "engine": t.engine, "reason": "full"})
                    continue
                if c_now >= p.max_c_positions:
                    c_cap_count += 1
                    rejection_rows.append({"date": d, "code": t.code, "engine": t.engine, "reason": "c_cap"})
                    continue
                if t.code in held_codes():
                    duplicate_count += 1
                    rejection_rows.append({"date": d, "code": t.code, "engine": t.engine, "reason": "duplicate_code"})
                    continue
                eq = _equity(cash, positions, pxmap, d, "open")
                target = eq * t.target_weight
                sh, used = _buy_shares(cash, target, t.entry_price, p)
                if sh <= 0:
                    continue
                cash -= used
                positions[tid] = Position(tid, t.code, t.engine, sh, d, t.exit_date)
                accepted.append(tid); c_now += 1

            # Tested dynamic exits occur at close.
            for tid in list(by_exit.get(d, [])):
                if tid in positions:
                    close_position(tid, d, "close")

        else:
            # close-entry: tested exits first, then new entries at same close.
            for tid in list(by_exit.get(d, [])):
                if tid in positions:
                    close_position(tid, d, "close")

            candidates = list(by_entry.get(d, []))
            ab = [tid for tid in candidates if valid[tid].engine in {"A", "B"}]
            cc = [tid for tid in candidates if valid[tid].engine == "C"]
            rng.shuffle(ab); rng.shuffle(cc)

            for tid in ab:
                t = valid[tid]
                if t.code in held_codes():
                    duplicate_count += 1
                    rejection_rows.append({"date": d, "code": t.code, "engine": t.engine, "reason": "duplicate_code"})
                    continue
                if len(positions) >= p.max_positions and p.c_yields_to_ab:
                    cpos = [x for x, pos in positions.items() if pos.engine == "C"]
                    if cpos:
                        old = min(cpos, key=lambda x: _mark(pxmap, positions[x].code, d, "close") / valid[x].entry_price)
                        close_position(old, d, "close", "C让位A/B")
                        replacement_count += 1
                if len(positions) >= p.max_positions:
                    full_count += 1
                    rejection_rows.append({"date": d, "code": t.code, "engine": t.engine, "reason": "full"})
                    continue
                eq = _equity(cash, positions, pxmap, d, "close")
                target = eq * t.target_weight
                sh, used = _buy_shares(cash, target, t.entry_price, p)
                if sh <= 0:
                    continue
                cash -= used
                positions[tid] = Position(tid, t.code, t.engine, sh, d, t.exit_date)
                accepted.append(tid)

            c_now = sum(1 for x in positions.values() if x.engine == "C")
            for tid in cc:
                t = valid[tid]
                if len(positions) >= p.max_positions:
                    full_count += 1
                    rejection_rows.append({"date": d, "code": t.code, "engine": t.engine, "reason": "full"})
                    continue
                if c_now >= p.max_c_positions:
                    c_cap_count += 1
                    rejection_rows.append({"date": d, "code": t.code, "engine": t.engine, "reason": "c_cap"})
                    continue
                if t.code in held_codes():
                    duplicate_count += 1
                    rejection_rows.append({"date": d, "code": t.code, "engine": t.engine, "reason": "duplicate_code"})
                    continue
                eq = _equity(cash, positions, pxmap, d, "close")
                target = eq * t.target_weight
                sh, used = _buy_shares(cash, target, t.entry_price, p)
                if sh <= 0:
                    continue
                cash -= used
                positions[tid] = Position(tid, t.code, t.engine, sh, d, t.exit_date)
                accepted.append(tid); c_now += 1

        eq_close = _equity(cash, positions, pxmap, d, "close")
        equity_rows.append({
            "date": d, "equity": eq_close, "cash": cash, "positions": len(positions),
            "a": sum(1 for x in positions.values() if x.engine == "A"),
            "b": sum(1 for x in positions.values() if x.engine == "B"),
            "c": sum(1 for x in positions.values() if x.engine == "C"),
        })

    eqdf = pd.DataFrame(equity_rows)
    ddm = _drawdown_metrics(eqdf)
    terminal = float(eqdf["equity"].iloc[-1]) if len(eqdf) else 1.0
    days = (calendar[-1] - calendar[0]).days if len(calendar) > 1 else 0
    years = days / 365.25
    cagr = terminal ** (1 / years) - 1 if years > 0 and terminal > 0 else np.nan

    accepted_engines = pd.Series([valid[x].engine for x in accepted]).value_counts().to_dict() if accepted else {}
    metrics = {
        "terminal": terminal,
        "cagr": float(cagr),
        "mdd": ddm["mdd"],
        "peak_date": ddm["peak_date"],
        "trough_date": ddm["trough_date"],
        "recovery_date": ddm["recovery_date"],
        "max_underwater_days": ddm["max_underwater_days"],
        "accepted": len(accepted),
        "rejected": len(rejection_rows),
        "skipped": len(skipped),
        "full_rejections": full_count,
        "duplicate_rejections": duplicate_count,
        "c_cap_rejections": c_cap_count,
        "c_replacements": replacement_count,
        "accepted_A": int(accepted_engines.get("A", 0)),
        "accepted_B": int(accepted_engines.get("B", 0)),
        "accepted_C": int(accepted_engines.get("C", 0)),
        "calmar_like": float(cagr / abs(ddm["mdd"])) if np.isfinite(cagr) and ddm["mdd"] < 0 else np.nan,
    }

    accepted_rows = []
    for tid in accepted:
        t = valid[tid]
        accepted_rows.append({
            "code": t.code, "engine": t.engine, "signal_date": t.signal_date,
            "entry_date": t.entry_date, "entry_price": t.entry_price,
            "exit_date": t.exit_date, "exit_price": t.exit_price,
            "return": t.net_return, "risk_pct": t.risk_pct,
            "target_weight": t.target_weight, "gap_pct": t.gap_pct,
            "exit_reason": t.exit_reason,
        })

    return {
        "metrics": metrics,
        "equity": eqdf.to_dict("records"),
        "yearly": _yearly(eqdf),
        "trades": accepted_rows,
        "rejections": rejection_rows,
        "skipped_detail": [
            {"code": t.code, "engine": t.engine, "signal_date": t.signal_date, "reason": t.skip_reason}
            for t in skipped
        ],
    }



def monte_carlo_portfolio(
    trades: list[ExecutedTrade],
    frames: dict[str, pd.DataFrame],
    execution: ExecutionParams,
    portfolio: PortfolioParams,
    seeds: int = 200,
) -> dict:
    rows = []
    for i in range(max(1, seeds)):
        pp = PortfolioParams(
            max_positions=portfolio.max_positions,
            max_c_positions=portfolio.max_c_positions,
            c_yields_to_ab=portfolio.c_yields_to_ab,
            random_seed=portfolio.random_seed + i,
            commission_bps=portfolio.commission_bps,
            stamp_tax_bps=portfolio.stamp_tax_bps,
        )
        r = simulate_portfolio(trades, frames, execution, pp)["metrics"]
        rows.append(r)
    x = pd.DataFrame(rows)
    if x.empty:
        return {}
    return {
        "seeds": len(x),
        "terminal_p10": float(x["terminal"].quantile(.10)),
        "terminal_p50": float(x["terminal"].quantile(.50)),
        "terminal_p90": float(x["terminal"].quantile(.90)),
        "cagr_p10": float(x["cagr"].quantile(.10)),
        "cagr_p50": float(x["cagr"].quantile(.50)),
        "cagr_p90": float(x["cagr"].quantile(.90)),
        # MDD is negative: P10 is the worse tail.
        "mdd_p10": float(x["mdd"].quantile(.10)),
        "mdd_p50": float(x["mdd"].quantile(.50)),
        "mdd_p90": float(x["mdd"].quantile(.90)),
        "accepted_p50": float(x["accepted"].quantile(.50)),
    }
