from __future__ import annotations

from datetime import date, timedelta, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..config import settings
from ..models import DataUpdateRun
from ..providers import get_provider
from .repository import upsert_stocks, upsert_bars, latest_trade_date


def sync_market(db, start=None, end=None, workers=6, limit=None):
    provider = get_provider()
    end = end or date.today()
    latest = latest_trade_date(db)
    if start is None:
        start = (latest - timedelta(days=7)) if latest else date(2021, 1, 1)

    run = DataUpdateRun(
        provider=settings.data_provider, start_date=start, end_date=end, status="running"
    )
    db.add(run); db.commit(); db.refresh(run)

    errors = []
    results = []
    try:
        stocks = provider.stock_list()
        upsert_stocks(db, stocks)
        rows = stocks[~stocks.is_st].copy()
        rows = rows.head(limit) if limit else rows
        run.stock_count = len(rows); db.commit()

        def one(r):
            code = str(r.code).zfill(6)
            return code, provider.history(code, start, end)

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
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
        run.finished_at = datetime.utcnow()
        db.commit()
        return results
    except Exception as e:
        run.status = "error"
        run.message = str(e)
        run.finished_at = datetime.utcnow()
        db.commit()
        raise
