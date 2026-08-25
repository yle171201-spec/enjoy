from __future__ import annotations

from datetime import date
import numpy as np
import pandas as pd
from sqlalchemy import select, func

from ..config import settings
from ..models import Signal, ScanRun, LiveScanRun, LiveMarketState
from .repository import load_frames_for_codes, latest_trade_date
from .execution_engine import (
    ExecutionParams, ExecutedTrade, execute_signals, materialize_next_open_exit,
)
from .portfolio import PortfolioParams, simulate_portfolio, monte_carlo_portfolio


def perf(values) -> dict:
    a = pd.Series(values, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
    if len(a) == 0:
        return {"N": 0, "mean": np.nan, "median": np.nan, "win": np.nan, "PF": np.nan,
                "payoff": np.nan, "q10": np.nan, "worst": np.nan, "p90": np.nan, "best": np.nan}
    pos = a[a > 0]
    neg = a[a < 0]
    pf = pos.sum() / abs(neg.sum()) if len(neg) and neg.sum() != 0 else np.inf
    payoff = pos.mean() / abs(neg.mean()) if len(pos) and len(neg) and neg.mean() != 0 else np.nan
    return {
        "N": int(len(a)), "mean": float(a.mean()), "median": float(a.median()),
        "win": float((a > 0).mean()), "PF": float(pf), "payoff": float(payoff),
        "q10": float(a.quantile(.10)), "worst": float(a.min()),
        "p90": float(a.quantile(.90)), "best": float(a.max()),
    }


def _signals_query(db, engines=("A", "B", "C"), start: date | None = None, end: date | None = None):
    versions = (settings.strategy_version, settings.live_strategy_version)
    q = select(Signal).where(
        Signal.strategy_version.in_(versions),
        Signal.engine.in_(tuple(engines)),
    )
    if start:
        q = q.where(Signal.signal_date >= start)
    if end:
        q = q.where(Signal.signal_date <= end)
    rows = db.execute(q).scalars().all()

    best = {}
    for s in rows:
        key = (str(s.code).zfill(6), s.signal_date, s.engine)
        cur = best.get(key)
        if cur is None or (
            s.strategy_version == settings.strategy_version
            and cur.strategy_version != settings.strategy_version
        ):
            best[key] = s
    return sorted(best.values(), key=lambda s: (s.signal_date, s.engine, str(s.code).zfill(6)))


def _source_counts(sigs) -> dict:
    out = {}
    for s in sigs:
        v = str(getattr(s, "strategy_version", "") or "UNKNOWN")
        out[v] = out.get(v, 0) + 1
    return out


def _freshness_diag(db, signal_end):
    data_end = latest_trade_date(db)
    full_scan_end = db.execute(
        select(func.max(ScanRun.data_date)).where(ScanRun.status == "ok")
    ).scalar_one_or_none()
    live_scan_end = db.execute(
        select(func.max(LiveScanRun.data_date)).where(LiveScanRun.status == "ok")
    ).scalar_one_or_none()
    live_state_end = db.execute(
        select(func.max(LiveMarketState.trade_date)).where(
            LiveMarketState.strategy_version == settings.strategy_version
        )
    ).scalar_one_or_none()

    coverage_candidates = [
        x for x in (full_scan_end, live_scan_end, live_state_end)
        if x is not None
    ]
    covered_through = max(coverage_candidates) if coverage_candidates else None

    stale = bool(
        data_end is not None
        and (covered_through is None or covered_through < data_end)
    )
    gap_days = (
        (data_end - signal_end).days
        if data_end is not None and signal_end is not None
        else None
    )
    return {
        "data_end": data_end,
        "full_scan_end": full_scan_end,
        "live_scan_end": live_scan_end,
        "live_state_end": live_state_end,
        "covered_through": covered_through,
        "signal_gap_days": gap_days,
        "signal_history_stale": stale,
    }


def _trade_dict(t: ExecutedTrade) -> dict:
    return {
        "code": t.code, "engine": t.engine, "signal_date": t.signal_date,
        "signal_close": t.signal_close, "entry_date": t.entry_date, "entry_price": t.entry_price,
        "gap_pct": t.gap_pct, "fail_price": t.fail_price, "risk_pct": t.risk_pct,
        "target_weight": t.target_weight, "exit_date": t.exit_date, "exit_price": t.exit_price,
        "gross_return": t.gross_return, "net_return": t.net_return, "exit_reason": t.exit_reason,
        "mfe": t.mfe, "mae": t.mae, "skip_reason": t.skip_reason,
    }


def dynamic_backtest(
    db, engines=("A", "B", "C"), execution="close",
    start: date | None = None, end: date | None = None,
    slippage_bps: float = 0, commission_bps: float = 0, stamp_tax_bps: float = 0,
) -> dict:
    sigs = _signals_query(db, engines, start, end)
    frames = load_frames_for_codes(db, [s.code for s in sigs])
    ep = ExecutionParams(
        mode=execution, slippage_bps=slippage_bps,
        commission_bps=commission_bps, stamp_tax_bps=stamp_tax_bps,
    )
    execution_errors = []
    trades = execute_signals(sigs, frames, ep, errors=execution_errors)

    if execution == "next_open":
        actual = []
        for t in trades:
            if t.skip_reason:
                actual.append(t)
                continue
            try:
                actual.append(materialize_next_open_exit(t, frames.get(t.code), ep))
            except Exception as e:
                t.skip_reason = "EXIT_EXECUTION_ERROR"
                execution_errors.append({
                    "stage": "next_open_exit", "code": t.code, "engine": t.engine,
                    "signal_date": str(t.signal_date), "error": f"{type(e).__name__}: {e}",
                })
                actual.append(t)
        trades = actual

    valid = [t for t in trades if not t.skip_reason]
    skipped = [t for t in trades if t.skip_reason]

    rows = []
    p = perf([t.net_return for t in valid]); p["scope"] = "ALL"; rows.append(p)
    for e in engines:
        p = perf([t.net_return for t in valid if t.engine == e]); p["scope"] = e; rows.append(p)

    reason = {}
    for t in valid:
        reason.setdefault(t.exit_reason, []).append(t.net_return)
    reason_rows = []
    for k, vals in sorted(reason.items(), key=lambda kv: -len(kv[1])):
        q = perf(vals); q["reason"] = k; reason_rows.append(q)

    signal_end = max((s.signal_date for s in sigs), default=None)
    diagnostics = {
        "input_signals": len(sigs), "executed_objects": len(trades),
        "valid_trades": len(valid), "skipped": len(skipped),
        "execution_error_count": len(execution_errors),
        "execution_errors": execution_errors[:50],
        "source_counts": _source_counts(sigs),
        "signal_start": min((s.signal_date for s in sigs), default=None),
        "signal_end": signal_end,
        **_freshness_diag(db, signal_end),
    }
    return {
        "summary": rows, "trades": [_trade_dict(t) for t in valid],
        "skipped": [_trade_dict(t) for t in skipped], "reasons": reason_rows,
        "skip_counts": pd.Series([t.skip_reason for t in skipped]).value_counts().to_dict() if skipped else {},
        "diagnostics": diagnostics,
    }


def close_vs_next_open(
    db, engines=("A", "B", "C"), start: date | None = None, end: date | None = None,
    slippage_bps: float = 0, commission_bps: float = 0, stamp_tax_bps: float = 0,
) -> dict:
    close = dynamic_backtest(db, engines, "close", start, end, 0, commission_bps, stamp_tax_bps)
    nxt = dynamic_backtest(db, engines, "next_open", start, end, slippage_bps, commission_bps, stamp_tax_bps)

    cdf = pd.DataFrame(close["trades"])
    ndf = pd.DataFrame(nxt["trades"])
    if len(cdf): cdf["key"] = list(zip(cdf.code, cdf.signal_date, cdf.engine))
    if len(ndf): ndf["key"] = list(zip(ndf.code, ndf.signal_date, ndf.engine))

    paired = []
    if len(cdf) and len(ndf):
        m = cdf[["key", "net_return"]].merge(
            ndf[["key", "net_return", "gap_pct", "entry_price", "risk_pct"]],
            on="key", suffixes=("_close", "_next")
        )
        m["delta"] = m["net_return_next"] - m["net_return_close"]
        paired = m.to_dict("records")

    gap_rows = []
    if len(ndf):
        bins = [-np.inf, 0, .02, .05, .08, np.inf]
        labels = ["<=0%", "0~2%", "2~5%", "5~8%", ">8%"]
        ndf["gap_bucket"] = pd.cut(ndf["gap_pct"], bins=bins, labels=labels, right=True)
        for b, g in ndf.groupby("gap_bucket", observed=True):
            q = perf(g["net_return"]); q["bucket"] = str(b); q["gap_mean"] = float(g.gap_pct.mean())
            q["mfe_mean"] = float(g.mfe.mean()); q["mae_mean"] = float(g.mae.mean())
            gap_rows.append(q)

    return {
        "close": close, "next_open": nxt, "paired": paired,
        "paired_perf": perf([x["delta"] for x in paired]) if paired else {"N": 0},
        "gap_buckets": gap_rows,
    }


def portfolio_backtest(
    db, engines=("A", "B", "C"), execution="next_open",
    start: date | None = None, end: date | None = None,
    k: int = 5, ab_risk: float = .025, c_risk: float = .015, max_weight: float = .20,
    slippage_bps: float = 0, commission_bps: float = 0, stamp_tax_bps: float = 0,
    c_yields_to_ab: bool = True, max_c: int = 1, monte_carlo_seeds: int = 0,
    seed: int = 20260819, rc4_enabled: bool = True,
) -> dict:
    sigs = _signals_query(db, engines, start, end)
    frames = load_frames_for_codes(db, [s.code for s in sigs])
    ep = ExecutionParams(
        mode=execution, slippage_bps=slippage_bps,
        commission_bps=commission_bps, stamp_tax_bps=stamp_tax_bps,
        max_weight=max_weight, ab_risk_budget=ab_risk, c_risk_budget=c_risk,
    )
    execution_errors = []
    trades = execute_signals(sigs, frames, ep, errors=execution_errors)
    pp = PortfolioParams(
        max_positions=k, max_c_positions=max_c, c_yields_to_ab=c_yields_to_ab,
        random_seed=seed, commission_bps=commission_bps, stamp_tax_bps=stamp_tax_bps,
        rc4_enabled=rc4_enabled,
    )
    result = simulate_portfolio(trades, frames, ep, pp)
    signal_end = max((s.signal_date for s in sigs), default=None)
    result["diagnostics"] = {
        "input_signals": len(sigs), "execution_objects": len(trades),
        "execution_error_count": len(execution_errors),
        "execution_errors": execution_errors[:50],
        "source_counts": _source_counts(sigs),
        "signal_start": min((s.signal_date for s in sigs), default=None),
        "signal_end": signal_end,
        "production_start": settings.bootstrap_start_date,
        **_freshness_diag(db, signal_end),
    }
    result["monte_carlo"] = (
        monte_carlo_portfolio(trades, frames, ep, pp, min(int(monte_carlo_seeds), 500))
        if monte_carlo_seeds > 0 else None
    )
    return result


def compare_portfolio_results(rc4: dict, baseline: dict) -> dict:
    rm = rc4.get("metrics", {}); bm = baseline.get("metrics", {})
    req = rc4.get("equity", []) or []; beq = baseline.get("equity", []) or []
    r_start = req[0]["date"] if req else None; b_start = beq[0]["date"] if beq else None
    r_end = req[-1]["date"] if req else None; b_end = beq[-1]["date"] if beq else None
    common_end = max([x for x in (r_end, b_end) if x is not None], default=None)
    same_start = bool(r_start is not None and b_start is not None and r_start == b_start)
    common_start = r_start if same_start else None

    rc4_cagr_aligned = rm.get("cagr"); baseline_cagr_aligned = bm.get("cagr")
    if same_start and common_end is not None:
        years = (common_end - common_start).days / 365.25
        if years > 0:
            rt = float(rm.get("terminal", 1.0)); bt = float(bm.get("terminal", 1.0))
            rc4_cagr_aligned = rt ** (1 / years) - 1 if rt > 0 else np.nan
            baseline_cagr_aligned = bt ** (1 / years) - 1 if bt > 0 else np.nan

    def f(x, default=np.nan):
        try: return float(x)
        except Exception: return default

    rc4_cagr_aligned=f(rc4_cagr_aligned); baseline_cagr_aligned=f(baseline_cagr_aligned)
    r_mdd=f(rm.get("mdd")); b_mdd=f(bm.get("mdd"))
    r_terminal=f(rm.get("terminal")); b_terminal=f(bm.get("terminal"))
    return {
        "same_start": same_start, "start": common_start, "common_end": common_end,
        "baseline_end": b_end, "rc4_end": r_end,
        "baseline_cagr": baseline_cagr_aligned, "rc4_cagr": rc4_cagr_aligned,
        "cagr_delta": rc4_cagr_aligned - baseline_cagr_aligned,
        "baseline_mdd": b_mdd, "rc4_mdd": r_mdd, "mdd_delta": r_mdd - b_mdd,
        "baseline_terminal": b_terminal, "rc4_terminal": r_terminal,
        "terminal_delta": r_terminal - b_terminal,
        "baseline_accepted": int(bm.get("accepted",0) or 0),
        "rc4_accepted": int(rm.get("accepted",0) or 0),
        "accepted_delta": int(rm.get("accepted",0) or 0)-int(bm.get("accepted",0) or 0),
        "tail_created": int(rm.get("rc4_tail_created",0) or 0),
    }
