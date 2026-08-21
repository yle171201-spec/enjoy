from __future__ import annotations

from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd
from sqlalchemy import select

from ..models import Signal
from .repository import load_frames_for_codes
from .execution_engine import ExecutionParams, ExecutedTrade, execute_signals
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
        "N": int(len(a)),
        "mean": float(a.mean()),
        "median": float(a.median()),
        "win": float((a > 0).mean()),
        "PF": float(pf),
        "payoff": float(payoff),
        "q10": float(a.quantile(.10)),
        "worst": float(a.min()),
        "p90": float(a.quantile(.90)),
        "best": float(a.max()),
    }


def _signals_query(db, engines=("A", "B", "C"), start: date | None = None, end: date | None = None):
    q = select(Signal).where(Signal.engine.in_(tuple(engines))).order_by(Signal.signal_date, Signal.engine)
    if start:
        q = q.where(Signal.signal_date >= start)
    if end:
        q = q.where(Signal.signal_date <= end)
    return db.execute(q).scalars().all()


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
    db,
    engines=("A", "B", "C"),
    execution="close",
    start: date | None = None,
    end: date | None = None,
    slippage_bps: float = 0,
    commission_bps: float = 0,
    stamp_tax_bps: float = 0,
) -> dict:
    sigs = _signals_query(db, engines, start, end)
    frames = load_frames_for_codes(db, [s.code for s in sigs])
    ep = ExecutionParams(
        mode=execution, slippage_bps=slippage_bps,
        commission_bps=commission_bps, stamp_tax_bps=stamp_tax_bps,
    )
    trades = execute_signals(sigs, frames, ep)
    valid = [t for t in trades if not t.skip_reason]
    skipped = [t for t in trades if t.skip_reason]

    rows = []
    for label, subset in [("ALL", valid)]:
        p = perf([t.net_return for t in subset])
        p["scope"] = label
        rows.append(p)
    for e in engines:
        subset = [t for t in valid if t.engine == e]
        p = perf([t.net_return for t in subset]); p["scope"] = e
        rows.append(p)

    reason = {}
    for t in valid:
        reason.setdefault(t.exit_reason, []).append(t.net_return)
    reason_rows = []
    for k, vals in sorted(reason.items(), key=lambda kv: -len(kv[1])):
        q = perf(vals); q["reason"] = k
        reason_rows.append(q)

    return {
        "summary": rows,
        "trades": [_trade_dict(t) for t in valid],
        "skipped": [_trade_dict(t) for t in skipped],
        "reasons": reason_rows,
        "skip_counts": pd.Series([t.skip_reason for t in skipped]).value_counts().to_dict() if skipped else {},
    }


def close_vs_next_open(
    db,
    engines=("A", "B", "C"),
    start: date | None = None,
    end: date | None = None,
    slippage_bps: float = 0,
    commission_bps: float = 0,
    stamp_tax_bps: float = 0,
) -> dict:
    close = dynamic_backtest(db, engines, "close", start, end, 0, commission_bps, stamp_tax_bps)
    nxt = dynamic_backtest(db, engines, "next_open", start, end, slippage_bps, commission_bps, stamp_tax_bps)

    cdf = pd.DataFrame(close["trades"])
    ndf = pd.DataFrame(nxt["trades"])
    if len(cdf):
        cdf["key"] = list(zip(cdf.code, cdf.signal_date, cdf.engine))
    if len(ndf):
        ndf["key"] = list(zip(ndf.code, ndf.signal_date, ndf.engine))

    paired = []
    if len(cdf) and len(ndf):
        m = cdf[["key", "net_return"]].merge(
            ndf[["key", "net_return", "gap_pct", "entry_price", "risk_pct"]], on="key", suffixes=("_close", "_next")
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
        "close": close,
        "next_open": nxt,
        "paired": paired,
        "paired_perf": perf([x["delta"] for x in paired]) if paired else {"N": 0},
        "gap_buckets": gap_rows,
    }


def portfolio_backtest(
    db,
    engines=("A", "B", "C"),
    execution="next_open",
    start: date | None = None,
    end: date | None = None,
    k: int = 5,
    ab_risk: float = .025,
    c_risk: float = .015,
    max_weight: float = .20,
    slippage_bps: float = 0,
    commission_bps: float = 0,
    stamp_tax_bps: float = 0,
    c_yields_to_ab: bool = True,
    max_c: int = 1,
    monte_carlo_seeds: int = 0,
    seed: int = 20260819,
) -> dict:
    sigs = _signals_query(db, engines, start, end)
    frames = load_frames_for_codes(db, [s.code for s in sigs])
    ep = ExecutionParams(
        mode=execution, slippage_bps=slippage_bps,
        commission_bps=commission_bps, stamp_tax_bps=stamp_tax_bps,
        max_weight=max_weight, ab_risk_budget=ab_risk, c_risk_budget=c_risk,
    )
    trades = execute_signals(sigs, frames, ep)
    pp = PortfolioParams(
        max_positions=k, max_c_positions=max_c, c_yields_to_ab=c_yields_to_ab,
        random_seed=seed, commission_bps=commission_bps, stamp_tax_bps=stamp_tax_bps,
    )
    result = simulate_portfolio(trades, frames, ep, pp)
    if monte_carlo_seeds > 0:
        result["monte_carlo"] = monte_carlo_portfolio(trades, frames, ep, pp, min(int(monte_carlo_seeds), 500))
    else:
        result["monte_carlo"] = None
    return result
