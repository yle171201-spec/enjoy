from __future__ import annotations

import json
from datetime import date
import numpy as np
import pandas as pd
from sqlalchemy import select, func, delete

from ..models import Stock, DailyBar, Signal


def upsert_stocks(db, frame):
    for r in frame.itertuples(index=False):
        code = str(r.code).zfill(6)
        obj = db.get(Stock, code)
        if obj is None:
            obj = Stock(code=code)
            db.add(obj)
        obj.name = str(getattr(r, "name", "") or "")
        obj.market = str(getattr(r, "market", "") or "")
        obj.board = str(getattr(r, "board", "") or "")
        obj.is_st = bool(getattr(r, "is_st", False))
    db.commit()


def upsert_bars(db, code, frame):
    code = str(code).zfill(6)
    n = 0
    for r in frame.itertuples(index=False):
        td = pd.Timestamp(r.date).date()
        obj = db.execute(
            select(DailyBar).where(DailyBar.code == code, DailyBar.trade_date == td)
        ).scalar_one_or_none()
        if obj is None:
            obj = DailyBar(
                code=code, trade_date=td, open=float(r.open), high=float(r.high),
                low=float(r.low), close=float(r.close), volume=float(r.volume),
                amount=float(r.amount), turnover=float(r.turnover)
            )
            db.add(obj)
        else:
            obj.open = float(r.open); obj.high = float(r.high); obj.low = float(r.low); obj.close = float(r.close)
            obj.volume = float(r.volume); obj.amount = float(r.amount); obj.turnover = float(r.turnover)
        n += 1
    db.commit()
    return n


def latest_trade_date(db):
    return db.execute(select(func.max(DailyBar.trade_date))).scalar_one_or_none()


def _rows_to_frames(rows):
    if not rows:
        return {}
    x = pd.DataFrame([
        {
            "code": r.code, "date": r.trade_date, "open": r.open, "high": r.high,
            "low": r.low, "close": r.close, "volume": r.volume,
            "amount": r.amount, "turnover": r.turnover
        } for r in rows
    ])
    out = {}
    for code, g in x.groupby("code", sort=False):
        z = g[["date", "open", "high", "low", "close", "volume", "amount", "turnover"]].copy()
        z["date"] = pd.to_datetime(z.date)
        out[str(code).zfill(6)] = z.sort_values("date").reset_index(drop=True)
    return out


def load_all_frames(db):
    rows = db.execute(select(DailyBar).order_by(DailyBar.code, DailyBar.trade_date)).scalars().all()
    return _rows_to_frames(rows)


def load_frames_for_codes(db, codes):
    codes = sorted({str(c).zfill(6) for c in codes})
    if not codes:
        return {}
    rows = db.execute(
        select(DailyBar)
        .where(DailyBar.code.in_(codes))
        .order_by(DailyBar.code, DailyBar.trade_date)
    ).scalars().all()
    return _rows_to_frames(rows)


def latest_prices(db, codes):
    result = {}
    for code in sorted({str(c).zfill(6) for c in codes}):
        r = db.execute(
            select(DailyBar)
            .where(DailyBar.code == code)
            .order_by(DailyBar.trade_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if r:
            result[code] = (r.trade_date, float(r.close))
    return result


def replace_signals(db, frame, version="V18"):
    db.execute(delete(Signal).where(Signal.strategy_version == version))
    db.commit()

    for r in frame.itertuples(index=False):
        raw = r._asdict()
        core = {
            "code", "date", "engine", "buy", "fail_price", "risk_pct", "target_weight",
            "exit_ret", "exit_reason", "exit_idx", "exit_date"
        }
        meta = {k: v for k, v in raw.items() if k not in core}

        def clean(v):
            if isinstance(v, pd.Timestamp):
                return v.strftime("%Y-%m-%d")
            if isinstance(v, (date,)):
                return v.isoformat()
            if isinstance(v, np.integer):
                return int(v)
            if isinstance(v, np.floating):
                return None if not np.isfinite(v) else float(v)
            if isinstance(v, np.ndarray):
                return v.tolist()
            return v

        meta = {k: clean(v) for k, v in meta.items()}
        h = getattr(r, "Hdaily", None)
        p_level = getattr(r, "P", None)
        exit_date = getattr(r, "exit_date", None)
        if pd.notna(exit_date) if exit_date is not None else False:
            exit_date = pd.Timestamp(exit_date).date()
        else:
            exit_date = None

        db.add(Signal(
            strategy_version=version,
            code=str(r.code).zfill(6),
            signal_date=pd.Timestamp(r.date).date(),
            engine=str(r.engine),
            signal_close=float(r.buy),
            fail_price=float(r.fail_price),
            risk_pct=float(r.risk_pct),
            target_weight=float(r.target_weight),
            h_daily=float(h) if h is not None and pd.notna(h) else None,
            p_level=float(p_level) if p_level is not None and pd.notna(p_level) else None,
            exit_date=exit_date,
            exit_ret=float(r.exit_ret) if hasattr(r, "exit_ret") and pd.notna(r.exit_ret) else None,
            exit_reason=str(r.exit_reason) if hasattr(r, "exit_reason") and pd.notna(r.exit_reason) else None,
            metadata_json=json.dumps(meta, ensure_ascii=False, default=str),
        ))
    db.commit()
