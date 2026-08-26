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


def _bulk_upsert_dailybar_rows(db, rows):
    if not rows:
        return 0
    dialect = db.bind.dialect.name
    table = DailyBar.__table__
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
        stmt = insert(table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.code, table.c.trade_date],
            set_={
                "open": stmt.excluded.open, "high": stmt.excluded.high,
                "low": stmt.excluded.low, "close": stmt.excluded.close,
                "volume": stmt.excluded.volume, "amount": stmt.excluded.amount,
                "turnover": stmt.excluded.turnover,
            },
        )
        db.execute(stmt)
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        stmt = insert(table).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.code, table.c.trade_date],
            set_={
                "open": stmt.excluded.open, "high": stmt.excluded.high,
                "low": stmt.excluded.low, "close": stmt.excluded.close,
                "volume": stmt.excluded.volume, "amount": stmt.excluded.amount,
                "turnover": stmt.excluded.turnover,
            },
        )
        db.execute(stmt)
    else:
        # Portable fallback.
        for row in rows:
            obj = db.execute(
                select(DailyBar).where(
                    DailyBar.code == row["code"],
                    DailyBar.trade_date == row["trade_date"],
                )
            ).scalar_one_or_none()
            if obj is None:
                db.add(DailyBar(**row))
            else:
                for k in ("open", "high", "low", "close", "volume", "amount", "turnover"):
                    setattr(obj, k, row[k])
    db.commit()
    return len(rows)


def upsert_bars(db, code, frame):
    code = str(code).zfill(6)
    rows = []
    for r in frame.itertuples(index=False):
        vals = [float(r.open), float(r.high), float(r.low), float(r.close),
                float(r.volume), float(r.amount), float(r.turnover)]
        if not all(np.isfinite(x) for x in vals):
            continue
        rows.append({
            "code": code, "trade_date": pd.Timestamp(r.date).date(),
            "open": vals[0], "high": vals[1], "low": vals[2], "close": vals[3],
            "volume": vals[4], "amount": vals[5], "turnover": vals[6],
        })
    return _bulk_upsert_dailybar_rows(db, rows)


def upsert_snapshot(db, frame):
    rows = []
    for r in frame.itertuples(index=False):
        vals = [float(r.open), float(r.high), float(r.low), float(r.close),
                float(r.volume), float(r.amount), float(r.turnover)]
        if not all(np.isfinite(x) for x in vals):
            continue
        rows.append({
            "code": str(r.code).zfill(6),
            "trade_date": pd.Timestamp(r.date).date(),
            "open": vals[0], "high": vals[1], "low": vals[2], "close": vals[3],
            "volume": vals[4], "amount": vals[5], "turnover": vals[6],
        })
    # Keep statements comfortably below parameter limits.
    total = 0
    for i in range(0, len(rows), 1000):
        total += _bulk_upsert_dailybar_rows(db, rows[i:i+1000])
    return total


def latest_trade_date(db):
    return db.execute(select(func.max(DailyBar.trade_date))).scalar_one_or_none()


def _df_to_frames(x):
    if x is None or x.empty:
        return {}
    x = x.rename(columns={"trade_date": "date"})
    out = {}
    for code, g in x.groupby("code", sort=False):
        z = g[["date", "open", "high", "low", "close", "volume", "amount", "turnover"]].copy()
        z["date"] = pd.to_datetime(z["date"])
        out[str(code).zfill(6)] = z.sort_values("date").reset_index(drop=True)
    return out


def load_all_frames(db, start_date=None):
    cols = [
        DailyBar.code, DailyBar.trade_date, DailyBar.open, DailyBar.high, DailyBar.low,
        DailyBar.close, DailyBar.volume, DailyBar.amount, DailyBar.turnover,
    ]
    stmt = select(*cols)
    if start_date is not None:
        stmt = stmt.where(DailyBar.trade_date >= start_date)
    stmt = stmt.order_by(DailyBar.code, DailyBar.trade_date)
    x = pd.read_sql(stmt, db.bind)
    return _df_to_frames(x)



