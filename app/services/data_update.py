from __future__ import annotations

from datetime import date, timedelta, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
from sqlalchemy import select, func

from ..config import settings
from ..models import DataUpdateRun, Stock, DailyBar, BootstrapStock
from ..providers import get_provider
from .repository import upsert_stocks, upsert_bars, upsert_snapshot, latest_trade_date


def _new_run(db, provider: str, start: date | None, end: date | None) -> DataUpdateRun:
    run = DataUpdateRun(provider=provider, start_date=start, end_date=end, status="running")
    db.add(run); db.commit(); db.refresh(run)
    return run


def data_stats(db) -> dict:
    stocks = db.execute(select(func.count(Stock.code))).scalar_one()
    bars = db.execute(select(func.count(DailyBar.id))).scalar_one()
    earliest = db.execute(select(func.min(DailyBar.trade_date))).scalar_one_or_none()
    latest = db.execute(select(func.max(DailyBar.trade_date))).scalar_one_or_none()
    done = db.execute(select(func.count(BootstrapStock.code)).where(BootstrapStock.status == "ok")).scalar_one()
    errors = db.execute(select(func.count(BootstrapStock.code)).where(BootstrapStock.status == "error")).scalar_one()
    return {
        "stocks": int(stocks or 0), "bars": int(bars or 0), "earliest": earliest, "latest": latest,
        "bootstrap_done": int(done or 0), "bootstrap_errors": int(errors or 0),
    }


def bootstrap_batch(db, start=None, end=None, limit=None):
    """Backfill a resumable batch of current non-ST A shares from public history.

    Completion is tracked per stock in ``bootstrap_stocks`` so a restart/deploy does
    not lose progress. This is intentionally separate from the cheap daily EOD snapshot.
    """
    provider = get_provider()
    start = start or date.fromisoformat(settings.bootstrap_start_date)
    end = end or provider.latest_completed_trade_date()
    limit = int(limit or settings.bootstrap_batch_size)
    run = _new_run(db, f"{getattr(provider, 'name', 'provider')}-bootstrap", start, end)

    try:
        stocks = provider.stock_list()
        upsert_stocks(db, stocks)
        rows = stocks[~stocks.is_st].copy()
        completed = set(db.execute(
            select(BootstrapStock.code).where(BootstrapStock.status == "ok")
        ).scalars().all())
        rows = rows[~rows.code.astype(str).str.zfill(6).isin(completed)].head(limit)
        run.stock_count = len(rows); db.commit()

        if rows.empty:
            run.status = "complete"
            run.message = "所有当前非ST股票均已完成历史初始化"
            run.finished_at = datetime.utcnow(); db.commit()
            return []

        results = []
        errors = []
        # PublicDataProvider serializes BaoStock access; other providers may use >1 worker.
        workers = max(1, int(getattr(provider, "max_workers", 1)))

        def one(r):
            code = str(r.code).zfill(6)
            return code, provider.history(code, start, end)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(one, r): str(r.code).zfill(6) for r in rows.itertuples(index=False)}
            for fut in as_completed(futs):
                code = futs[fut]
                state = db.get(BootstrapStock, code) or BootstrapStock(code=code)
                if state not in db:
                    db.add(state)
                try:
                    c, frame = fut.result()
                    n = upsert_bars(db, c, frame)
                    if n <= 0:
                        raise RuntimeError("历史接口返回0条有效日线")
                    state.status = "ok"; state.row_count = n; state.message = ""
                    results.append((c, n))
                except Exception as e:
                    state.status = "error"; state.message = str(e)[:1000]
                    errors.append(f"{code}: {e}")
                state.updated_at = datetime.utcnow()
                db.commit()

        run.success_count = len(results)
        run.failed_count = len(errors)
        run.status = "ok" if not errors else "partial"
        run.message = "\n".join(errors[:100])
        run.finished_at = datetime.utcnow(); db.commit()
        return results
    except Exception as e:
        run.status = "error"; run.message = str(e); run.finished_at = datetime.utcnow(); db.commit()
        raise


def sync_daily_public(db):
    """Cheap production EOD update using one public all-A snapshot request.

    The target date is resolved against the A-share trading calendar and is never
    treated as complete before 16:00 Asia/Shanghai.
    """
    provider = get_provider()
    target = provider.latest_completed_trade_date()
    before = latest_trade_date(db)
    run = _new_run(db, f"{getattr(provider, 'name', 'provider')}-snapshot", target, target)
    try:
        snap = provider.daily_snapshot(target)
        meta = snap[["code", "name", "market", "board", "is_st"]].drop_duplicates("code")
        upsert_stocks(db, meta)
        valid = snap[~snap.is_st].copy()
        n = upsert_snapshot(db, valid)
        run.stock_count = len(valid)
        run.success_count = n
        run.failed_count = max(0, len(valid) - n)
        run.status = "ok" if n >= 3000 else "partial"

        note = f"EOD snapshot {target}: {n} rows"
        if before:
            try:
                missed = provider.trade_dates(before + timedelta(days=1), target)
                if len(missed) > 1:
                    note += f"; WARNING: DB可能缺失{len(missed)-1}个中间交易日，需执行历史补洞"
            except Exception:
                pass
        run.message = note
        run.finished_at = datetime.utcnow(); db.commit()
        return {"target": target, "rows": n, "message": note}
    except Exception as e:
        run.status = "error"; run.message = str(e); run.finished_at = datetime.utcnow(); db.commit()
        raise


def sync_market(db, start=None, end=None, workers=None, limit=None):
    """Per-stock historical repair/update path.

    Kept for explicit repairs. The normal daily job should use ``sync_daily_public``.
    """
    provider = get_provider()
    end = end or provider.latest_completed_trade_date()
    latest = latest_trade_date(db)
    if start is None:
        start = (latest - timedelta(days=7)) if latest else date.fromisoformat(settings.bootstrap_start_date)

    run = _new_run(db, getattr(provider, "name", settings.data_provider), start, end)
    errors = []
    results = []
    try:
        stocks = provider.stock_list()
        upsert_stocks(db, stocks)
        rows = stocks[~stocks.is_st].copy()
        rows = rows.head(limit) if limit else rows
        run.stock_count = len(rows); db.commit()

        if workers is None:
            workers = getattr(provider, "max_workers", 1)

        def one(r):
            code = str(r.code).zfill(6)
            return code, provider.history(code, start, end)

        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            futs = {ex.submit(one, r): str(r.code).zfill(6) for r in rows.itertuples(index=False)}
            for fut in as_completed(futs):
                code = futs[fut]
                try:
                    c, frame = fut.result()
                    results.append((c, upsert_bars(db, c, frame)))
                except Exception as e:
                    errors.append(f"{code}: {e}")

        run.success_count = len(results)
        run.failed_count = len(errors)
        run.status = "ok" if not errors else "partial"
        run.message = "\n".join(errors[:100])
        run.finished_at = datetime.utcnow(); db.commit()
        return results
    except Exception as e:
        run.status = "error"; run.message = str(e); run.finished_at = datetime.utcnow(); db.commit()
        raise
