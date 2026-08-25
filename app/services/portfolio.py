from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd

from .execution_engine import ExecutedTrade, ExecutionParams, execute_signals
from .rc4_final import (
    RC4_RUNNER_FRACTION, RC4_PROOF_H, RC4_WASH5,
    decision_index_for_date, rc4_a_runner_eligibility, rc4_a_tail_technical_exit,
)


@dataclass
class PortfolioParams:
    max_positions: int = 5
    max_c_positions: int = 1
    c_yields_to_ab: bool = True
    random_seed: int = 20260819
    commission_bps: float = 0.0
    stamp_tax_bps: float = 0.0
    rc4_enabled: bool = True
    runner_fraction: float = RC4_RUNNER_FRACTION
    runner_proof_h: float = RC4_PROOF_H
    runner_wash5: float = RC4_WASH5


@dataclass
class Position:
    tid: int
    code: str
    engine: str
    shares: float
    entry_date: date
    scheduled_exit_date: date
    is_tail: bool = False


def _next_trading_date(px: pd.DataFrame, d: date) -> date | None:
    if px is None or px.empty:
        return None
    pos = int(px.index.searchsorted(d, side="right"))
    if pos >= len(px.index):
        return None
    return px.index[pos]


def _runner_info(t: ExecutedTrade, raw_df: pd.DataFrame | None, p: PortfolioParams) -> dict:
    base = {
        "eligible": False, "proof_h": np.nan, "ret5": np.nan,
        "tail_exit_date": None, "tail_reason": None,
    }
    if raw_df is None or t.engine != "A" or t.exit_reason == "数据末端":
        return base
    idx = decision_index_for_date(raw_df, t.exit_date)
    if idx is None:
        return base
    e = rc4_a_runner_eligibility(
        t.h_daily, t.mfe, raw_df, idx, p.runner_proof_h, p.runner_wash5
    )
    base.update({
        "eligible": bool(e.eligible),
        "proof_h": float(e.proof_h),
        "ret5": float(e.ret5),
    })
    if not e.eligible:
        return base
    tx = rc4_a_tail_technical_exit(raw_df, idx)
    base["tail_exit_date"] = tx.exit_date.date() if tx.exit_date is not None else None
    base["tail_reason"] = tx.reason
    return base


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
                "accepted_C": 0, "rc4_enabled": bool(p.rc4_enabled and execution.mode == "next_open"),
                "rc4_tail_created": 0, "rc4_tail_technical_exits": 0,
                "rc4_tail_capacity_yields": 0, "rc4_tail_open_at_end": 0,
                "calmar_like": 0.0
            },
            "equity": [], "yearly": [], "trades": [], "runner_audit": [],
            "realized": [], "rejections": [], "skipped_detail": []
        }

    codes = {t.code for t in valid}
    pxmap, full_calendar = _frame_maps(frames, codes)
    rc4_active = bool(p.rc4_enabled and execution.mode == "next_open")

    runner_info: dict[int, dict] = {}
    planned_exit: dict[int, date] = {}
    for tid, t in enumerate(valid):
        info = _runner_info(t, frames.get(t.code), p) if rc4_active else {
            "eligible": False, "proof_h": np.nan, "ret5": np.nan,
            "tail_exit_date": None, "tail_reason": None,
        }
        runner_info[tid] = info
        if execution.mode == "next_open" and t.exit_reason != "数据末端":
            nd = _next_trading_date(pxmap.get(t.code), t.exit_date)
            planned_exit[tid] = nd or t.exit_date
        else:
            planned_exit[tid] = t.exit_date

    start = min(t.entry_date for t in valid)
    end_candidates = list(planned_exit.values())
    end_candidates.extend([
        x["tail_exit_date"] for x in runner_info.values()
        if x.get("tail_exit_date") is not None
    ])
    if rc4_active and any(
        x.get("eligible") and x.get("tail_exit_date") is None
        for x in runner_info.values()
    ) and full_calendar:
        end_candidates.append(full_calendar[-1])
    end = max(end_candidates) if end_candidates else max(t.exit_date for t in valid)
    calendar = [d for d in full_calendar if start <= d <= end]

    by_entry: dict[date, list[int]] = {}
    by_exit: dict[date, list[int]] = {}
    by_tail_exit: dict[date, list[int]] = {}
    for tid, t in enumerate(valid):
        by_entry.setdefault(t.entry_date, []).append(tid)
        by_exit.setdefault(planned_exit[tid], []).append(tid)
        td = runner_info[tid].get("tail_exit_date")
        if td is not None:
            by_tail_exit.setdefault(td, []).append(tid)

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
    tail_created = 0
    tail_technical_exits = 0
    tail_capacity_yields = 0

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
            "shares": pos.shares, "kind": "TAIL" if pos.is_tail else "CORE",
            "reason": reason_override or t.exit_reason,
        })
        del positions[tid]

    def core_exit_position(tid: int, d: date, when: str):
        nonlocal cash, tail_created
        pos = positions.get(tid)
        if pos is None or pos.is_tail:
            return
        t = valid[tid]
        info = runner_info.get(tid, {})
        if not (rc4_active and info.get("eligible")):
            close_position(tid, d, when)
            return

        px = _mark(pxmap, pos.code, d, when)
        if not np.isfinite(px):
            return
        frac = float(np.clip(p.runner_fraction, 0.0, 1.0))
        core_shares = pos.shares * (1.0 - frac)
        tail_shares = pos.shares - core_shares
        if core_shares > 0:
            cash += _sell_value(core_shares, px, p)
            realized_rows.append({
                "tid": tid, "code": pos.code, "engine": pos.engine,
                "entry_date": pos.entry_date, "exit_date": d, "exit_price": px,
                "shares": core_shares, "kind": "CORE85",
                "reason": t.exit_reason + " -> RC4留15%",
            })
        pos.shares = tail_shares
        pos.is_tail = True
        pos.scheduled_exit_date = info.get("tail_exit_date") or (calendar[-1] if calendar else d)
        tail_created += 1

    for d in calendar:
        if execution.mode == "next_open":
            # Decisions at close execute next open; exits free cash/capacity first.
            for tid in list(by_exit.get(d, [])):
                if (
                    tid in positions
                    and not positions[tid].is_tail
                    and valid[tid].exit_reason != "数据末端"
                ):
                    core_exit_position(tid, d, "open")

            for tid in list(by_tail_exit.get(d, [])):
                if tid in positions and positions[tid].is_tail:
                    close_position(tid, d, "open", "RC4_MA30_3close_next_open")
                    tail_technical_exits += 1

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
                while len(positions) >= p.max_positions:
                    tails = [x for x, pos in positions.items() if pos.is_tail]
                    if not tails:
                        break
                    old = min(
                        tails,
                        key=lambda x: _mark(pxmap, positions[x].code, d, "open")
                        / max(valid[x].entry_price, 1e-12),
                    )
                    close_position(old, d, "open", "RC4_tail_yield_fresh_AB")
                    tail_capacity_yields += 1

                if len(positions) >= p.max_positions and p.c_yields_to_ab:
                    cpos = [
                        x for x, pos in positions.items()
                        if pos.engine == "C" and not pos.is_tail
                    ]
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

        if execution.mode == "next_open":
            for tid in list(by_exit.get(d, [])):
                if (
                    tid in positions
                    and not positions[tid].is_tail
                    and valid[tid].exit_reason == "数据末端"
                ):
                    close_position(tid, d, "close", "数据末端")

        eq_close = _equity(cash, positions, pxmap, d, "close")
        equity_rows.append({
            "date": d, "equity": eq_close, "cash": cash, "positions": len(positions),
            "a": sum(1 for x in positions.values() if x.engine == "A"),
            "b": sum(1 for x in positions.values() if x.engine == "B"),
            "c": sum(1 for x in positions.values() if x.engine == "C" and not x.is_tail),
            "tails": sum(1 for x in positions.values() if x.is_tail),
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
        "rc4_enabled": rc4_active,
        "rc4_tail_created": int(tail_created),
        "rc4_tail_technical_exits": int(tail_technical_exits),
        "rc4_tail_capacity_yields": int(tail_capacity_yields),
        "rc4_tail_open_at_end": int(sum(1 for x in positions.values() if x.is_tail)),
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
            "rc4_runner_eligible": bool(runner_info[tid].get("eligible")),
            "rc4_proof_h": runner_info[tid].get("proof_h"),
            "rc4_ret5": runner_info[tid].get("ret5"),
            "rc4_tail_exit_date": runner_info[tid].get("tail_exit_date"),
        })

    runner_audit = []
    for tid in accepted:
        info = runner_info.get(tid, {})
        if not (rc4_active and info.get("eligible")):
            continue
        t = valid[tid]
        rr = [x for x in realized_rows if x.get("tid") == tid]
        core = next((x for x in rr if x.get("kind") == "CORE85"), None)
        tail = next((x for x in rr if x.get("kind") == "TAIL"), None)

        base_px = float(core["exit_price"]) if core else np.nan
        tail_px = float(tail["exit_price"]) if tail else np.nan
        tail_leg_return = (
            tail_px / base_px - 1
            if np.isfinite(base_px) and base_px > 0 and np.isfinite(tail_px)
            else np.nan
        )
        runner_audit.append({
            "code": t.code,
            "signal_date": t.signal_date,
            "decision_date": t.exit_date,
            "base_exit_date": planned_exit.get(tid),
            "base_exit_price": base_px,
            "original_exit_reason": t.exit_reason,
            "proof_h": info.get("proof_h"),
            "ret5": info.get("ret5"),
            "tail_exit_date": tail.get("exit_date") if tail else None,
            "tail_exit_price": tail_px,
            "tail_exit_reason": tail.get("reason") if tail else "OPEN_AT_SAMPLE_END",
            "planned_technical_exit": info.get("tail_exit_date"),
            "tail_leg_return": tail_leg_return,
        })

    return {
        "metrics": metrics,
        "equity": eqdf.to_dict("records"),
        "yearly": _yearly(eqdf),
        "trades": accepted_rows,
        "runner_audit": runner_audit,
        "realized": realized_rows,
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
            rc4_enabled=portfolio.rc4_enabled,
            runner_fraction=portfolio.runner_fraction,
            runner_proof_h=portfolio.runner_proof_h,
            runner_wash5=portfolio.runner_wash5,
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