def load_all_frames_batched(
    db,
    start_date=None,
    batch_size: int = 160,
    progress_cb=None,
):
    """Load all frames in small code batches to cap peak pandas memory."""
    import gc

    code_stmt = select(DailyBar.code).distinct().order_by(DailyBar.code)
    if start_date is not None:
        code_stmt = code_stmt.where(DailyBar.trade_date >= start_date)

    codes = [str(x).zfill(6) for x in db.execute(code_stmt).scalars().all()]
    if not codes:
        return {}

    batch_size = max(20, int(batch_size))
    out = {}
    cols = [
        DailyBar.code, DailyBar.trade_date, DailyBar.open, DailyBar.high,
        DailyBar.low, DailyBar.close, DailyBar.volume, DailyBar.amount,
        DailyBar.turnover,
    ]

    total = len(codes)
    for start in range(0, total, batch_size):
        batch = codes[start:start + batch_size]
        stmt = select(*cols).where(DailyBar.code.in_(batch))
        if start_date is not None:
            stmt = stmt.where(DailyBar.trade_date >= start_date)
        stmt = stmt.order_by(DailyBar.code, DailyBar.trade_date)

        x = pd.read_sql(stmt, db.bind)
        out.update(_df_to_frames(x))
        del x
        gc.collect()

        done = min(total, start + len(batch))
        if progress_cb is not None:
            progress_cb(done, total, len(out))

    return out





def iter_frame_batches(
    db,
    start_date=None,
    end_date=None,
    batch_size: int = 160,
    codes=None,
    nonst_only: bool = True,
):
    """Yield (done, total, frames) without retaining the whole market in RAM."""
    if codes is None:
        q = select(Stock.code)
        if nonst_only:
            q = q.where(Stock.is_st.is_(False))
        codes = [str(x).zfill(6) for x in db.execute(q.order_by(Stock.code)).scalars().all()]
    else:
        codes = sorted({str(x).zfill(6) for x in codes})

    total = len(codes)
    if not total:
        return

    batch_size = max(20, int(batch_size))
    cols = [
        DailyBar.code, DailyBar.trade_date, DailyBar.open, DailyBar.high,
        DailyBar.low, DailyBar.close, DailyBar.volume, DailyBar.amount,
        DailyBar.turnover,
    ]
    for start in range(0, total, batch_size):
        batch = codes[start:start + batch_size]
        stmt = select(*cols).where(DailyBar.code.in_(batch))
        if start_date is not None:
            stmt = stmt.where(DailyBar.trade_date >= start_date)
        if end_date is not None:
            stmt = stmt.where(DailyBar.trade_date <= end_date)
        stmt = stmt.order_by(DailyBar.code, DailyBar.trade_date)
        x = pd.read_sql(stmt, db.bind)
        frames = _df_to_frames(x)
        done = min(total, start + len(batch))
        yield done, total, frames


def close_asof_for_codes(db, codes, asof_date):
    codes = sorted({str(c).zfill(6) for c in codes})
    if not codes:
        return {}
    sub = (
        select(DailyBar.code.label("code"), func.max(DailyBar.trade_date).label("mx"))
        .where(DailyBar.code.in_(codes), DailyBar.trade_date <= asof_date)
        .group_by(DailyBar.code)
        .subquery()
    )
    rows = db.execute(
        select(DailyBar.code, DailyBar.close)
        .join(sub, (DailyBar.code == sub.c.code) & (DailyBar.trade_date == sub.c.mx))
    ).all()
    return {str(code).zfill(6): float(close) for code, close in rows}

def load_frames_for_codes(db, codes, start_date=None):
    codes = sorted({str(c).zfill(6) for c in codes})
    if not codes:
        return {}
    cols = [
        DailyBar.code, DailyBar.trade_date, DailyBar.open, DailyBar.high, DailyBar.low,
        DailyBar.close, DailyBar.volume, DailyBar.amount, DailyBar.turnover,
    ]
    stmt = select(*cols).where(DailyBar.code.in_(codes))
    if start_date is not None:
        stmt = stmt.where(DailyBar.trade_date >= start_date)
    stmt = stmt.order_by(DailyBar.code, DailyBar.trade_date)
    x = pd.read_sql(stmt, db.bind)
    return _df_to_frames(x)


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

def replace_signals_for_date(db, frame, signal_date, version="V18-LIVE"):
    """Replace only one strategy/date slice; historical V18 rows are untouched."""
    db.execute(delete(Signal).where(
        Signal.strategy_version == version,
        Signal.signal_date == signal_date,
    ))
    db.commit()
    if frame is None or frame.empty:
        return 0

    count = 0
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
            if isinstance(v, date):
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
            exit_date=None,
            exit_ret=None,
            exit_reason=None,
            metadata_json=json.dumps(meta, ensure_ascii=False, default=str),
        ))
        count += 1
    db.commit()
    return count
